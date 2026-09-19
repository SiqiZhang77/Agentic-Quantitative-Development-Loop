from pathlib import Path
import sys


MCP_ROOT = Path(__file__).resolve().parents[1]
if str(MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(MCP_ROOT))

from submit_backtest import submit_backtest


def test_submit_backtest_writes_all_atrade_sifting_branch_overrides(tmp_path):
    template = tmp_path / "baseline.request"
    template.write_text(
        "\n".join(
            [
                "atrade_sifting_commons = main",
                "atrade_sifting_pretrade = main",
                "atrade_sifting_portfolio = main",
                "atrade_sifting_hedge = main",
                "atrade_sifting_model = main",
                "atrade_sifting_config = main",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = submit_backtest(
        "SCRUM_22",
        atrade_sifting_commons="quant/SCRUM-22/commons",
        atrade_sifting_pretrade="quant/SCRUM-22/pretrade",
        atrade_sifting_portfolio="quant/SCRUM-22/portfolio",
        atrade_sifting_hedge="quant/SCRUM-22/hedge",
        atrade_sifting_model="quant/SCRUM-22/model",
        atrade_sifting_config="quant/SCRUM-22/config",
        baseline=template,
        dry_run=True,
    )

    request = result["request"]
    assert "atrade_sifting_commons = quant/SCRUM-22/commons" in request
    assert "atrade_sifting_pretrade = quant/SCRUM-22/pretrade" in request
    assert "atrade_sifting_portfolio = quant/SCRUM-22/portfolio" in request
    assert "atrade_sifting_hedge = quant/SCRUM-22/hedge" in request
    assert "atrade_sifting_model = quant/SCRUM-22/model" in request
    assert "atrade_sifting_config = quant/SCRUM-22/config" in request
