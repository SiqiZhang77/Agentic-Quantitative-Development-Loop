from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).parents[1]
DAGS_ROOT = PROJECT_ROOT / "dags"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if str(DAGS_ROOT) not in sys.path:
    sys.path.insert(0, str(DAGS_ROOT))
