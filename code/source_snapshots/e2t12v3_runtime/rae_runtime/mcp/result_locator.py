"""result_locator - find the raw resultsTable.csv for a completed run.

Filesystem discovery (unzip + locate) kept SEPARATE from the pure transform in
engine_adapter.py, per the "don't mix I/O with data transformation" rule.

Real layout (confirmed from a real run): the engine writes resultsTable.csv at
the TOP level of the results dir, next to the job-*-log.zip - no unzip needed for
the summary metrics. (The forCluster/ZQQ equity-curve files are a separate parse
step.) We check the top level first, then fall back to unzipping + searching, so
it stays robust to other layouts.
"""

from __future__ import annotations

import zipfile
from pathlib import Path


def locate_results_table(results_dir: str | Path) -> Path:
    """Return the path to resultsTable.csv for a completed run.

    Looks at the results-dir top level first (where the engine writes it); if not
    there, unzips the job log and searches inside.
    """
    results_dir = Path(results_dir)

    # 1) Top level, next to the zip - the normal case, no unzip needed.
    top = results_dir / "resultsTable.csv"
    if top.is_file():
        return top

    # 2) Fallback: unzip the job-log zip and search inside.
    zips = sorted(results_dir.glob("*.zip"))
    if zips:
        dest = results_dir / "_extracted"
        dest.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zips[0]) as zf:
            zf.extractall(dest)
        for csv_path in dest.rglob("resultsTable.csv"):
            return csv_path

    raise FileNotFoundError(f"resultsTable.csv not found under {results_dir}")
