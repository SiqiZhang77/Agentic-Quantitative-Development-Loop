"""``submit_backtest`` — build a complete, engine-valid ``.request`` from a
proven baseline + caller overrides, stage it, and deploy it for the engine.

Design (RAE-09 / supervisor review):

* **Byte-perfect immutability.** The baseline template is treated as immutable
  text; :func:`build_request` only swaps the *value* of keys explicitly passed
  in ``overrides`` and preserves every other byte (trailing tabs, ``\\u0020``
  key escapes, comments, blank lines). It never dict-reserialises the file.
* **Type coercion.** An override value is written to match the baseline's own
  representation for that key (bare int / decimal / string / lowercase bool).
* **Permissive ``extra_params``.** Passed straight through; never validated
  against a whitelist, never fails the run — forward-compatible with new
  engine parameters.
* **Swappable delivery.** Toggled by ``USE_REAL_BACKTESTER`` (``false`` -> a
  local mock dir that keeps the offline loop working for other sub-teams;
  ``true`` -> explicit request/result dirs mounted into the sandbox for the
  cluster watcher). SSH/SFTP transport is a later adapter.

stdlib only.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from collections.abc import Iterable
from pathlib import Path
from typing import Any

_THIS_DIR = Path(__file__).resolve().parent
DEFAULT_BASELINE = _THIS_DIR / "templates" / "value_growth_factor.request"

JOB_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# A ``KEY = value`` line, capturing the surrounding format so it can be rebuilt
# byte-identically except for the value:
#   lead | key | sep(= with surrounding ws) | value | trailing ws
_LINE_RE = re.compile(
    r"^(?P<lead>\s*)(?P<key>[^=\s][^=]*?)(?P<sep>\s*=\s*)(?P<val>.*?)(?P<trail>\s*)$"
)

_BRANCH_KEYS = (
    "atrade_sifting_commons", "atrade_sifting_pretrade", "atrade_sifting_portfolio",
    "atrade_sifting_hedge", "atrade_sifting_model", "atrade_sifting_config",
)


def _coerce(value: Any, baseline_value: str | None = None) -> str:
    """Format *value* to match the engine's representation for this key.

    Booleans -> lowercase ``true``/``false``. If the baseline stores the key as
    a bare integer or decimal, cast accordingly so we never introduce quotes,
    stray decimals, or Python's ``True``/``False``.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    kind = None
    if baseline_value is not None:
        bv = baseline_value.strip()
        if re.fullmatch(r"-?\d+", bv):
            kind = "int"
        elif re.fullmatch(r"-?\d*\.\d+", bv):
            kind = "float"
    try:
        if kind == "int":
            return str(int(float(value)))
        if kind == "float":
            return str(float(value))
    except (TypeError, ValueError):
        # Override isn't numeric after all (e.g. a typo'd extra_params value for
        # a baseline-numeric key). Pass it through as text rather than crashing
        # the whole submission; the engine will surface the bad value.
        return str(value)
    return str(value)


def build_request(
    template_text: str,
    overrides: dict[str, Any],
    remove_keys: Iterable[str] | None = None,
) -> str:
    """Return *template_text* with the values of ``overrides`` keys replaced and
    the keys in ``remove_keys`` deleted (their whole line is dropped).

    Surgical, line-level: only matched lines change; every other byte is
    preserved. Keys in ``overrides`` not present in the template are appended.
    ``build_request(t, {})`` is byte-identical to *t*. A key in both
    ``overrides`` and ``remove_keys`` is removed (removal wins).
    """
    remove = set(remove_keys or ())
    out: list[str] = []
    seen: set[str] = set()
    for raw in template_text.splitlines(keepends=True):
        body, nl = raw, ""
        if body.endswith("\r\n"):
            body, nl = body[:-2], "\r\n"
        elif body.endswith("\n"):
            body, nl = body[:-1], "\n"
        if body.lstrip().startswith("#") or "=" not in body:
            out.append(raw)
            continue
        m = _LINE_RE.match(body)
        if not m:
            out.append(raw)
            continue
        key = m.group("key").strip()
        if key in remove:                 # delete this key: drop the whole line
            seen.add(key)
            continue
        if key not in overrides:
            out.append(raw)
            continue
        new_val = _coerce(overrides[key], m.group("val"))
        out.append(f"{m.group('lead')}{m.group('key')}{m.group('sep')}{new_val}{m.group('trail')}{nl}")
        seen.add(key)
    appended = [k for k in overrides if k not in seen and k not in remove]
    if appended:
        if out and not out[-1].endswith("\n"):
            out.append("\n")
        for k in appended:
            out.append(f"{k}={_coerce(overrides[k])}\n")
    return "".join(out)


def _baseline_value(template_text: str, key: str) -> str | None:
    for line in template_text.splitlines():
        if line.lstrip().startswith("#") or "=" not in line:
            continue
        m = _LINE_RE.match(line)
        if m and m.group("key").strip() == key:
            return m.group("val")
    return None


def _template_keys(template_text: str) -> set[str]:
    """All ``KEY`` names defined in *template_text* (same line rules as
    :func:`build_request`). Used to flag override keys that are absent from the
    baseline — i.e. keys that get appended rather than replacing an existing
    line, which is the signature of a mistyped parameter."""
    keys: set[str] = set()
    for line in template_text.splitlines():
        if line.lstrip().startswith("#") or "=" not in line:
            continue
        m = _LINE_RE.match(line)
        if m:
            keys.add(m.group("key").strip())
    return keys


# --- backtest-window guard ------------------------------------------------
#
# The engine needs a warmup of max(NDELAY, LTU_WINDOW_SIZE) *trading* days before
# it holds any position. A window shorter than that warmup (plus a usable
# simulation span) produces an all-zero resultsTable — zero ZQQ points, zero
# positions — that then silently drives the iterate loop as if it were a real
# result. We fail fast instead of shipping a degenerate run.
_TRADING_DAYS_PER_YEAR = 252
_CALENDAR_DAYS_PER_YEAR = 365.25
_DEFAULT_MIN_SIM_TRADING_DAYS = 252  # ~1y of actual simulation after warmup


def _effective_value(key: str, overrides: dict[str, Any], template_text: str) -> Any:
    """The value the engine will actually see for *key*: an explicit override wins,
    otherwise the baseline's own value (or ``None`` if the key is absent)."""
    if key in overrides and overrides[key] is not None:
        return overrides[key]
    return _baseline_value(template_text, key)


def _parse_date(value: Any) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        return None


def _parse_int(value: Any) -> int | None:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _min_sim_trading_days() -> int:
    try:
        return max(0, int(os.getenv("BACKTEST_MIN_SIM_TRADING_DAYS",
                                    str(_DEFAULT_MIN_SIM_TRADING_DAYS))))
    except ValueError:
        return _DEFAULT_MIN_SIM_TRADING_DAYS


def _window_guard_error(overrides: dict[str, Any], template_text: str) -> str | None:
    """Return an error message if the *effective* backtest window can't clear the
    engine warmup, else ``None``.

    Evaluates the final STARTDATE/ENDDATE/NDELAY/LTU_WINDOW_SIZE (override else
    baseline), so a request that only inherits the baseline's long window is never
    flagged. Unparseable or missing dates skip the check rather than guess.
    """
    start = _parse_date(_effective_value("STARTDATE", overrides, template_text))
    end = _parse_date(_effective_value("ENDDATE", overrides, template_text))
    if start is None or end is None:
        return None
    span_days = (end - start).days
    if span_days <= 0:
        return (f"backtest window is empty or inverted (STARTDATE={start}, "
                f"ENDDATE={end}); ENDDATE must be after STARTDATE")
    ndelay = _parse_int(_effective_value("NDELAY", overrides, template_text)) or 0
    ltu = _parse_int(_effective_value("LTU_WINDOW_SIZE", overrides, template_text)) or 0
    warmup_td = max(ndelay, ltu)
    if warmup_td <= 0:
        return None  # no warmup configured -> nothing to clear
    min_sim_td = _min_sim_trading_days()
    required_td = warmup_td + min_sim_td
    approx_td = span_days * (_TRADING_DAYS_PER_YEAR / _CALENDAR_DAYS_PER_YEAR)
    if approx_td >= required_td:
        return None
    required_cal_days = int(required_td * (_CALENDAR_DAYS_PER_YEAR / _TRADING_DAYS_PER_YEAR))
    suggested_end = start + timedelta(days=required_cal_days)
    return (
        f"backtest window {start}..{end} (~{approx_td:.0f} trading days) is too short "
        f"to clear the engine warmup of {warmup_td} trading days "
        f"(NDELAY={ndelay}, LTU_WINDOW_SIZE={ltu}) plus {min_sim_td} simulation days. "
        f"Use a span of at least ~{required_td / _TRADING_DAYS_PER_YEAR:.1f} years — "
        f"from STARTDATE {start}, an ENDDATE on or after {suggested_end}. A shorter "
        "window produces an all-zero result (no positions are ever held)."
    )


_VERSION_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(.+)_([^_]+)$")


def _expand_master_indices(
    version: str,
    template_text: str,
    *,
    universe: str | None = None,
    short_stock_defs: str | None = None,
    short_indice_defs: str | None = None,
) -> dict[str, str]:
    """Expand a data-pool *version* into the ``runner_*`` overrides.

    ``2026-06-08_2.9.0-RELEASE_pyspark`` ->
      runner_dataURL         = 2026-06-08_pyspark/2.9.0-RELEASE
      runner_longStockDefs   = /opt/.../master-indices_<version>/<universe>
      runner_shortStockDefs  = /opt/.../master-indices_<version>/<short_stock_defs>
      runner_shortIndiceDefs = /opt/.../master-indices_<version>/<short_indice_defs>

    The three stock-definition *filenames* are supplied by the caller (the LLM
    picks them from ``list_master_indices``, which reports the real variants in
    the chosen version) and **all three are handled identically**: a name that
    is supplied is used verbatim under the version directory; a name left out
    falls back to the baseline's filename for that key, so an unknown name is
    never invented. Unknown version shapes return ``{}`` (we do not guess the
    directory layout).
    """
    m = _VERSION_RE.match(version)
    if not m:
        return {}
    date, release, variant = m.groups()
    conf = f"/opt/simulations-service/conf/master-indices_{date}_{release}_{variant}"
    out = {"runner_dataURL": f"{date}_{variant}/{release}"}
    supplied = {
        "runner_longStockDefs": universe,
        "runner_shortStockDefs": short_stock_defs,
        "runner_shortIndiceDefs": short_indice_defs,
    }
    for key, name in supplied.items():
        if not name:  # caller didn't pick one -> reuse the baseline's filename
            bv = _baseline_value(template_text, key)
            name = bv.rsplit("/", 1)[-1] if bv else key.replace("runner_", "") + ".txt"
        out[key] = f"{conf}/{name}"
    return out


_DEFAULT_MASTER_INDICES_CONF_DIR = "/opt/simulations-service/conf"


def _master_indices_warnings(
    version: str,
    *,
    universe: str | None = None,
    short_stock_defs: str | None = None,
    short_indice_defs: str | None = None,
    conf_dir: str | Path | None = None,
) -> list[str]:
    """Report (non-blocking) if a requested master_indices *version* — or a
    stock-definition file named for it — is not present in the data store.

    Mirrors the store that :func:`list_master_indices` scans. If the store
    itself is unreachable (e.g. running off-cluster / in mock mode) we cannot
    verify existence, so we stay silent rather than raise a false alarm — the
    check only reports when it can actually see the store and the version is
    genuinely absent.
    """
    conf = Path(conf_dir or os.getenv("MASTER_INDICES_CONF_DIR",
                                      _DEFAULT_MASTER_INDICES_CONF_DIR)).expanduser()
    if not conf.is_dir():
        return []  # data store not reachable -> cannot verify, no false alarm
    version_dir = conf / f"master-indices_{version}"
    if not version_dir.is_dir():
        return [f"master_indices version {version!r} was not found in the data store "
                f"at {conf}; call list_master_indices to see the available versions"]
    try:
        present = {p.name for p in version_dir.iterdir()}
    except OSError:
        return []
    warns: list[str] = []
    for label, name in (("universe", universe),
                        ("short_stock_defs", short_stock_defs),
                        ("short_indice_defs", short_indice_defs)):
        if name and name not in present:
            warns.append(f"{label} file {name!r} was not found in master_indices version "
                         f"{version!r}; call list_master_indices to see the available files")
    return warns


# --- delivery (swappable transport) ---------------------------------------

def _use_real() -> bool:
    return os.getenv("USE_REAL_BACKTESTER", "false").strip().lower() == "true"


def _absolute_dir_from_env(name: str) -> Path:
    raw = os.getenv(name)
    if not raw or not raw.strip():
        raise ValueError(f"{name} must be set to an explicit absolute path in real backtester mode")
    raw = raw.strip()
    if raw.startswith("~"):
        raise ValueError(f"{name} must be an explicit absolute path; '~' expansion is not allowed")
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError(f"{name} must be an explicit absolute path, got {raw!r}")
    return path


def _validate_writable_dir(path: Path, *, label: str) -> None:
    if not path.exists():
        raise OSError(f"{label} does not exist: {path}")
    if not path.is_dir():
        raise OSError(f"{label} is not a directory: {path}")
    probe = path / f".write-test-{os.getpid()}"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise OSError(f"{label} is not writable: {path}: {exc}") from exc


def _validate_readable_dir(path: Path, *, label: str) -> None:
    if not path.exists():
        raise OSError(f"{label} does not exist: {path}")
    if not path.is_dir():
        raise OSError(f"{label} is not a directory: {path}")
    try:
        list(path.iterdir())
    except OSError as exc:
        raise OSError(f"{label} is not readable: {path}: {exc}") from exc


class LocalDirDelivery:
    """Write the ``.request`` into a directory. Serves both the local mock dir
    and the real watched dir (a local path on the cluster/container). A future
    ``RealDelivery`` can add SSH/SFTP for off-cluster submission."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    def deliver(self, relative_path: str, content: str) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        # submit runs inside the cluster sandbox and writes the request straight
        # into the watched simulation-requests tree, so the model-builder's
        # watcher must never catch a half-written file. Write a hidden temp file
        # in the same dir, then os.replace it in (atomic rename within one
        # filesystem).
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise
        return path


def get_transport() -> LocalDirDelivery:
    if _use_real():
        target = _absolute_dir_from_env("SIMULATION_REQUESTS_DIR")
        _validate_writable_dir(target, label="SIMULATION_REQUESTS_DIR")
    else:
        target = Path(os.getenv("BACKTEST_REQUESTS_DIR", "mock_runtime/backtest-requests"))
    return LocalDirDelivery(target)


def results_root() -> Path:
    """Directory the engine writes results into — the mirror of the requests dir.

    Real (``USE_REAL_BACKTESTER=true``): the explicit absolute
    ``SIMULATION_RESULTS_DIR`` mounted into the sandbox, where the engine drops
    one directory per job. Mock: ``MOCK_RUNTIME_DIR/simulation-results``.

    Single source of truth so status polling and result-reading resolve the same
    location the requests dir was paired with (they must not silently fall back to
    the mock dir when submitting for real)."""
    if _use_real():
        root = _absolute_dir_from_env("SIMULATION_RESULTS_DIR")
        _validate_readable_dir(root, label="SIMULATION_RESULTS_DIR")
        return root
    return Path(os.getenv("MOCK_RUNTIME_DIR", "mock_runtime")) / "simulation-results"


# --- public tool -----------------------------------------------------------

def submit_backtest(
    job_name: str,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    atrade_sifting_commons: str | None = None,
    atrade_sifting_pretrade: str | None = None,
    atrade_sifting_portfolio: str | None = None,
    atrade_sifting_hedge: str | None = None,
    atrade_sifting_model: str | None = None,
    atrade_sifting_config: str | None = None,
    nport: int | None = None,
    nfreq: int | None = None,
    ndelay: int | None = None,
    slippage: float | None = None,
    initcash: int | None = None,
    value_active_rules: str | None = None,
    carry_active_rules: str | None = None,
    master_indices: str | None = None,
    universe: str | None = None,
    short_stock_defs: str | None = None,
    short_indice_defs: str | None = None,
    extra_params: dict[str, Any] | None = None,
    remove_keys: list[str] | None = None,
    dry_run: bool = False,
    baseline: str | Path | None = None,
) -> dict[str, Any]:
    """Build a complete ``.request`` from the baseline + the supplied overrides,
    then deliver it (unless ``dry_run``). Returns ``job_name``/``submitted_at``
    on success, or the generated request when ``dry_run`` is set."""
    if not isinstance(job_name, str) or not JOB_NAME_RE.fullmatch(job_name):
        return {"status": "failed",
                "error": f"invalid job_name {job_name!r}: use letters, digits, '_' or '-' only"}

    template_path = Path(baseline) if baseline else Path(
        os.getenv("BACKTEST_BASELINE", str(DEFAULT_BASELINE)))
    try:
        template = template_path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"status": "failed", "error": f"cannot read baseline {template_path}: {exc}"}

    overrides: dict[str, Any] = {}
    branch_vals = (atrade_sifting_commons, atrade_sifting_pretrade, atrade_sifting_portfolio,
                   atrade_sifting_hedge, atrade_sifting_model, atrade_sifting_config)
    for key, val in zip(_BRANCH_KEYS, branch_vals):
        if val is not None:
            overrides[key] = val
    scalar_vals = {
        "STARTDATE": start_date, "ENDDATE": end_date, "NPORT": nport, "NFREQ": nfreq,
        "NDELAY": ndelay, "SLIPPAGE": slippage, "INITCASH": initcash,
        "VALUE_ACTIVE_RULES": value_active_rules, "CARRY_ACTIVE_RULES": carry_active_rules,
    }
    for key, val in scalar_vals.items():
        if val is not None:
            overrides[key] = val
    if master_indices:
        overrides.update(_expand_master_indices(
            master_indices, template,
            universe=universe,
            short_stock_defs=short_stock_defs,
            short_indice_defs=short_indice_defs,
        ))
    if extra_params:
        for k, v in extra_params.items():        # permissive passthrough; no whitelist
            overrides[str(k)] = v

    content = build_request(template, overrides, remove_keys=remove_keys)

    # Permissive-but-not-silent: keys absent from the baseline are still written
    # (never rejected), but reported so a typo'd parameter is not lost quietly.
    known_keys = _template_keys(template)
    removed = set(remove_keys or ())
    warnings = [
        f"parameter {k!r} is not in the baseline and was added as a new key; "
        "check for a typo"
        for k in overrides if k not in known_keys and k not in removed
    ]
    # If a data version was requested, report when it (or a named stock-def file)
    # is absent from the store, so a missing/typo'd universe is caught here rather
    # than only being rejected by the engine later.
    if master_indices:
        warnings += _master_indices_warnings(
            master_indices, universe=universe,
            short_stock_defs=short_stock_defs, short_indice_defs=short_indice_defs)

    # A window too short to clear the engine warmup would make the real engine
    # produce an all-zero result that silently drives the iterate loop. Fail fast
    # for real submissions; in mock/dry_run there is no real engine (results are
    # canned), so only surface it as a non-blocking warning rather than block.
    window_error = _window_guard_error(overrides, template)

    if dry_run:
        if window_error:
            warnings = [*warnings, window_error]
        return {"status": "dry_run", "job_name": job_name,
                "overrides": sorted(overrides), "warnings": warnings,
                "request": content}

    if window_error:
        if _use_real():
            return {"status": "failed", "job_name": job_name,
                    "error": window_error, "warnings": warnings}
        warnings = [*warnings, window_error]  # mock: informative, non-blocking

    request_relative_path = f"{job_name}.request"
    try:
        transport = get_transport()
        request_root = transport.directory
        results_dir = results_root()
        expected_results_path = results_dir / job_name
        path = transport.deliver(request_relative_path, content)
        deployed_job_path = None
        deployed_request_path = None
    except (OSError, ValueError) as exc:
        return {"status": "failed",
                "error": f"delivery failed (could not write the request to the watched dir): {exc}"}
    if _use_real():
        print(
            "Backtest real handoff: "
            f"SIMULATION_REQUESTS_DIR={request_root}, "
            f"SIMULATION_RESULTS_DIR={results_dir}, "
            f"request_file={path}, "
            f"expected_results_path={expected_results_path}",
            file=sys.stderr,
        )
    return {"status": "submitted", "job_name": job_name,
            "mode": "real" if _use_real() else "mock",
            "request_file": str(path),
            "request_path": str(path),
            "expected_results_path": str(expected_results_path),
            "deployed_job_path": str(deployed_job_path) if deployed_job_path else None,
            "deployed_request_path": str(deployed_request_path) if deployed_request_path else None,
            "warnings": warnings,
            "submitted_at": datetime.now(timezone.utc).isoformat()}


if __name__ == "__main__":
    import json

    demo = submit_backtest("demo_job", start_date="2010-01-01", nport=50, dry_run=True)
    print(json.dumps({k: v for k, v in demo.items() if k != "request"}, indent=2))
    print("--- first 12 lines of generated request ---")
    print("\n".join(demo["request"].splitlines()[:12]))
