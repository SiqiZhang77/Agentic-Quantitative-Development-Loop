"""Re-iteration must build on prior tuning: the edit agent reads the strategy
from the ticket's quant branch once it holds a previous edit, and only from the
source ref (main) on the first iteration. Otherwise iteration 2+ would overwrite
the previous tuning with a fresh change off main.
"""
import pytest

from pipeline_mcp import (
    InvalidStrategyRequest,
    NoCodeChanges,
    _read_ref,
    _validate_request_value_only_change,
)


PATH = "rae_runtime/proxy/strategy.request"
BASELINE = "# strategy\nNPORT = 80\nNFREQ = 4\nrunner_url = https://example.test?a=b\n"


def test_first_iteration_reads_source_ref():
    # No file on the branch yet -> read the source ref (main).
    assert _read_ref("main", "quant/SCRUM-9", branch_has_file=False) == "main"


def test_reiteration_reads_ticket_branch():
    # Branch already holds prior tuning -> read from it, building on that edit.
    assert _read_ref("main", "quant/SCRUM-9", branch_has_file=True) == "quant/SCRUM-9"


def test_request_validation_accepts_existing_value_change():
    edited = BASELINE.replace("NPORT = 80", "NPORT = 60")
    _validate_request_value_only_change(BASELINE, edited, PATH)


def test_request_validation_rejects_comment_only_change():
    edited = BASELINE.replace("# strategy", "# edited strategy")
    with pytest.raises(NoCodeChanges, match="without changing an engine parameter"):
        _validate_request_value_only_change(BASELINE, edited, PATH)


@pytest.mark.parametrize(
    ("edited", "message"),
    [
        (BASELINE + "NEW_KEY = 1\n", "added keys"),
        (BASELINE.replace("NFREQ = 4\n", ""), "removed keys"),
        (
            "# strategy\nNFREQ = 4\nNPORT = 80\nrunner_url = https://example.test?a=b\n",
            "reordered",
        ),
        (BASELINE + "NPORT = 60\n", "duplicates parameter"),
        (BASELINE + "print('not a request parameter')\n", "not a KEY = value"),
    ],
)
def test_request_validation_rejects_contract_changes(edited, message):
    with pytest.raises(InvalidStrategyRequest, match=message):
        _validate_request_value_only_change(BASELINE, edited, PATH)
