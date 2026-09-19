"""submit_backtest non-blocking warnings (ported from mcp-new): a param absent
from the baseline is still written but surfaced, so a typo isn't lost silently.
This is what the backtest agent's prompt tells the LLM to relay to the user.
"""
from submit_backtest import submit_backtest, _master_indices_warnings


def test_unknown_override_key_warns_but_still_submits():
    out = submit_backtest("demo", extra_params={"NFRQ": "4"}, dry_run=True)  # typo of NFREQ
    assert any("NFRQ" in w and "typo" in w for w in out["warnings"])


def test_known_override_key_produces_no_warning():
    out = submit_backtest("demo", nport=50, extra_params={"NFREQ": "4"}, dry_run=True)
    assert out["warnings"] == []


def test_default_baseline_is_current_value_growth_pyspark(monkeypatch):
    monkeypatch.delenv("BACKTEST_BASELINE", raising=False)
    out = submit_backtest("demo", dry_run=True)

    assert out["status"] == "dry_run"
    assert "runner_dataURL   = 2026-06-07_pyspark/2.9.0-RELEASE" in out["request"]
    assert "master-indices_2026-06-07_2.9.0-RELEASE_pyspark" in out["request"]
    assert "VOLATILITY_EXPERTWGHT_" not in out["request"]
    assert "VOLATILITY_ACTIVE_RULES" not in out["request"]


def test_submitted_response_carries_warnings_key(tmp_path, monkeypatch):
    monkeypatch.setenv("USE_REAL_BACKTESTER", "false")
    monkeypatch.setenv("BACKTEST_REQUESTS_DIR", str(tmp_path / "requests"))
    out = submit_backtest("demo", extra_params={"BOGUS": "1"})
    assert out["status"] == "submitted"
    assert "warnings" in out and out["warnings"]


def test_real_submission_uses_workflow_scoped_request_path(tmp_path, monkeypatch):
    requests = tmp_path / "simulation-requests"
    results = tmp_path / "simulation-results"
    requests.mkdir()
    results.mkdir()
    monkeypatch.setenv("USE_REAL_BACKTESTER", "true")
    monkeypatch.setenv("SIMULATION_REQUESTS_DIR", str(requests))
    monkeypatch.setenv("SIMULATION_RESULTS_DIR", str(results))

    out = submit_backtest("workflow-1")

    assert out["status"] == "submitted"
    assert out["request_path"] == str(requests / "workflow-1.request")
    assert (requests / "workflow-1.request").is_file()
    assert out["deployed_job_path"] is None
    assert out["deployed_request_path"] is None
    assert out["expected_results_path"] == str(results / "workflow-1")


def test_real_submissions_for_distinct_workflows_do_not_overwrite(tmp_path, monkeypatch):
    requests = tmp_path / "simulation-requests"
    results = tmp_path / "simulation-results"
    requests.mkdir()
    results.mkdir()
    monkeypatch.setenv("USE_REAL_BACKTESTER", "true")
    monkeypatch.setenv("SIMULATION_REQUESTS_DIR", str(requests))
    monkeypatch.setenv("SIMULATION_RESULTS_DIR", str(results))

    first = submit_backtest("workflow-1", start_date="2020-01-01")
    second = submit_backtest("workflow-2", start_date="2021-01-01")

    assert first["status"] == "submitted"
    assert second["status"] == "submitted"
    assert first["request_path"] == str(requests / "workflow-1.request")
    assert second["request_path"] == str(requests / "workflow-2.request")
    assert (requests / "workflow-1.request").read_text(encoding="utf-8") != (
        requests / "workflow-2.request"
    ).read_text(encoding="utf-8")


def test_master_indices_warning_silent_when_store_unreachable(monkeypatch):
    # Off-cluster the conf dir doesn't exist -> cannot verify -> no false alarm.
    monkeypatch.setenv("MASTER_INDICES_CONF_DIR", "/no/such/conf/dir")
    assert _master_indices_warnings("2026-06-07_2.9.0-RELEASE_pyspark", universe="x.txt") == []


def test_master_indices_warning_when_version_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("MASTER_INDICES_CONF_DIR", str(tmp_path))  # exists but empty
    warns = _master_indices_warnings("2026-06-07_2.9.0-RELEASE_pyspark")
    assert warns and "was not found in the data store" in warns[0]
