"""mcp_client polling: a real backtest takes ~10 min, so the client must poll
get_backtest_status until terminal rather than checking once. Mock mode reports
'completed' on the first check, so polling must not add any wait there.
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

_SANDBOX = Path(__file__).parent.parent
if str(_SANDBOX) not in sys.path:
    sys.path.insert(0, str(_SANDBOX))

import mcp_client as mc


def _run(coro):
    return asyncio.run(coro)


def test_returns_immediately_when_first_check_completed():
    call = AsyncMock(return_value={"status": "completed", "job_name": "j"})
    with patch.object(mc, "_call", call), \
         patch.object(mc.asyncio, "sleep", AsyncMock()) as sleep:
        out = _run(mc._poll_until_terminal(object(), "j", "2020-01-01T00:00:00+00:00", 5400))
    assert out["status"] == "completed"
    sleep.assert_not_awaited()  # mock path must not wait


def test_polls_through_running_then_completes_with_backoff():
    call = AsyncMock(side_effect=[
        {"status": "running"}, {"status": "running"}, {"status": "completed"},
    ])
    with patch.object(mc, "_call", call), \
         patch.object(mc.asyncio, "sleep", AsyncMock()) as sleep:
        out = _run(mc._poll_until_terminal(object(), "j", "2020-01-01T00:00:00+00:00", 5400))
    assert out["status"] == "completed"
    assert call.await_count == 3
    # Exponential backoff: 5s then 10s between the three checks.
    assert [c.args[0] for c in sleep.await_args_list] == [5, 10]


def test_propagates_terminal_timeout_from_status():
    call = AsyncMock(return_value={"status": "timeout", "job_name": "j"})
    with patch.object(mc, "_call", call), \
         patch.object(mc.asyncio, "sleep", AsyncMock()):
        out = _run(mc._poll_until_terminal(object(), "j", "2020-01-01T00:00:00+00:00", 5400))
    assert out["status"] == "timeout"


def test_poll_timeout_prefers_request_iteration_controls():
    assert mc._poll_timeout({"iteration_controls": {"timeout_seconds": 1200}}) == 1200


def test_poll_timeout_falls_back_when_absent(monkeypatch):
    monkeypatch.delenv("BACKTEST_POLL_TIMEOUT", raising=False)
    assert mc._poll_timeout({}) == 5400


def test_run_backtest_surfaces_failed_submission():
    call = AsyncMock(
        return_value={
            "status": "failed",
            "error": "delivery failed (could not write the request to the watched dir)",
        }
    )
    with patch.object(mc, "_call", call), \
         patch.object(mc, "_make_transport", lambda: object()), \
         patch.object(mc, "Client") as client:
        client.return_value.__aenter__.return_value = object()
        with pytest.raises(RuntimeError, match="backtest submission failed over MCP"):
            _run(mc._run_backtest({"issue_key": "SCRUM-9", "run_id": "run-1"}))
    assert call.await_count == 1


def test_run_backtest_rejects_submission_without_timestamp():
    call = AsyncMock(return_value={"status": "submitted", "job_name": "j"})
    with patch.object(mc, "_call", call), \
         patch.object(mc, "_make_transport", lambda: object()), \
         patch.object(mc, "Client") as client:
        client.return_value.__aenter__.return_value = object()
        with pytest.raises(RuntimeError, match="did not return submitted_at"):
            _run(mc._run_backtest({"issue_key": "SCRUM-9", "run_id": "run-1"}))
    assert call.await_count == 1


def test_run_backtest_does_not_inject_repository_branches_into_engine_keys():
    call = AsyncMock(return_value={"status": "failed", "error": "stop after submit"})
    payload = {
        "issue_key": "SCRUM-9",
        "run_id": "run-1",
        "repositories": [
            {
                "repo_full_name": "bankingscience/ATPDataHandlersRepo",
                "source_branch": "develop",
                "target_branch": "quant/SCRUM-9",
            }
        ],
        "repository_branch_map": {
            "atrade_sifting_pretrade": "quant/SCRUM-9",
        },
    }
    with patch.object(mc, "_call", call), \
         patch.object(mc, "_make_transport", lambda: object()), \
         patch.object(mc, "Client") as client:
        client.return_value.__aenter__.return_value = object()
        with pytest.raises(RuntimeError, match="backtest submission failed over MCP"):
            _run(mc._run_backtest(payload))

    submit_args = call.await_args.args[2]
    assert "atrade_sifting_pretrade" not in submit_args
