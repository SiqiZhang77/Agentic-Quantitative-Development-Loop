"""RAE-19: parallel-safety / statelessness tests.

Unit tests pin down run/iteration-scoped backtest job names; the integration
test runs two containers' worth of work (two run.py subprocesses, same ticket,
different run_id) CONCURRENTLY against one shared MOCK_RUNTIME_DIR — the
stand-in for the shared cluster home — and asserts they do not collide.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("fastmcp")  # mcp_client imports it; not in requirements-dev

from iteration_loop import run_iteration_loop
from mcp_client import _job_name_from

SANDBOX = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = SANDBOX if (SANDBOX / "mcp" / "server.py").is_file() else SANDBOX.parent
ENGINE_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")


# ------------------------- job-name scoping (unit) -------------------------

def test_job_names_distinct_across_runs_and_iterations():
    a = _job_name_from("SCRUM-46", "run-A", 1)
    b = _job_name_from("SCRUM-46", "run-B", 1)
    a2 = _job_name_from("SCRUM-46", "run-A", 2)
    assert len({a, b, a2}) == 3
    assert a.endswith("-i1") and a2.endswith("-i2")


def test_job_names_engine_safe():
    for name in (
        _job_name_from("SCRUM 46!", "run/1.0", 1),
        _job_name_from(None, None, 1),
        _job_name_from("", "", 3),
    ):
        assert ENGINE_NAME.match(name), name


def test_job_names_long_run_id_capped_but_unique():
    long_a = _job_name_from("SCRUM-46", "x" * 100 + "A", 1)
    long_b = _job_name_from("SCRUM-46", "x" * 100 + "B", 1)
    assert long_a != long_b  # hash suffix keeps them apart
    assert len(long_a) <= 75


def test_job_name_legacy_run_id_equals_ticket():
    # legacy env path defaults run_id to issue_key -> no duplicated prefix
    assert _job_name_from("CI-1", "CI-1", 1) == "CI-1-i1"
    assert _job_name_from("CI-1", None, 1) == "CI-1-i1"


# ------------------------- iteration plumbing (unit) -------------------------

def test_loop_passes_iteration_number_without_mutating_payload():
    seen = []

    def backtest_fn(p):
        seen.append(p["iteration"])
        return {"recommended_action": "iterate", "evaluation": {}}

    # command=backtest: iteration defaults on only for backtest runs.
    payload = {
        "issue_key": "T-1",
        "command": "backtest",
        "execution_objectives": {"max_iterations": 3},
    }
    run_iteration_loop(payload, lambda p: {}, backtest_fn)
    assert seen == [1, 2, 3]
    assert "iteration" not in payload  # caller's payload untouched


# ------------------------- concurrent runs (integration) -------------------------

# run.py insists on /workspace/output paths (schema) and emit_result would die on
# the unwritable local /workspace — so the driver redirects the result path AFTER
# schema validation, standing in for the container's /workspace tmpfs.
_DRIVER = """
import os, sys, run
_load = run.load_payload
def _patched():
    p = _load()
    rp = os.path.join(sys.argv[1], "result.json")
    p["result_path"] = rp
    p["output_paths"] = {**p["output_paths"], "result_path": rp,
                         "progress_events_path": None}
    return p
run.load_payload = _patched
sys.exit(run.main())
"""


def test_concurrent_same_ticket_runs_do_not_collide(tmp_path):
    fixture = json.loads((SANDBOX / "tests" / "fixtures" / "runtime_request_example.json").read_text())
    shared_runtime = tmp_path / "mock_runtime"  # the "shared cluster home"

    procs = {}
    for tag in ("A", "B"):
        payload = json.loads(json.dumps(fixture))
        payload["run_id"] = f"run_par_{tag}"
        payload["iteration_controls"] = {
            **payload["iteration_controls"],
            "max_iterations": 1,
            "timeout_seconds": 60,
        }
        out_dir = tmp_path / f"out_{tag}"
        out_dir.mkdir()
        env = {
            **os.environ,
            "RAE_OFFLINE": "1",
            # This is a mock-only concurrency test.  Pin the mode explicitly so
            # an outer shell or an earlier integration test cannot redirect the
            # child processes to the real backtester.
            "USE_REAL_BACKTESTER": "false",
            # source tree has proxy/ as a sibling of sandbox/ (the container
            # image has it at /app/proxy, already importable)
            "PYTHONPATH": str(RUNTIME_ROOT / "proxy"),
            # headless matplotlib: the macOS GUI backend crashes off the main
            # thread when the MCP server renders the equity curve (the
            # container is headless Linux anyway)
            "MPLBACKEND": "Agg",
            "MCP_SERVER_PATH": str(RUNTIME_ROOT / "mcp" / "server.py"),
            "MOCK_RUNTIME_DIR": str(shared_runtime),  # SHARED across both runs
            # Separate per run: the mock's process_all_pending watcher would race
            # on a shared requests dir (mock-only artefact; the real engine is a
            # single watcher daemon). The shared collision surface under test is
            # simulation-results/, which lives in the shared MOCK_RUNTIME_DIR.
            "BACKTEST_REQUESTS_DIR": str(tmp_path / f"requests_{tag}"),
            "OFFLINE_PUSH_DIR": str(out_dir),
        }
        env.pop("RAE_LEGACY_INPUT", None)
        p = subprocess.Popen(
            [sys.executable, "-c", _DRIVER, str(out_dir)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=SANDBOX, env=env, text=True,
        )
        procs[tag] = (p, json.dumps(payload), out_dir)

    # Both spawned before either is waited on -> genuinely concurrent.
    results = {}
    for tag, (p, stdin_data, out_dir) in procs.items():
        stdout, stderr = p.communicate(input=stdin_data, timeout=180)
        assert p.returncode == 0, (
            f"run {tag} failed:\n"
            f"stdout:\n{stdout[-4000:]}\n"
            f"stderr:\n{stderr[-4000:]}"
        )
        results[tag] = (json.loads(stdout.strip().splitlines()[-1]), out_dir)

    for tag, (resp, out_dir) in results.items():
        assert resp["execution_summary"]["status"] == "succeeded"
        assert resp["run_id"] == f"run_par_{tag}"
        assert json.loads((out_dir / "result.json").read_text())["run_id"] == f"run_par_{tag}"

    # The shared results dir holds one job dir per run — distinct, no overwrite.
    job_dirs = sorted(d.name for d in (shared_runtime / "simulation-results").iterdir() if d.is_dir())
    assert len(job_dirs) == 2, job_dirs
    for tag, expected in (("A", "run_par_A"), ("B", "run_par_B")):
        matches = [d for d in job_dirs if expected in d]
        assert len(matches) == 1 and matches[0].endswith("-i1"), job_dirs
        assert (shared_runtime / "simulation-results" / matches[0] / "resultsTable.csv").exists()
