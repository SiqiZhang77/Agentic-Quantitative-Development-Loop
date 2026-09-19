import sys
from pathlib import Path

AFTER_BACKTEST = Path(__file__).resolve().parents[1]
if str(AFTER_BACKTEST) not in sys.path:
    sys.path.append(str(AFTER_BACKTEST))
