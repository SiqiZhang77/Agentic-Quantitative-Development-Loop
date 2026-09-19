"""Tests for the list_jobs MCP tool."""

from list_jobs import list_jobs


def test_lists_completed_rejected_and_running(tmp_path):
    results = tmp_path / "simulation-results"
    results.mkdir(parents=True)
    (results / "jobA").mkdir()                        # directory -> completed
    (results / "jobB").write_text("rejected: bad")    # file -> rejected

    reqs = tmp_path / "reqs"
    reqs.mkdir()
    (reqs / "jobC.request").write_text("...")         # pending request -> running

    out = list_jobs(runtime_dir=tmp_path, requests_dir=reqs)

    statuses = {j["job_name"]: j["status"] for j in out["jobs"]}
    assert statuses == {"jobA": "completed", "jobB": "rejected", "jobC": "running"}
    assert out["count"] == 3


def test_empty(tmp_path):
    out = list_jobs(runtime_dir=tmp_path, requests_dir=tmp_path / "none")
    assert out == {"count": 0, "jobs": []}


def test_real_mode_lists_watcher_pending_requests(tmp_path, monkeypatch):
    requests = tmp_path / "simulation-requests"
    results = tmp_path / "simulation-results"
    requests.mkdir(parents=True)
    results.mkdir()
    (requests / "jobC.request").write_text("...", encoding="utf-8")
    monkeypatch.setenv("USE_REAL_BACKTESTER", "true")
    monkeypatch.setenv("SIMULATION_REQUESTS_DIR", str(requests))
    monkeypatch.setenv("SIMULATION_RESULTS_DIR", str(results))

    out = list_jobs()

    assert out == {"count": 1, "jobs": [{"job_name": "jobC", "status": "running"}]}
