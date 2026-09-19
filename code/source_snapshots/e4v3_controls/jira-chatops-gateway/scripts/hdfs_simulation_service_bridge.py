#!/usr/bin/env python3
"""Bridge HDFS backtest requests/results to Bialobog's local watcher.

The vendor watcher only observes directories in ``masteruser``'s home. YARN
workers, however, can only exchange persistent files through HDFS. This
one-shot program is intended to run from cron once per minute on Bialobog.

Request delivery and result publication use same-filesystem/HDFS renames so a
consumer never observes a partially copied artifact. Retention cleanup is
limited to results published and verified by this bridge.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit


REDACT_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|token|secret|api[_-]?key)\s*[:=]\s*([^\s,;]+)"
)
REDACT_URL_USERINFO_RE = re.compile(r"(https?://)[^/@\s]+@", re.IGNORECASE)
JOB_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def sanitize(value: object, *, limit: int = 2000) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = REDACT_ASSIGNMENT_RE.sub(r"\1=[REDACTED]", text)
    text = REDACT_URL_USERINFO_RE.sub(r"\1[REDACTED]@", text)
    return text[:limit]


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-") or "unknown"


def utc_now(epoch: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if epoch is None else epoch, timezone.utc).isoformat()


def parse_bool(name: str, value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def parse_positive_int(name: str, value: str, *, allow_zero: bool = False) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    minimum = 0 if allow_zero else 1
    if parsed < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return parsed


def parse_positive_float(name: str, value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not parsed > 0:
        raise ValueError(f"{name} must be greater than 0")
    return parsed


def parse_cleanup_mode(name: str, value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in {"disabled", "report", "delete"}:
        raise ValueError(f"{name} must be one of: disabled, report, delete")
    return normalized


def validate_hdfs_uri(name: str, value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "hdfs" or not parsed.path.startswith("/") or parsed.path == "/":
        raise ValueError(
            f"{name} must be an hdfs:// URI with a non-root absolute path"
        )
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError(f"{name} must not contain credentials, a query, or a fragment")
    return value.rstrip("/")


@dataclass(frozen=True)
class BridgeConfig:
    hadoop_home: str
    hadoop_conf_dir: str
    hdfs_bin: str
    hdfs_requests_dir: str
    hdfs_results_dir: str
    local_requests_dir: Path
    local_results_dir: Path
    state_dir: Path
    log_path: Path
    stable_seconds: int
    retention_days: int
    min_free_gb: float
    resume_free_gb: float
    stuck_seconds: int
    cleanup_enabled: bool
    hdfs_retention_days: int
    hdfs_cleanup_mode: str

    @property
    def hdfs_tmp_results_dir(self) -> str:
        return f"{self.hdfs_results_dir}/.tmp-bridge"

    @property
    def request_state_dir(self) -> Path:
        return self.state_dir / "requests"

    @property
    def result_state_dir(self) -> Path:
        return self.state_dir / "results"

    @property
    def job_state_dir(self) -> Path:
        return self.state_dir / "jobs"

    @property
    def heartbeat_path(self) -> Path:
        return self.state_dir / "heartbeat.json"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "bridge.lock"

    @classmethod
    def from_env(cls) -> "BridgeConfig":
        hadoop_home = os.environ.get("HADOOP_HOME", "/opt/hadoop-3.4.1")
        min_free_gb = parse_positive_float(
            "HDFS_BRIDGE_MIN_FREE_GB", os.environ.get("HDFS_BRIDGE_MIN_FREE_GB", "10")
        )
        resume_free_gb = parse_positive_float(
            "HDFS_BRIDGE_RESUME_FREE_GB",
            os.environ.get("HDFS_BRIDGE_RESUME_FREE_GB", "12"),
        )
        if resume_free_gb < min_free_gb:
            raise ValueError("HDFS_BRIDGE_RESUME_FREE_GB must be at least HDFS_BRIDGE_MIN_FREE_GB")

        hdfs_requests_dir = validate_hdfs_uri(
            "HDFS_SIMULATION_REQUESTS_DIR",
            os.environ.get(
                "HDFS_SIMULATION_REQUESTS_DIR",
                "hdfs:///quant-sandbox/simulation-requests",
            ),
        )
        hdfs_results_dir = validate_hdfs_uri(
            "HDFS_SIMULATION_RESULTS_DIR",
            os.environ.get(
                "HDFS_SIMULATION_RESULTS_DIR",
                "hdfs:///quant-sandbox/simulation-results",
            ),
        )

        return cls(
            hadoop_home=hadoop_home,
            hadoop_conf_dir=os.environ.get(
                "HADOOP_CONF_DIR", f"{hadoop_home}/etc/hadoop"
            ),
            hdfs_bin=os.environ.get("HDFS_BIN", f"{hadoop_home}/bin/hdfs"),
            hdfs_requests_dir=hdfs_requests_dir,
            hdfs_results_dir=hdfs_results_dir,
            local_requests_dir=Path(
                os.environ.get(
                    "LOCAL_SIMULATION_REQUESTS_DIR",
                    "/home/masteruser/simulation-requests",
                )
            ),
            local_results_dir=Path(
                os.environ.get(
                    "LOCAL_SIMULATION_RESULTS_DIR",
                    "/home/masteruser/simulation-results",
                )
            ),
            state_dir=Path(
                os.environ.get(
                    "HDFS_BRIDGE_STATE_DIR", "/home/masteruser/.quant-hdfs-bridge"
                )
            ),
            log_path=Path(
                os.environ.get(
                    "HDFS_BRIDGE_LOG",
                    "/opt/airflow3/logs/hdfs-simulation-service-bridge.log",
                )
            ),
            stable_seconds=parse_positive_int(
                "HDFS_BRIDGE_RESULT_STABLE_SECONDS",
                os.environ.get("HDFS_BRIDGE_RESULT_STABLE_SECONDS", "45"),
                allow_zero=True,
            ),
            retention_days=parse_positive_int(
                "HDFS_BRIDGE_RETENTION_DAYS",
                os.environ.get("HDFS_BRIDGE_RETENTION_DAYS", "7"),
            ),
            min_free_gb=min_free_gb,
            resume_free_gb=resume_free_gb,
            stuck_seconds=parse_positive_int(
                "HDFS_BRIDGE_STUCK_SECONDS",
                os.environ.get("HDFS_BRIDGE_STUCK_SECONDS", "10800"),
            ),
            cleanup_enabled=parse_bool(
                "HDFS_BRIDGE_CLEANUP_ENABLED",
                os.environ.get("HDFS_BRIDGE_CLEANUP_ENABLED", "false"),
            ),
            hdfs_retention_days=parse_positive_int(
                "HDFS_BRIDGE_HDFS_RETENTION_DAYS",
                os.environ.get("HDFS_BRIDGE_HDFS_RETENTION_DAYS", "30"),
            ),
            hdfs_cleanup_mode=parse_cleanup_mode(
                "HDFS_BRIDGE_HDFS_CLEANUP_MODE",
                os.environ.get("HDFS_BRIDGE_HDFS_CLEANUP_MODE", "disabled"),
            ),
        )


Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


class Bridge:
    def __init__(
        self,
        config: BridgeConfig,
        *,
        runner: Runner | None = None,
        now_fn: Callable[[], float] = time.time,
        disk_usage_fn: Callable[[Path], Any] = shutil.disk_usage,
    ) -> None:
        self.config = config
        self._runner = runner or self._subprocess_runner
        self._now = now_fn
        self._disk_usage = disk_usage_fn
        self.last_error: str | None = None
        self.stats = {
            "requests_delivered": 0,
            "requests_consumed": 0,
            "results_published": 0,
            "results_cleaned": 0,
            "local_retention_cleaned": 0,
            "local_emergency_cleaned": 0,
            "hdfs_cleanup_candidates": 0,
            "hdfs_results_cleaned": 0,
            "collisions": 0,
            "errors": 0,
        }

    def _subprocess_runner(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["HADOOP_HOME"] = self.config.hadoop_home
        env["HADOOP_CONF_DIR"] = self.config.hadoop_conf_dir
        env["PATH"] = (
            f"{self.config.hadoop_home}/bin:{self.config.hadoop_home}/sbin:"
            + env.get("PATH", "")
        )
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
        )

    def hdfs(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self._runner([self.config.hdfs_bin, "dfs", *args])

    def log(self, message: str) -> None:
        self.config.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.config.log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{utc_now(self._now())} {sanitize(message)}\n")

    def record_error(self, message: str, *, job_name: str | None = None) -> None:
        clean = sanitize(message)
        self.last_error = clean
        self.stats["errors"] += 1
        self.log(f"ERROR {clean}")
        if job_name:
            self.update_job(job_name, "error", error=clean)

    @staticmethod
    def read_json(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def atomic_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
        try:
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(value, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def ensure_active_dirs(self) -> None:
        self.config.local_requests_dir.mkdir(parents=True, exist_ok=True)
        self.config.local_results_dir.mkdir(parents=True, exist_ok=True)
        self.config.request_state_dir.mkdir(parents=True, exist_ok=True)
        self.config.result_state_dir.mkdir(parents=True, exist_ok=True)
        self.config.job_state_dir.mkdir(parents=True, exist_ok=True)
        completed = self.hdfs(
            "-mkdir",
            "-p",
            self.config.hdfs_requests_dir,
            self.config.hdfs_results_dir,
            self.config.hdfs_tmp_results_dir,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"HDFS mkdir failed: {sanitize(completed.stderr)}")

    def check(self) -> dict[str, Any]:
        checks: dict[str, Any] = {
            "mode": "check",
            "checked_at": utc_now(self._now()),
            "hdfs_requests_dir": self.config.hdfs_requests_dir,
            "hdfs_results_dir": self.config.hdfs_results_dir,
            "local_requests_dir": str(self.config.local_requests_dir),
            "local_results_dir": str(self.config.local_results_dir),
            "hdfs_cleanup_mode": self.config.hdfs_cleanup_mode,
            "hdfs_retention_days": self.config.hdfs_retention_days,
        }
        errors: list[str] = []
        if not os.access(self.config.hdfs_bin, os.X_OK):
            errors.append(f"HDFS binary is not executable: {self.config.hdfs_bin}")
        for label, path in (
            ("local request directory", self.config.local_requests_dir),
            ("local result directory", self.config.local_results_dir),
        ):
            if not path.is_dir():
                errors.append(f"{label} does not exist: {path}")
            elif not os.access(path, os.R_OK | os.W_OK | os.X_OK):
                errors.append(f"{label} is not readable/writable: {path}")
        for label, path in (
            ("HDFS request directory", self.config.hdfs_requests_dir),
            ("HDFS result directory", self.config.hdfs_results_dir),
        ):
            completed = self.hdfs("-test", "-d", path)
            if completed.returncode != 0:
                errors.append(f"{label} is unavailable: {sanitize(completed.stderr)}")

        disk_target = (
            self.config.local_results_dir
            if self.config.local_results_dir.exists()
            else self.config.local_results_dir.parent
        )
        free_bytes = self._disk_usage(disk_target).free
        checks["free_bytes"] = free_bytes
        checks["free_gb"] = round(free_bytes / (1024**3), 3)
        checks["minimum_free_gb"] = self.config.min_free_gb
        checks["resume_free_gb"] = self.config.resume_free_gb
        checks["intake_would_pause"] = free_bytes < self.config.min_free_gb * 1024**3
        if checks["intake_would_pause"]:
            errors.append(
                f"free disk is below the {self.config.min_free_gb} GiB intake threshold"
            )
        checks["healthy"] = not errors
        checks["errors"] = [sanitize(error) for error in errors]
        return checks

    def job_path(self, job_name: str) -> Path:
        return self.config.job_state_dir / f"{safe_name(job_name)}.json"

    def update_job(self, job_name: str, status: str, **fields: Any) -> dict[str, Any]:
        path = self.job_path(job_name)
        state = self.read_json(path) or {"job_name": job_name}
        state.update(fields)
        state["status"] = status
        state["updated_at"] = utc_now(self._now())
        state["updated_epoch"] = self._now()
        self.atomic_json(path, state)
        return state

    def hdfs_exists(self, path: str) -> bool:
        return self.hdfs("-test", "-e", path).returncode == 0

    def hdfs_is_dir(self, path: str) -> bool:
        return self.hdfs("-test", "-d", path).returncode == 0

    def hdfs_size(self, path: str) -> int | None:
        completed = self.hdfs("-stat", "%b", path)
        if completed.returncode != 0:
            return None
        try:
            return int(completed.stdout.strip())
        except ValueError:
            return None

    def hdfs_checksum(self, path: str) -> str | None:
        completed = self.hdfs("-checksum", path)
        if completed.returncode != 0:
            return None
        parts = completed.stdout.split()
        if len(parts) < 3:
            return None
        return f"{parts[-2]}:{parts[-1]}"

    def list_hdfs_requests(self) -> list[str]:
        completed = self.hdfs("-ls", self.config.hdfs_requests_dir)
        if completed.returncode != 0:
            raise RuntimeError(f"HDFS request listing failed: {sanitize(completed.stderr)}")
        requests: list[str] = []
        for line in completed.stdout.splitlines():
            parts = line.split()
            if len(parts) < 8:
                continue
            path = parts[-1]
            name = path.rsplit("/", 1)[-1]
            if not name.startswith(".") and name.endswith(".request"):
                requests.append(path)
        return sorted(set(requests))

    def request_marker_path(self, request_name: str) -> Path:
        return self.config.request_state_dir / f"{safe_name(request_name)}.json"

    def deliver_requests(self) -> None:
        for remote in self.list_hdfs_requests():
            request_name = remote.rsplit("/", 1)[-1]
            job_name = request_name.removesuffix(".request")
            if not JOB_NAME_RE.fullmatch(job_name):
                self.record_error(f"invalid HDFS request job name: {request_name}")
                continue
            marker_path = self.request_marker_path(request_name)
            local_path = self.config.local_requests_dir / request_name
            if marker_path.exists():
                marker = self.read_json(marker_path)
                if marker is None:
                    self.record_error(f"invalid request marker: {marker_path}", job_name=job_name)
                    continue
                if not self.job_path(job_name).exists():
                    self.update_job(
                        job_name,
                        "delivered" if local_path.exists() else "consumed",
                        remote_request=remote,
                        local_request=str(local_path),
                        delivered_at=marker.get("copied_at") or marker.get("marked_at"),
                    )
                continue

            if local_path.exists():
                marker = {
                    "remote": remote,
                    "local": str(local_path),
                    "job_name": job_name,
                    "status": "already_local",
                    "marked_at": utc_now(self._now()),
                }
                self.atomic_json(marker_path, marker)
                self.update_job(
                    job_name,
                    "delivered",
                    remote_request=remote,
                    local_request=str(local_path),
                    delivered_at=marker["marked_at"],
                    delivered_epoch=self._now(),
                )
                continue

            completed = self.hdfs("-cat", remote)
            if completed.returncode != 0:
                self.record_error(
                    f"HDFS request read failed remote={remote}: {completed.stderr}",
                    job_name=job_name,
                )
                continue

            tmp = local_path.with_name(f".{request_name}.tmp.{os.getpid()}")
            try:
                tmp.write_text(completed.stdout, encoding="utf-8")
                tmp.chmod(0o644)
                os.replace(tmp, local_path)
            finally:
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass

            delivered_at = utc_now(self._now())
            self.atomic_json(
                marker_path,
                {
                    "remote": remote,
                    "local": str(local_path),
                    "job_name": job_name,
                    "status": "copied_to_local",
                    "copied_at": delivered_at,
                },
            )
            self.update_job(
                job_name,
                "delivered",
                remote_request=remote,
                local_request=str(local_path),
                delivered_at=delivered_at,
                delivered_epoch=self._now(),
            )
            self.stats["requests_delivered"] += 1
            self.log(f"DELIVERED request={request_name} job={job_name}")

    def request_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for marker_path in sorted(self.config.request_state_dir.glob("*.json")):
            marker = self.read_json(marker_path)
            if marker is None:
                self.record_error(f"invalid request marker: {marker_path}")
                continue
            job_name = marker.get("job_name")
            if isinstance(job_name, str) and JOB_NAME_RE.fullmatch(job_name):
                records.append(marker)
            else:
                self.record_error(f"invalid job name in request marker: {marker_path}")
        return records

    def is_stable(self, path: Path) -> bool:
        try:
            if path.is_file():
                return self._now() - path.stat().st_mtime >= self.config.stable_seconds
            if not path.is_dir():
                return False
            latest = path.stat().st_mtime
            has_file = False
            for root, dirs, files in os.walk(path, followlinks=False):
                root_path = Path(root)
                for name in dirs + files:
                    child = root_path / name
                    if child.is_symlink():
                        raise ValueError(f"result tree contains symlink: {child}")
                for name in files:
                    has_file = True
                    latest = max(latest, (root_path / name).stat().st_mtime)
            return has_file and self._now() - latest >= self.config.stable_seconds
        except (FileNotFoundError, OSError):
            return False

    def terminal_kind(self, result_path: Path) -> str | None:
        if result_path.is_symlink():
            raise ValueError(f"result path is a symlink: {result_path}")
        if result_path.is_file():
            return "rejected" if self.is_stable(result_path) else None
        if result_path.is_dir():
            results_table = result_path / "resultsTable.csv"
            if results_table.is_symlink():
                raise ValueError(f"resultsTable.csv is a symlink: {results_table}")
            if results_table.is_file() and self.is_stable(result_path):
                return "completed"
        return None

    @staticmethod
    def terminal_paths(
        local_path: Path, final_hdfs_path: str, kind: str
    ) -> tuple[Path, str, str]:
        if kind == "completed":
            local_terminal = local_path / "resultsTable.csv"
            remote_terminal = f"{final_hdfs_path}/resultsTable.csv"
            relative_terminal = "resultsTable.csv"
        else:
            local_terminal = local_path
            remote_terminal = final_hdfs_path
            relative_terminal = "."
        return local_terminal, remote_terminal, relative_terminal

    def verify_hdfs_result(self, local_path: Path, final_hdfs_path: str, kind: str) -> bool:
        if kind == "completed":
            if not self.hdfs_is_dir(final_hdfs_path):
                return False
        elif not self.hdfs_exists(final_hdfs_path) or self.hdfs_is_dir(final_hdfs_path):
            return False
        local_terminal, remote_terminal, _ = self.terminal_paths(
            local_path, final_hdfs_path, kind
        )
        try:
            local_size = local_terminal.stat().st_size
        except OSError:
            return False
        return self.hdfs_size(remote_terminal) == local_size

    @staticmethod
    def file_sha256(path: Path) -> str | None:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            return None
        return digest.hexdigest()

    def verify_marker_result(
        self,
        marker: dict[str, Any],
        local_path: Path,
        hdfs_path: str,
        kind: str,
    ) -> bool:
        expected_size = marker.get("terminal_size")
        expected_checksum = marker.get("terminal_checksum")
        expected_local_sha256 = marker.get("terminal_local_sha256")
        if not (
            isinstance(expected_size, int)
            and isinstance(expected_checksum, str)
            and isinstance(expected_local_sha256, str)
        ):
            return self.verify_hdfs_result(local_path, hdfs_path, kind)
        local_terminal, remote_terminal, _ = self.terminal_paths(local_path, hdfs_path, kind)
        try:
            if local_terminal.stat().st_size != expected_size:
                return False
        except OSError:
            return False
        return (
            self.file_sha256(local_terminal) == expected_local_sha256
            and self.hdfs_size(remote_terminal) == expected_size
            and self.hdfs_checksum(remote_terminal) == expected_checksum
        )

    def publish_result(self, job_name: str, local_path: Path, kind: str) -> None:
        final_hdfs_path = f"{self.config.hdfs_results_dir}/{job_name}"
        if self.hdfs_exists(final_hdfs_path):
            if self.verify_hdfs_result(local_path, final_hdfs_path, kind):
                self.mark_published(job_name, local_path, final_hdfs_path, kind, already=True)
            else:
                self.stats["collisions"] += 1
                self.stats["errors"] += 1
                self.last_error = sanitize(
                    f"HDFS result collision for {job_name}: {final_hdfs_path}"
                )
                self.update_job(
                    job_name,
                    "collision",
                    local_result=str(local_path),
                    hdfs_result=final_hdfs_path,
                )
                self.log(f"COLLISION job={job_name} hdfs={final_hdfs_path}")
            return

        temp_parent = (
            f"{self.config.hdfs_tmp_results_dir}/{safe_name(job_name)}."
            f"{int(self._now())}.{os.getpid()}"
        )
        mkdir = self.hdfs("-mkdir", "-p", temp_parent)
        if mkdir.returncode != 0:
            raise RuntimeError(f"HDFS temporary mkdir failed: {sanitize(mkdir.stderr)}")
        try:
            uploaded = f"{temp_parent}/{local_path.name}"
            put = self.hdfs("-put", str(local_path), temp_parent)
            if put.returncode != 0:
                raise RuntimeError(f"HDFS result upload failed: {sanitize(put.stderr)}")
            move = self.hdfs("-mv", uploaded, final_hdfs_path)
            if move.returncode != 0:
                raise RuntimeError(f"HDFS result publish failed: {sanitize(move.stderr)}")
        finally:
            self.hdfs("-rm", "-r", "-f", temp_parent)

        if not self.verify_hdfs_result(local_path, final_hdfs_path, kind):
            raise RuntimeError(f"HDFS result verification failed: {final_hdfs_path}")
        self.mark_published(job_name, local_path, final_hdfs_path, kind, already=False)

    def result_marker_path(self, job_name: str) -> Path:
        return self.config.result_state_dir / f"{safe_name(job_name)}.json"

    def mark_published(
        self,
        job_name: str,
        local_path: Path,
        hdfs_path: str,
        kind: str,
        *,
        already: bool,
    ) -> None:
        local_terminal, remote_terminal, relative_terminal = self.terminal_paths(
            local_path, hdfs_path, kind
        )
        terminal_size = self.hdfs_size(remote_terminal)
        terminal_checksum = self.hdfs_checksum(remote_terminal)
        if terminal_size is None:
            raise RuntimeError(f"HDFS terminal metadata unavailable: {remote_terminal}")
        try:
            local_terminal_size = local_terminal.stat().st_size
        except OSError as exc:
            raise RuntimeError(f"local terminal metadata unavailable: {local_terminal}") from exc
        if terminal_size != local_terminal_size:
            raise RuntimeError(f"HDFS terminal size changed: {remote_terminal}")

        marker = {
            "job_name": job_name,
            "local_result": str(local_path),
            "hdfs_result": hdfs_path,
            "kind": kind,
            "status": "published",
            "verified": True,
            "published_at": utc_now(self._now()),
            "published_epoch": self._now(),
            "already_hdfs": already,
            "terminal_relative_path": relative_terminal,
            "terminal_size": terminal_size,
            "terminal_checksum": terminal_checksum,
            "terminal_local_sha256": self.file_sha256(local_terminal),
        }
        self.atomic_json(self.result_marker_path(job_name), marker)
        job_fields = dict(marker)
        job_fields.pop("status")
        job_fields.pop("job_name")
        self.update_job(job_name, "published", **job_fields)
        self.stats["results_published"] += 1
        self.log(f"PUBLISHED result job={job_name} kind={kind} hdfs={hdfs_path}")

    @staticmethod
    def marker_epoch(marker: dict[str, Any]) -> float | None:
        for key in ("delivered_epoch", "updated_epoch", "published_epoch"):
            value = marker.get(key)
            if isinstance(value, (int, float)):
                return float(value)
        for key in ("copied_at", "marked_at", "delivered_at"):
            value = marker.get(key)
            if isinstance(value, str) and value:
                try:
                    return datetime.fromisoformat(value).timestamp()
                except ValueError:
                    continue
        return None

    def reconcile_and_publish(self) -> None:
        for record in self.request_records():
            job_name = str(record["job_name"])
            local_request = self.config.local_requests_dir / f"{job_name}.request"
            local_result = self.config.local_results_dir / job_name
            try:
                kind = self.terminal_kind(local_result)
            except ValueError as exc:
                self.record_error(str(exc), job_name=job_name)
                continue

            if kind:
                published_marker = self.read_json(self.result_marker_path(job_name)) or {}
                if (
                    published_marker.get("status") == "published"
                    and published_marker.get("verified") is True
                ):
                    current = self.read_json(self.job_path(job_name)) or {}
                    if current.get("status") != "published":
                        self.update_job(job_name, "published")
                    continue
                self.update_job(job_name, kind, local_result=str(local_result))
                try:
                    self.publish_result(job_name, local_result, kind)
                except RuntimeError as exc:
                    self.record_error(str(exc), job_name=job_name)
                continue

            job_state = self.read_json(self.job_path(job_name)) or {}
            if not local_request.exists() and job_state.get("status") == "delivered":
                self.update_job(job_name, "consumed")
                self.stats["requests_consumed"] += 1

            started = self.marker_epoch(job_state) or self.marker_epoch(record)
            if started is not None and self._now() - started >= self.config.stuck_seconds:
                current = self.read_json(self.job_path(job_name)) or {}
                if current.get("status") not in {
                    "published",
                    "collision",
                    "error",
                    "timed_out",
                }:
                    self.update_job(job_name, "timed_out", timed_out_at=utc_now(self._now()))

    def local_result_candidates(
        self, *, retention_seconds: float | None
    ) -> list[tuple[float, Path, dict[str, Any], Path, str, str, str]]:
        root = self.config.local_results_dir.resolve()
        candidates: list[tuple[float, Path, dict[str, Any], Path, str, str, str]] = []
        for marker_path in sorted(self.config.result_state_dir.glob("*.json")):
            marker = self.read_json(marker_path)
            if (
                not marker
                or marker.get("status") != "published"
                or marker.get("verified") is not True
                or marker.get("local_cleaned_epoch") is not None
            ):
                continue
            local_value = marker.get("local_result") or marker.get("local_result_dir")
            hdfs_value = marker.get("hdfs_result") or marker.get("hdfs_result_dir")
            job_name = marker.get("job_name")
            kind = marker.get("kind", "completed")
            if not all(isinstance(v, str) and v for v in (local_value, hdfs_value, job_name)):
                continue
            if kind not in {"completed", "rejected"}:
                continue
            local_path = Path(str(local_value))
            try:
                if local_path.is_symlink():
                    continue
                resolved = local_path.resolve(strict=True)
                resolved.relative_to(root)
                retention_start = local_path.stat().st_mtime
                published_epoch = marker.get("published_epoch")
                if isinstance(published_epoch, (int, float)):
                    retention_start = max(retention_start, float(published_epoch))
                if (
                    retention_seconds is not None
                    and self._now() - retention_start < retention_seconds
                ):
                    continue
            except (FileNotFoundError, OSError, ValueError):
                continue
            candidate_order = retention_start
            if retention_seconds is None and isinstance(published_epoch, (int, float)):
                candidate_order = float(published_epoch)
            candidates.append(
                (
                    candidate_order,
                    marker_path,
                    marker,
                    local_path,
                    str(hdfs_value),
                    str(job_name),
                    str(kind),
                )
            )
        return sorted(candidates, key=lambda item: (item[0], str(item[3])))

    def delete_verified_local_result(
        self,
        marker_path: Path,
        marker: dict[str, Any],
        local_path: Path,
        hdfs_path: str,
        job_name: str,
        kind: str,
        *,
        reason: str,
    ) -> bool:
        if not self.verify_marker_result(marker, local_path, hdfs_path, kind):
            self.record_error(
                f"local cleanup verification failed for {job_name}", job_name=job_name
            )
            return False
        try:
            if local_path.is_dir():
                shutil.rmtree(local_path)
            elif local_path.is_file():
                local_path.unlink()
            else:
                return False
        except OSError as exc:
            self.record_error(f"local cleanup failed for {job_name}: {exc}", job_name=job_name)
            return False
        marker["local_cleaned_at"] = utc_now(self._now())
        marker["local_cleaned_epoch"] = self._now()
        marker["local_cleanup_reason"] = reason
        self.atomic_json(marker_path, marker)
        self.stats["results_cleaned"] += 1
        self.stats[
            "local_emergency_cleaned" if reason == "disk_pressure" else "local_retention_cleaned"
        ] += 1
        self.log(f"CLEANED verified local result job={job_name} reason={reason}")
        return True

    def cleanup_verified_results(self) -> None:
        if not self.config.cleanup_enabled:
            return
        retention_seconds = self.config.retention_days * 86400
        for _, marker_path, marker, local_path, hdfs_path, job_name, kind in (
            self.local_result_candidates(retention_seconds=retention_seconds)
        ):
            self.delete_verified_local_result(
                marker_path,
                marker,
                local_path,
                hdfs_path,
                job_name,
                kind,
                reason="retention",
            )

    def cleanup_for_disk_pressure(self) -> None:
        if not self.config.cleanup_enabled:
            return
        free_bytes = self._disk_usage(self.config.local_results_dir).free
        trigger_bytes = self.config.min_free_gb * 1024**3
        target_bytes = self.config.resume_free_gb * 1024**3
        if free_bytes >= trigger_bytes:
            return
        free_before = free_bytes
        for _, marker_path, marker, local_path, hdfs_path, job_name, kind in (
            self.local_result_candidates(retention_seconds=None)
        ):
            if free_bytes >= target_bytes:
                break
            if self.delete_verified_local_result(
                marker_path,
                marker,
                local_path,
                hdfs_path,
                job_name,
                kind,
                reason="disk_pressure",
            ):
                free_bytes = self._disk_usage(self.config.local_results_dir).free
        self.log(
            f"DISK_PRESSURE_CLEANUP free_before_gb={free_before / (1024**3):.3f} "
            f"free_after_gb={free_bytes / (1024**3):.3f} "
            f"target_gb={self.config.resume_free_gb:g}"
        )

    def cleanup_hdfs_results(self) -> None:
        if self.config.hdfs_cleanup_mode == "disabled":
            return
        retention_seconds = self.config.hdfs_retention_days * 86400
        for marker_path in sorted(self.config.result_state_dir.glob("*.json")):
            marker = self.read_json(marker_path)
            if (
                not marker
                or marker.get("status") != "published"
                or marker.get("verified") is not True
                or marker.get("hdfs_cleaned_epoch") is not None
            ):
                continue
            job_name = marker.get("job_name")
            hdfs_path = marker.get("hdfs_result") or marker.get("hdfs_result_dir")
            kind = marker.get("kind")
            published_epoch = marker.get("published_epoch")
            terminal_relative = marker.get("terminal_relative_path")
            terminal_size = marker.get("terminal_size")
            terminal_checksum = marker.get("terminal_checksum")
            if (
                not isinstance(job_name, str)
                or not JOB_NAME_RE.fullmatch(job_name)
                or not isinstance(hdfs_path, str)
                or hdfs_path != f"{self.config.hdfs_results_dir}/{job_name}"
                or kind not in {"completed", "rejected"}
                or terminal_relative
                != ("resultsTable.csv" if kind == "completed" else ".")
                or not isinstance(published_epoch, (int, float))
                or not isinstance(terminal_size, int)
                or not isinstance(terminal_checksum, str)
                or self._now() - float(published_epoch) < retention_seconds
            ):
                continue
            terminal_path = (
                hdfs_path
                if terminal_relative == "."
                else f"{hdfs_path}/{terminal_relative}"
            )
            if (
                self.hdfs_size(terminal_path) != terminal_size
                or self.hdfs_checksum(terminal_path) != terminal_checksum
            ):
                self.record_error(
                    f"HDFS retention verification failed for {job_name}", job_name=job_name
                )
                continue
            self.stats["hdfs_cleanup_candidates"] += 1
            if self.config.hdfs_cleanup_mode == "report":
                self.log(f"HDFS_RETENTION_CANDIDATE job={job_name} hdfs={hdfs_path}")
                continue
            removed = (
                self.hdfs("-rm", "-r", hdfs_path)
                if kind == "completed"
                else self.hdfs("-rm", hdfs_path)
            )
            if removed.returncode != 0:
                self.record_error(
                    f"HDFS retention delete failed for {job_name}: {removed.stderr}",
                    job_name=job_name,
                )
                continue
            marker["hdfs_cleaned_at"] = utc_now(self._now())
            marker["hdfs_cleaned_epoch"] = self._now()
            self.atomic_json(marker_path, marker)
            self.stats["hdfs_results_cleaned"] += 1
            self.log(f"HDFS_RETENTION_CLEANED job={job_name} hdfs={hdfs_path}")

    def prior_intake_paused(self) -> bool:
        heartbeat = self.read_json(self.config.heartbeat_path) or {}
        return heartbeat.get("intake_paused") is True

    def disk_state(self) -> tuple[int, bool]:
        free_bytes = self._disk_usage(self.config.local_results_dir).free
        free_gb = free_bytes / (1024**3)
        if self.prior_intake_paused():
            paused = free_gb < self.config.resume_free_gb
        else:
            paused = free_gb < self.config.min_free_gb
        return free_bytes, paused

    def state_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for path in self.config.job_state_dir.glob("*.json"):
            state = self.read_json(path) or {}
            status = state.get("status")
            if isinstance(status, str):
                counts[status] = counts.get(status, 0) + 1
        return counts

    def write_heartbeat(self, *, free_bytes: int, intake_paused: bool, healthy: bool) -> None:
        prior = self.read_json(self.config.heartbeat_path) or {}
        self.atomic_json(
            self.config.heartbeat_path,
            {
                "updated_at": utc_now(self._now()),
                "updated_epoch": self._now(),
                "healthy": healthy,
                "intake_paused": intake_paused,
                "free_bytes": free_bytes,
                "free_gb": round(free_bytes / (1024**3), 3),
                "minimum_free_gb": self.config.min_free_gb,
                "resume_free_gb": self.config.resume_free_gb,
                "cleanup_enabled": self.config.cleanup_enabled,
                "hdfs_cleanup_mode": self.config.hdfs_cleanup_mode,
                "hdfs_retention_days": self.config.hdfs_retention_days,
                "job_counts": self.state_counts(),
                "run_counts": self.stats,
                "last_error": self.last_error or prior.get("last_error"),
            },
        )

    def run(self) -> None:
        self.ensure_active_dirs()
        self.reconcile_and_publish()
        self.cleanup_verified_results()
        self.cleanup_for_disk_pressure()
        self.cleanup_hdfs_results()
        free_bytes, intake_paused = self.disk_state()
        if intake_paused:
            self.log(
                f"INTAKE_PAUSED free_gb={free_bytes / (1024**3):.3f} "
                f"minimum_gb={self.config.min_free_gb} resume_gb={self.config.resume_free_gb}"
            )
        else:
            try:
                self.deliver_requests()
            except RuntimeError as exc:
                self.record_error(str(exc))
        self.write_heartbeat(
            free_bytes=free_bytes,
            intake_paused=intake_paused,
            healthy=self.stats["errors"] == 0,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate configured local/HDFS visibility without copying, deleting, or writing state.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = BridgeConfig.from_env()
    except ValueError as exc:
        print(f"configuration error: {sanitize(exc)}", file=sys.stderr)
        return 2
    bridge = Bridge(config)

    if args.check:
        result = bridge.check()
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["healthy"] else 1

    config.state_dir.mkdir(parents=True, exist_ok=True)
    with config.lock_path.open("w", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            bridge.log("SKIP another bridge instance is running")
            return 0
        try:
            bridge.run()
            return 0
        except Exception as exc:
            bridge.record_error(f"FATAL {type(exc).__name__}: {exc}")
            try:
                free_bytes, paused = bridge.disk_state()
                bridge.write_heartbeat(
                    free_bytes=free_bytes, intake_paused=paused, healthy=False
                )
            except Exception:
                pass
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
