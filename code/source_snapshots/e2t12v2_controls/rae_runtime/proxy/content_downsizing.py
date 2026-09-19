"""Reduce oversized repository files to something a model can actually read.

A Jupyter notebook that carries its cell outputs is routinely megabytes of JSON,
most of it base64 images and dumped dataframes. Feeding one to a small model
either exhausts its context outright or, once truncated, spends the whole budget
on JSON metadata before reaching a single line of source. Other large text files
have the same problem in a milder form.

This module produces a *model-facing view* of a file. That view is deliberately
lossy, so it must never be used where byte-exact content matters:

- Model-facing (downsize): the MCP read_file tool, analysis and review prompts.
- Byte-exact (never downsize): artifact validation, commit change-detection, and
  anything comparing repository state.

Keeping those apart is why the transform is applied at the read_file tool
boundary rather than inside ``github_client.get_strategy_code``, which serves
both purposes.
"""

from __future__ import annotations

import json
from pathlib import PurePosixPath


# A single read should not be able to monopolise the model context.  In
# particular, a tool result remains in the agent conversation after the call,
# so several 40k-character reads of one file can overflow a 128k-token model.
# This budget leaves room for the task prompt, tool history and model output.
MODEL_READ_BUDGET_CHARS = 16_000

_OUTPUTS_OMITTED = "[outputs omitted]"


def is_notebook(path: str) -> bool:
    return PurePosixPath(str(path or "")).suffix.lower() == ".ipynb"


def _cell_source(cell: dict) -> str:
    source = cell.get("source")
    if isinstance(source, list):
        return "".join(str(part) for part in source)
    return str(source or "")


def parse_notebook(content: str) -> dict | None:
    """Return the notebook object, or None when there is nothing to render.

    Deliberately permissive: this feeds reading, where a slightly irregular
    notebook should still be rendered as source rather than fall back to raw
    JSON truncation. Writing is gated by ``notebook_structure_errors`` instead.
    """

    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("cells"), list):
        return None
    return parsed


_VALID_CELL_TYPES = frozenset({"code", "markdown", "raw"})
_MAX_REPORTED_ERRORS = 5


def _cell_errors(index: int, cell: object) -> list[str]:
    if not isinstance(cell, dict):
        return [f"cell {index} is not an object"]

    errors: list[str] = []
    cell_type = cell.get("cell_type")
    if cell_type not in _VALID_CELL_TYPES:
        errors.append(f"cell {index} has invalid cell_type {cell_type!r}")

    source = cell.get("source")
    if not isinstance(source, str) and not (
        isinstance(source, list) and all(isinstance(part, str) for part in source)
    ):
        errors.append(f"cell {index} source must be a string or list of strings")

    if not isinstance(cell.get("metadata"), dict):
        errors.append(f"cell {index} is missing object metadata")

    if cell_type == "code":
        if not isinstance(cell.get("outputs"), list):
            errors.append(f"code cell {index} is missing a list of outputs")
        if "execution_count" not in cell:
            errors.append(f"code cell {index} is missing execution_count")
        elif cell["execution_count"] is not None and not isinstance(
            cell["execution_count"], int
        ):
            errors.append(f"code cell {index} has a non-integer execution_count")
    return errors


def notebook_structure_errors(content: str) -> list[str]:
    """Structural problems that would make content invalid as a .ipynb file.

    A structural sanity check, not full nbformat validation. It exists because
    this gates commits: accepting a malformed notebook writes a broken file into
    the repository, while rejecting a good one only costs the caller a retry, so
    it errs towards strictness.
    """

    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return ["content is not valid JSON"]
    if not isinstance(parsed, dict):
        return ["top level is not a JSON object"]

    errors: list[str] = []
    if not isinstance(parsed.get("nbformat"), int):
        errors.append("missing or non-integer nbformat")
    if not isinstance(parsed.get("nbformat_minor"), int):
        errors.append("missing or non-integer nbformat_minor")
    if not isinstance(parsed.get("metadata"), dict):
        errors.append("missing or non-object notebook metadata")

    cells = parsed.get("cells")
    if not isinstance(cells, list):
        errors.append("missing or non-list cells")
        return errors[:_MAX_REPORTED_ERRORS]

    for index, cell in enumerate(cells, start=1):
        errors.extend(_cell_errors(index, cell))
        if len(errors) >= _MAX_REPORTED_ERRORS:
            break
    return errors[:_MAX_REPORTED_ERRORS]


def is_valid_notebook(content: str) -> bool:
    """Whether content is well formed enough to be committed as a .ipynb file."""

    return not notebook_structure_errors(content)


def strip_notebook(content: str) -> str | None:
    """Render a notebook as readable source, dropping outputs and metadata.

    Returns None when the content is not a parseable notebook, so the caller can
    fall back to generic truncation.
    """

    notebook = parse_notebook(content)
    if notebook is None:
        return None

    blocks: list[str] = []
    for index, cell in enumerate(notebook["cells"], start=1):
        if not isinstance(cell, dict):
            continue
        cell_type = str(cell.get("cell_type") or "code")
        source = _cell_source(cell)
        if not source.strip():
            continue
        header = f"# --- cell {index} ({cell_type}) ---"
        had_output = bool(cell.get("outputs"))
        footer = f"\n{_OUTPUTS_OMITTED}" if had_output else ""
        blocks.append(f"{header}\n{source.rstrip()}{footer}")

    if not blocks:
        return "[notebook contains no non-empty cells]"
    return "\n\n".join(blocks)


def _truncate(text: str, budget_chars: int) -> str:
    if len(text) <= budget_chars:
        return text
    omitted = len(text) - budget_chars
    marker = f"\n[TRUNCATED — {omitted} characters omitted]"
    separator = "\n"
    visible = max(0, budget_chars - len(marker) - len(separator))
    # Source files commonly place imports and constants near the start but the
    # implementation requested by a ticket near the end.  Keeping both ends is
    # much more useful than returning only the head, and prevents an agent from
    # repeatedly asking for the same file because it cannot see the target.
    head = visible // 2
    tail = visible - head
    return text[:head].rstrip() + marker + separator + text[-tail:].lstrip()


def downsize_for_model(
    path: str,
    content: str,
    *,
    budget_chars: int = MODEL_READ_BUDGET_CHARS,
) -> tuple[str, dict]:
    """Return a model-readable view of ``content`` plus a record of the change.

    The record is log-safe and is surfaced to the agent so it knows it is looking
    at a reduced view rather than the whole file.
    """

    text = content if isinstance(content, str) else str(content or "")
    original_bytes = len(text)
    kind = "none"

    if is_notebook(path):
        stripped = strip_notebook(text)
        if stripped is not None:
            text = stripped
            kind = "notebook"

    if len(text) > budget_chars:
        text = _truncate(text, budget_chars)
        kind = "notebook+truncated" if kind == "notebook" else "truncated"

    info = {
        "kind": kind,
        "original_chars": original_bytes,
        "returned_chars": len(text),
    }
    if kind != "none":
        info["notice"] = _notice_for(kind, original_bytes, len(text))
    return text, info


_NOTICES = {
    "notebook": (
        "This notebook was reduced to its cell sources; outputs and metadata were "
        "removed, so it is not the literal file contents."
    ),
    "truncated": "This file was truncated to fit the context budget.",
    "notebook+truncated": (
        "This notebook was reduced to its cell sources and then truncated, so it "
        "is neither complete nor the literal file contents."
    ),
}


def _notice_for(kind: str, original: int, returned: int) -> str:
    return (
        f"{_NOTICES[kind]} Original {original} characters, shown {returned}. "
        "Do not commit this reduced view back to the repository."
    )
