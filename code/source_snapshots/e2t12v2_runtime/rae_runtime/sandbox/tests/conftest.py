import sys
from pathlib import Path

SANDBOX = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SANDBOX))
sys.path.insert(0, str(SANDBOX.parent / "proxy"))
