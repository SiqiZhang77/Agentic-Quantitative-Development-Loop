import sys
from pathlib import Path

PROXY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROXY))
