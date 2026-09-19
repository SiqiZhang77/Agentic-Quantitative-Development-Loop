from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import hdfs_simulation_service_bridge as bridge_module


class FakeHdfs:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = set()
        self.commands: list[tuple[str, ...]] = []
        self.fail: dict[str, str] = {}
        self.no_checksum: set[str] = set()

    @staticmethod
    def clean(path: str) -> str:
        return path.rstrip("/")

    def add_dir(self, path: str) -> None:
        self.dirs.add(self.clean(path))

    def add_file(self, path: str, content: bytes | str) -> None:
        self.files[self.clean(path)] = (
            content.encode("utf-8") if isinstance(content, str) else content
        )

    def result(self, args: list[str], rc: int = 0, out: str = "", err: str = ""):
        return subprocess.CompletedProcess(args, rc, out, err)

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        args = cmd[2:]
        self.commands.append(tuple(args))
        operation = args[0]
        if operation in self.fail:
            return self.result(cmd, 1, err=self.fail[operation])

        if operation == "-mkdir":
            for path in args[2:]:
                self.add_dir(path)
            return self.result(cmd)
        if operation == "-test":
            flag, path = args[1], self.clean(args[2])
            exists = path in self.files or path in self.dirs
            is_dir = path in self.dirs
            ok = exists if flag == "-e" else is_dir
            return self.result(cmd, 0 if ok else 1)
        if operation == "-ls":
            root = self.clean(args[1])
            prefix = root + "/"
            children: set[str] = set()
            for path in set(self.files) | self.dirs:
                if path.startswith(prefix):
                    relative = path[len(prefix) :]
                    if relative and "/" not in relative:
                        children.add(path)
            lines = []
            for path in sorted(children):
                if path in self.dirs:
                    lines.append(f"drwxr-xr-x - owner group 0 2026-01-01 00:00 {path}")
                else:
                    lines.append(
                        f"-rw-r--r-- 3 owner group {len(self.files[path])} "
                        f"2026-01-01 00:00 {path}"
                    )
            return self.result(cmd, out=(f"Found {len(lines)} items\n" + "\n".join(lines)))
        if operation == "-cat":
            path = self.clean(args[1])
            if path not in self.files:
                return self.result(cmd, 1, err="not found")
            return self.result(cmd, out=self.files[path].decode("utf-8"))
        if operation == "-put":
            local = Path(args[1])
            destination = self.clean(args[2])
            if local.is_file():
                self.add_file(f"{destination}/{local.name}", local.read_bytes())
            else:
                target = f"{destination}/{local.name}"
                self.add_dir(target)
                for path in local.rglob("*"):
                    relative = path.relative_to(local).as_posix()
                    if path.is_dir():
                        self.add_dir(f"{target}/{relative}")
                    else:
                        self.add_file(f"{target}/{relative}", path.read_bytes())
            return self.result(cmd)
        if operation == "-mv":
            source, destination = map(self.clean, args[1:3])
            if source in self.files:
                self.files[destination] = self.files.pop(source)
                return self.result(cmd)
            if source in self.dirs:
                old_dirs = [p for p in self.dirs if p == source or p.startswith(source + "/")]
                old_files = [p for p in self.files if p.startswith(source + "/")]
                for path in old_dirs:
                    self.dirs.remove(path)
                    self.dirs.add(destination + path[len(source) :])
                for path in old_files:
                    value = self.files.pop(path)
                    self.files[destination + path[len(source) :]] = value
                return self.result(cmd)
            return self.result(cmd, 1, err="source missing")
        if operation == "-rm":
            target = self.clean(args[-1])
            self.files = {
                path: value
                for path, value in self.files.items()
                if path != target and not path.startswith(target + "/")
            }
            self.dirs = {
                path
                for path in self.dirs
                if path != target and not path.startswith(target + "/")
            }
            return self.result(cmd)
        if operation == "-stat":
            path = self.clean(args[2])
            if path not in self.files:
                return self.result(cmd, 1, err="not found")
            return self.result(cmd, out=f"{len(self.files[path])}\n")
        if operation == "-checksum":
            path = self.clean(args[1])
            if path not in self.files:
                return self.result(cmd, 1, err="not found")
            if path in self.no_checksum:
                return self.result(cmd, out=f"{path}\tNONE\n")
            checksum = hashlib.sha256(self.files[path]).hexdigest()
            return self.result(cmd, out=f"{path} FAKE-SHA256 {checksum}\n")
        raise AssertionError(f"unexpected HDFS command: {args}")


@pytest.fixture
def config(tmp_path: Path) -> bridge_module.BridgeConfig:
    requests = tmp_path / "simulation-requests"
    results = tmp_path / "simulation-results"
    requests.mkdir()
    results.mkdir()
    return bridge_module.BridgeConfig(
        hadoop_home="/opt/hadoop",
        hadoop_conf_dir="/opt/hadoop/etc/hadoop",
        hdfs_bin=sys.executable,
        hdfs_requests_dir="hdfs:///quant-sandbox/simulation-requests",
        hdfs_results_dir="hdfs:///quant-sandbox/simulation-results",
        local_requests_dir=requests,
        local_results_dir=results,
        state_dir=tmp_path / "state",
        log_path=tmp_path / "bridge.log",
        stable_seconds=0,
        retention_days=7,
        min_free_gb=10,
        resume_free_gb=12,
        stuck_seconds=10800,
        cleanup_enabled=False,
        hdfs_retention_days=30,
        hdfs_cleanup_mode="disabled",
    )


@pytest.fixture
def fake_hdfs(config: bridge_module.BridgeConfig) -> FakeHdfs:
    fake = FakeHdfs()
    fake.add_dir(config.hdfs_requests_dir)
    fake.add_dir(config.hdfs_results_dir)
    fake.add_dir(config.hdfs_tmp_results_dir)
    return fake


def make_bridge(config, fake_hdfs, *, now=2_000_000_000, free_gb=20):
    for path in (
        config.request_state_dir,
        config.result_state_dir,
        config.job_state_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)
    disk_usage_fn = (
        free_gb
        if callable(free_gb)
        else lambda _path: SimpleNamespace(free=free_gb * 1024**3)
    )
    return bridge_module.Bridge(
        config,
        runner=fake_hdfs,
        now_fn=lambda: now,
        disk_usage_fn=disk_usage_fn,
    )


def add_request(fake_hdfs: FakeHdfs, config, job="job-1", content="purpose = backtest\n"):
    path = f"{config.hdfs_requests_dir}/{job}.request"
    fake_hdfs.add_file(path, content)
    return path


def write_request_marker(bridge, job="job-1", *, copied_at="1970-01-12T13:46:40+00:00"):
    marker = {
        "remote": f"{bridge.config.hdfs_requests_dir}/{job}.request",
        "local": str(bridge.config.local_requests_dir / f"{job}.request"),
        "job_name": job,
        "status": "copied_to_local",
        "copied_at": copied_at,
    }
    bridge.atomic_json(bridge.request_marker_path(f"{job}.request"), marker)
    return marker


def test_config_defaults_use_default_filesystem_paths(monkeypatch):
    keys = [key for key in os.environ if key.startswith("HDFS_BRIDGE_")]
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("HDFS_SIMULATION_REQUESTS_DIR", raising=False)
    monkeypatch.delenv("HDFS_SIMULATION_RESULTS_DIR", raising=False)

    config = bridge_module.BridgeConfig.from_env()

    assert config.hdfs_requests_dir == "hdfs:///quant-sandbox/simulation-requests"
    assert config.hdfs_results_dir == "hdfs:///quant-sandbox/simulation-results"
    assert config.hdfs_retention_days == 30
    assert config.hdfs_cleanup_mode == "disabled"


def test_config_accepts_decimal_disk_thresholds(monkeypatch):
    monkeypatch.setenv("HDFS_BRIDGE_MIN_FREE_GB", "5")
    monkeypatch.setenv("HDFS_BRIDGE_RESUME_FREE_GB", "7.5")
    monkeypatch.setenv("HDFS_BRIDGE_HDFS_CLEANUP_MODE", "report")

    config = bridge_module.BridgeConfig.from_env()

    assert config.min_free_gb == 5.0
    assert config.resume_free_gb == 7.5
    assert config.hdfs_cleanup_mode == "report"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("HDFS_BRIDGE_MIN_FREE_GB", "0"),
        ("HDFS_BRIDGE_RESUME_FREE_GB", "not-a-number"),
        ("HDFS_BRIDGE_HDFS_CLEANUP_MODE", "yes"),
    ],
)
def test_config_rejects_invalid_retention_settings(monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError):
        bridge_module.BridgeConfig.from_env()


def test_hdfs_uri_validation_preserves_explicit_authority(monkeypatch):
    monkeypatch.setenv("HDFS_SIMULATION_REQUESTS_DIR", "hdfs://bialobog:8020/requests/")
    monkeypatch.setenv("HDFS_SIMULATION_RESULTS_DIR", "hdfs://bialobog:8020/results/")

    config = bridge_module.BridgeConfig.from_env()

    assert config.hdfs_requests_dir == "hdfs://bialobog:8020/requests"
    assert config.hdfs_results_dir == "hdfs://bialobog:8020/results"


def test_check_is_non_mutating(config, fake_hdfs):
    bridge = bridge_module.Bridge(
        config,
        runner=fake_hdfs,
        now_fn=lambda: 1,
        disk_usage_fn=lambda _path: SimpleNamespace(free=20 * 1024**3),
    )

    result = bridge.check()

    assert result["healthy"] is True, result
    assert not config.state_dir.exists()
    assert all(command[0] == "-test" for command in fake_hdfs.commands)


def test_check_fails_when_disk_is_below_intake_threshold(config, fake_hdfs):
    bridge = bridge_module.Bridge(
        config,
        runner=fake_hdfs,
        now_fn=lambda: 1,
        disk_usage_fn=lambda _path: SimpleNamespace(free=6 * 1024**3),
    )

    result = bridge.check()

    assert result["healthy"] is False
    assert result["intake_would_pause"] is True
    assert "below" in result["errors"][0]


def test_delivers_request_atomically_and_suppresses_duplicate(config, fake_hdfs):
    bridge = make_bridge(config, fake_hdfs)
    add_request(fake_hdfs, config)
    fake_hdfs.add_file(f"{config.hdfs_requests_dir}/.ignored.request", "hidden")

    bridge.deliver_requests()
    bridge.deliver_requests()

    local = config.local_requests_dir / "job-1.request"
    assert local.read_text() == "purpose = backtest\n"
    assert not list(config.local_requests_dir.glob(".*.tmp.*"))
    marker = json.loads(bridge.request_marker_path("job-1.request").read_text())
    assert marker["status"] == "copied_to_local"
    assert bridge.read_json(bridge.job_path("job-1"))["status"] == "delivered"
    assert [cmd[0] for cmd in fake_hdfs.commands].count("-cat") == 1


def test_invalid_job_name_is_not_delivered(config, fake_hdfs):
    bridge = make_bridge(config, fake_hdfs)
    fake_hdfs.add_file(f"{config.hdfs_requests_dir}/bad.name.request", "request")

    bridge.deliver_requests()

    assert not (config.local_requests_dir / "bad.name.request").exists()
    assert "invalid HDFS request job name" in config.log_path.read_text()


def test_legacy_marker_is_compatible_and_records_consumption(config, fake_hdfs):
    bridge = make_bridge(config, fake_hdfs)
    add_request(fake_hdfs, config)
    write_request_marker(bridge)

    bridge.deliver_requests()

    assert bridge.read_json(bridge.job_path("job-1"))["status"] == "consumed"
    assert not any(command[0] == "-cat" for command in fake_hdfs.commands)


def test_incomplete_result_is_not_published(config, fake_hdfs):
    bridge = make_bridge(config, fake_hdfs)
    write_request_marker(bridge)
    result = config.local_results_dir / "job-1"
    result.mkdir()
    (result / "partial.log").write_text("running")

    bridge.reconcile_and_publish()

    assert f"{config.hdfs_results_dir}/job-1" not in fake_hdfs.dirs


def test_result_waits_for_stability_window(config, fake_hdfs):
    config = replace(config, stable_seconds=60)
    now = 2_000_000_000
    bridge = make_bridge(config, fake_hdfs, now=now)
    write_request_marker(bridge)
    result = config.local_results_dir / "job-1"
    result.mkdir()
    table = result / "resultsTable.csv"
    table.write_text("SHARPE\n1.0\n")
    os.utime(result, (now - 30, now - 30))
    os.utime(table, (now - 30, now - 30))

    bridge.reconcile_and_publish()

    assert f"{config.hdfs_results_dir}/job-1" not in fake_hdfs.dirs


def test_completed_result_is_published_and_verified(config, fake_hdfs):
    bridge = make_bridge(config, fake_hdfs)
    write_request_marker(bridge)
    result = config.local_results_dir / "job-1"
    result.mkdir()
    (result / "resultsTable.csv").write_text("SHARPE\n1.0\n")
    (result / "job.log").write_text("ok")

    bridge.reconcile_and_publish()

    final = f"{config.hdfs_results_dir}/job-1"
    assert fake_hdfs.files[f"{final}/resultsTable.csv"] == b"SHARPE\n1.0\n"
    marker = bridge.read_json(bridge.result_marker_path("job-1"))
    assert marker["verified"] is True
    assert marker["terminal_relative_path"] == "resultsTable.csv"
    assert marker["terminal_size"] == len(b"SHARPE\n1.0\n")
    assert marker["terminal_checksum"].startswith("FAKE-SHA256:")
    assert bridge.read_json(bridge.job_path("job-1"))["status"] == "published"


def test_rejection_file_is_published(config, fake_hdfs):
    bridge = make_bridge(config, fake_hdfs)
    write_request_marker(bridge)
    rejection = config.local_results_dir / "job-1"
    rejection.write_text("rejected: invalid request")

    bridge.reconcile_and_publish()

    final = f"{config.hdfs_results_dir}/job-1"
    assert fake_hdfs.files[final] == b"rejected: invalid request"
    assert bridge.read_json(bridge.result_marker_path("job-1"))["kind"] == "rejected"


def test_result_without_hdfs_checksum_is_published_and_safely_retained(
    config, fake_hdfs
):
    now = 2_000_000_000
    config = replace(
        config,
        cleanup_enabled=True,
        retention_days=1,
        hdfs_cleanup_mode="delete",
        hdfs_retention_days=30,
    )
    bridge = make_bridge(config, fake_hdfs, now=now)
    rejection = config.local_results_dir / "job-none"
    rejection.write_text("rejected")
    final = f"{config.hdfs_results_dir}/job-none"
    fake_hdfs.no_checksum.add(final)

    bridge.publish_result("job-none", rejection, "rejected")

    marker_path = bridge.result_marker_path("job-none")
    marker = bridge.read_json(marker_path)
    assert marker["status"] == "published"
    assert marker["verified"] is True
    assert marker["terminal_checksum"] is None
    assert bridge.read_json(bridge.job_path("job-none"))["status"] == "published"

    marker["published_epoch"] = now - 31 * 86400
    bridge.atomic_json(marker_path, marker)
    os.utime(rejection, (now - 31 * 86400, now - 31 * 86400))

    bridge.cleanup_verified_results()
    bridge.cleanup_hdfs_results()

    assert not rejection.exists()
    assert final in fake_hdfs.files
    assert bridge.stats["local_retention_cleaned"] == 1
    assert bridge.stats["hdfs_cleanup_candidates"] == 0
    assert bridge.stats["hdfs_results_cleaned"] == 0


def test_collision_never_overwrites_existing_result(config, fake_hdfs):
    bridge = make_bridge(config, fake_hdfs)
    write_request_marker(bridge)
    rejection = config.local_results_dir / "job-1"
    rejection.write_text("new rejection")
    final = f"{config.hdfs_results_dir}/job-1"
    fake_hdfs.add_file(final, "different existing result")

    bridge.reconcile_and_publish()

    assert fake_hdfs.files[final] == b"different existing result"
    assert bridge.read_json(bridge.job_path("job-1"))["status"] == "collision"
    assert bridge.stats["errors"] == 1


def test_upload_failure_records_error_and_does_not_mark_published(config, fake_hdfs):
    bridge = make_bridge(config, fake_hdfs)
    write_request_marker(bridge)
    rejection = config.local_results_dir / "job-1"
    rejection.write_text("rejected")
    fake_hdfs.fail["-put"] = "token=do-not-log upload failed"

    bridge.reconcile_and_publish()

    assert not bridge.result_marker_path("job-1").exists()
    assert bridge.read_json(bridge.job_path("job-1"))["status"] == "error"
    log = config.log_path.read_text()
    assert "do-not-log" not in log
    assert "[REDACTED]" in log


def test_symlink_result_is_refused(config, fake_hdfs):
    bridge = make_bridge(config, fake_hdfs)
    write_request_marker(bridge)
    outside = config.local_results_dir.parent / "outside"
    outside.write_text("do not publish")
    (config.local_results_dir / "job-1").symlink_to(outside)

    bridge.reconcile_and_publish()

    assert bridge.read_json(bridge.job_path("job-1"))["status"] == "error"
    assert f"{config.hdfs_results_dir}/job-1" not in fake_hdfs.files


def test_low_disk_pauses_intake_but_publishes_existing_result(config, fake_hdfs):
    bridge = make_bridge(config, fake_hdfs, free_gb=6)
    write_request_marker(bridge, "finished")
    rejection = config.local_results_dir / "finished"
    rejection.write_text("rejected")
    add_request(fake_hdfs, config, "pending")

    bridge.run()

    assert f"{config.hdfs_results_dir}/finished" in fake_hdfs.files
    assert not (config.local_requests_dir / "pending.request").exists()
    heartbeat = bridge.read_json(config.heartbeat_path)
    assert heartbeat["intake_paused"] is True


def test_hysteresis_requires_resume_threshold(config, fake_hdfs):
    bridge = make_bridge(config, fake_hdfs, free_gb=11)
    bridge.atomic_json(config.heartbeat_path, {"intake_paused": True})
    add_request(fake_hdfs, config)

    bridge.run()

    assert not (config.local_requests_dir / "job-1.request").exists()


def test_cleanup_deletes_only_old_verified_matching_result(config, fake_hdfs):
    config = replace(config, cleanup_enabled=True, retention_days=7)
    now = 2_000_000_000
    bridge = make_bridge(config, fake_hdfs, now=now)
    result = config.local_results_dir / "job-1"
    result.write_text("rejected")
    os.utime(result, (now - 8 * 86400, now - 8 * 86400))
    final = f"{config.hdfs_results_dir}/job-1"
    fake_hdfs.add_file(final, "rejected")
    bridge.atomic_json(
        bridge.result_marker_path("job-1"),
        {
            "job_name": "job-1",
            "local_result": str(result),
            "hdfs_result": final,
            "kind": "rejected",
            "status": "published",
            "verified": True,
        },
    )
    unverified = config.local_results_dir / "job-2"
    unverified.write_text("keep")
    os.utime(unverified, (now - 8 * 86400, now - 8 * 86400))

    bridge.cleanup_verified_results()

    assert not result.exists()
    assert unverified.exists()


def test_cleanup_preserves_result_when_hdfs_verification_changes(config, fake_hdfs):
    config = replace(config, cleanup_enabled=True, retention_days=7)
    now = 2_000_000_000
    bridge = make_bridge(config, fake_hdfs, now=now)
    result = config.local_results_dir / "job-1"
    result.write_text("local rejection")
    os.utime(result, (now - 8 * 86400, now - 8 * 86400))
    final = f"{config.hdfs_results_dir}/job-1"
    fake_hdfs.add_file(final, "different")
    bridge.atomic_json(
        bridge.result_marker_path("job-1"),
        {
            "job_name": "job-1",
            "local_result": str(result),
            "hdfs_result": final,
            "kind": "rejected",
            "status": "published",
            "verified": True,
        },
    )

    bridge.cleanup_verified_results()

    assert result.exists()
    assert bridge.read_json(bridge.job_path("job-1"))["status"] == "error"


def test_cleanup_preserves_same_size_locally_modified_result(config, fake_hdfs):
    config = replace(config, cleanup_enabled=True, retention_days=1)
    now = 2_000_000_000
    bridge = make_bridge(config, fake_hdfs, now=now)
    result = config.local_results_dir / "job-1"
    result.write_text("reject1")
    bridge.publish_result("job-1", result, "rejected")
    marker_path = bridge.result_marker_path("job-1")
    marker = bridge.read_json(marker_path)
    marker["published_epoch"] = now - 2 * 86400
    bridge.atomic_json(marker_path, marker)
    result.write_text("reject2")
    os.utime(result, (now - 2 * 86400, now - 2 * 86400))

    bridge.cleanup_verified_results()

    assert result.exists()
    assert bridge.stats["errors"] == 1


def test_cleanup_retention_starts_no_earlier_than_publication(config, fake_hdfs):
    config = replace(config, cleanup_enabled=True, retention_days=7)
    now = 2_000_000_000
    bridge = make_bridge(config, fake_hdfs, now=now)
    result = config.local_results_dir / "job-1"
    result.write_text("rejected")
    os.utime(result, (now - 8 * 86400, now - 8 * 86400))
    final = f"{config.hdfs_results_dir}/job-1"
    fake_hdfs.add_file(final, "rejected")
    bridge.atomic_json(
        bridge.result_marker_path("job-1"),
        {
            "job_name": "job-1",
            "local_result": str(result),
            "hdfs_result": final,
            "kind": "rejected",
            "status": "published",
            "verified": True,
            "published_epoch": now - 86400,
        },
    )

    bridge.cleanup_verified_results()

    assert result.exists()


def add_verified_rejection(bridge, fake_hdfs, job, *, published_epoch):
    local = bridge.config.local_results_dir / job
    local.write_text(f"rejected {job}")
    remote = f"{bridge.config.hdfs_results_dir}/{job}"
    fake_hdfs.add_file(remote, local.read_bytes())
    checksum = hashlib.sha256(local.read_bytes()).hexdigest()
    marker = {
        "job_name": job,
        "local_result": str(local),
        "hdfs_result": remote,
        "kind": "rejected",
        "status": "published",
        "verified": True,
        "published_epoch": published_epoch,
        "terminal_relative_path": ".",
        "terminal_size": local.stat().st_size,
        "terminal_checksum": f"FAKE-SHA256:{checksum}",
    }
    bridge.atomic_json(bridge.result_marker_path(job), marker)
    return local, remote


def test_disk_pressure_deletes_oldest_until_resume_target(config, fake_hdfs):
    config = replace(
        config,
        cleanup_enabled=True,
        min_free_gb=5.0,
        resume_free_gb=7.5,
    )
    readings = iter([4.0, 6.0, 8.0])
    last = [4.0]

    def disk_usage(_path):
        last[0] = next(readings, last[0])
        return SimpleNamespace(free=last[0] * 1024**3)

    bridge = make_bridge(config, fake_hdfs, free_gb=disk_usage)
    oldest, _ = add_verified_rejection(
        bridge, fake_hdfs, "job-old", published_epoch=1_000
    )
    newer, _ = add_verified_rejection(
        bridge, fake_hdfs, "job-new", published_epoch=2_000
    )

    bridge.cleanup_for_disk_pressure()

    assert not oldest.exists()
    assert not newer.exists()
    assert bridge.stats["local_emergency_cleaned"] == 2
    assert bridge.read_json(bridge.result_marker_path("job-old"))[
        "local_cleanup_reason"
    ] == "disk_pressure"


def test_disk_pressure_stops_after_reaching_resume_target(config, fake_hdfs):
    config = replace(
        config,
        cleanup_enabled=True,
        min_free_gb=5.0,
        resume_free_gb=7.5,
    )
    readings = iter([4.0, 8.0])
    last = [4.0]

    def disk_usage(_path):
        last[0] = next(readings, last[0])
        return SimpleNamespace(free=last[0] * 1024**3)

    bridge = make_bridge(config, fake_hdfs, free_gb=disk_usage)
    oldest, _ = add_verified_rejection(
        bridge, fake_hdfs, "job-old", published_epoch=1_000
    )
    newer, _ = add_verified_rejection(
        bridge, fake_hdfs, "job-new", published_epoch=2_000
    )

    bridge.cleanup_for_disk_pressure()

    assert not oldest.exists()
    assert newer.exists()
    assert bridge.stats["local_emergency_cleaned"] == 1


def test_hdfs_retention_report_does_not_delete(config, fake_hdfs):
    now = 2_000_000_000
    config = replace(config, hdfs_cleanup_mode="report", hdfs_retention_days=30)
    bridge = make_bridge(config, fake_hdfs, now=now)
    _, remote = add_verified_rejection(
        bridge, fake_hdfs, "job-old", published_epoch=now - 31 * 86400
    )

    bridge.cleanup_hdfs_results()

    assert remote in fake_hdfs.files
    assert bridge.stats["hdfs_cleanup_candidates"] == 1
    assert bridge.stats["hdfs_results_cleaned"] == 0


def test_hdfs_retention_deletes_only_verified_managed_result(config, fake_hdfs):
    now = 2_000_000_000
    config = replace(config, hdfs_cleanup_mode="delete", hdfs_retention_days=30)
    bridge = make_bridge(config, fake_hdfs, now=now)
    _, remote = add_verified_rejection(
        bridge, fake_hdfs, "job-old", published_epoch=now - 31 * 86400
    )

    bridge.cleanup_hdfs_results()

    assert remote not in fake_hdfs.files
    marker = bridge.read_json(bridge.result_marker_path("job-old"))
    assert marker["hdfs_cleaned_epoch"] == now
    assert bridge.stats["hdfs_results_cleaned"] == 1


def test_hdfs_retention_preserves_young_legacy_and_unmanaged_results(config, fake_hdfs):
    now = 2_000_000_000
    config = replace(config, hdfs_cleanup_mode="delete", hdfs_retention_days=30)
    bridge = make_bridge(config, fake_hdfs, now=now)
    _, young_remote = add_verified_rejection(
        bridge, fake_hdfs, "job-young", published_epoch=now - 29 * 86400
    )
    legacy_remote = f"{config.hdfs_results_dir}/job-legacy"
    fake_hdfs.add_file(legacy_remote, "legacy")
    bridge.atomic_json(
        bridge.result_marker_path("job-legacy"),
        {
            "job_name": "job-legacy",
            "hdfs_result": legacy_remote,
            "status": "published",
            "verified": True,
            "published_epoch": now - 31 * 86400,
        },
    )
    unmanaged_remote = f"{config.hdfs_results_dir}/external"
    fake_hdfs.add_file(unmanaged_remote, "external")

    bridge.cleanup_hdfs_results()

    assert young_remote in fake_hdfs.files
    assert legacy_remote in fake_hdfs.files
    assert unmanaged_remote in fake_hdfs.files
    assert bridge.stats["hdfs_results_cleaned"] == 0


def test_hdfs_retention_preserves_changed_result(config, fake_hdfs):
    now = 2_000_000_000
    config = replace(config, hdfs_cleanup_mode="delete", hdfs_retention_days=30)
    bridge = make_bridge(config, fake_hdfs, now=now)
    _, remote = add_verified_rejection(
        bridge, fake_hdfs, "job-old", published_epoch=now - 31 * 86400
    )
    fake_hdfs.add_file(remote, "changed after publication")

    bridge.cleanup_hdfs_results()

    assert remote in fake_hdfs.files
    assert bridge.stats["errors"] == 1
    assert bridge.stats["hdfs_results_cleaned"] == 0


def test_timeout_does_not_prevent_late_result_publication(config, fake_hdfs):
    bridge = make_bridge(config, fake_hdfs, now=2_000_000_000)
    write_request_marker(bridge, copied_at="1970-01-01T00:00:00+00:00")

    bridge.reconcile_and_publish()
    assert bridge.read_json(bridge.job_path("job-1"))["status"] == "timed_out"

    rejection = config.local_results_dir / "job-1"
    rejection.write_text("late rejection")
    bridge.reconcile_and_publish()
    assert bridge.read_json(bridge.job_path("job-1"))["status"] == "published"


def test_errors_are_sanitized():
    value = bridge_module.sanitize(
        "password=hunter2 token:abc https://user:pass@example.test/path\nnext"
    )
    assert "hunter2" not in value
    assert "abc" not in value
    assert "user:pass" not in value
    assert "\n" not in value


def test_lock_contention_skips_without_running(config, monkeypatch):
    config.state_dir.mkdir(parents=True)
    monkeypatch.setattr(bridge_module.BridgeConfig, "from_env", classmethod(lambda cls: config))
    with config.lock_path.open("w", encoding="utf-8") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert bridge_module.main([]) == 0
    assert "another bridge instance" in config.log_path.read_text()
