"""``list_master_indices`` — discover the master-index data pools available on
the cluster (read-only).

Each version is a daily snapshot of the trading universe, keyed by snapshot
date + release + variant. For each one we return: ``version`` (the value to pass
to ``submit_backtest(master_indices=...)``), the **list** of longStockDefs
variants (the universe filters - the LLM picks one and passes it as
``universe``), the shortStockDefs / shortIndiceDefs files, the matching
``runner_dataURL``, and an ``is_latest`` flag. Filenames vary between versions,
so submit fills them from here instead of copying a baseline.

The LLM must call this BEFORE ``submit_backtest`` to pick a current version,
rather than hardcoding outdated paths or guessing dataset versions.

**This is a discovery tool: it reports ground truth or fails — it never
substitutes a stale/guessed pool.** Returning a 2026-05-22 pool when the caller
wanted 2026-05-20 would be exactly the "hallucinated dataset version" this tool
exists to prevent. For offline testing, point ``MASTER_INDICES_CONF_DIR`` at a
local directory of fake ``master-indices_*`` folders; the same scan runs against
it (no in-code fixture). stdlib only.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

CONF_DIR = os.getenv("MASTER_INDICES_CONF_DIR", "/opt/simulations-service/conf")

_DIR_RE = re.compile(r"^master-indices_(\d{4}-\d{2}-\d{2})_(.+)_([^_]+)$")


def _pool_from_dir(path: Path, name: str) -> dict[str, Any] | None:
    m = _DIR_RE.match(name)
    if not m:
        return None
    date, release, variant = m.groups()
    try:
        files = [f.name for f in path.iterdir()] if path.is_dir() else []
    except OSError:  # unreadable snapshot dir -> treat as empty, don't crash the whole scan
        files = []

    def matches(prefix: str) -> list[str]:
        return sorted(f for f in files if f.startswith(prefix))

    return {
        "version": f"{date}_{release}_{variant}",
        "date": date,
        "release": release,
        "variant": variant,
        # All variants found, so the LLM can pick which one to use. longStockDefs
        # usually has several (the universe filters: trimmed / exBlackListed /
        # inPrimaryShareClass / ...); short defs usually one - but never assumed.
        "longStockDefs": matches("longStockDefs"),
        "shortStockDefs": matches("shortStockDefs"),
        "shortIndiceDefs": matches("shortIndiceDefs"),
        "runner_dataURL": f"{date}_{variant}/{release}",
    }


def list_master_indices(
    latest_only: bool = False,
    filter: str | None = None,
    conf_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Return the master-indices versions actually present on the cluster,
    newest first - each a dict with ``version`` / ``date`` / ``release`` /
    ``variant``, the ``longStockDefs`` variant list, ``shortStockDefs`` /
    ``shortIndiceDefs``, ``runner_dataURL`` and ``is_latest``.

    ``latest_only`` keeps only the newest; ``filter`` keeps versions whose id
    contains that substring. Reports ground truth or fails — never substitutes a
    stale/guessed version.
    """
    conf = Path(conf_dir or CONF_DIR).expanduser()
    if not conf.is_dir():
        return {
            "status": "failed",
            "error": f"cannot read the master-indices store at {conf}; "
                     "is the cluster/conf directory reachable?",
            "pools": [],
        }

    pools = [p for p in (_pool_from_dir(conf / e.name, e.name)
                         for e in conf.iterdir()) if p]
    if filter:
        needle = filter.lower()
        pools = [p for p in pools if needle in p["version"].lower()]
    pools.sort(key=lambda p: p["date"], reverse=True)
    for i, p in enumerate(pools):
        p["is_latest"] = i == 0
    if latest_only:
        pools = pools[:1]
    return {"status": "completed", "count": len(pools), "pools": pools}


if __name__ == "__main__":
    import json

    print(json.dumps(list_master_indices(), indent=2))
