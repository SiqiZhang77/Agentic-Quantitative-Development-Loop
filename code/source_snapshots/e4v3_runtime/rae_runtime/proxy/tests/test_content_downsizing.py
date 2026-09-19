import json
import pytest
import sys
from pathlib import Path

PROXY_ROOT = Path(__file__).resolve().parents[1]
if str(PROXY_ROOT) not in sys.path:
    sys.path.insert(0, str(PROXY_ROOT))

import content_downsizing as cd


def _notebook(with_outputs=True, extra_cells=0):
    cells = [
        {
            "cell_type": "markdown",
            "source": ["# Super feature GA\n", "Notes about the run.\n"],
            "metadata": {"scrolled": True},
        },
        {
            "cell_type": "code",
            "execution_count": 12,
            "source": ["def evolve(pop):\n", "    return pop\n"],
            "metadata": {},
            "outputs": (
                [{"output_type": "display_data", "data": {"image/png": "A" * 50_000}}]
                if with_outputs
                else []
            ),
        },
    ]
    for i in range(extra_cells):
        cells.append(
            {
                "cell_type": "code",
                "execution_count": None,
                "source": [f"x = {i}\n"],
                "metadata": {},
                "outputs": [],
            }
        )
    return json.dumps(
        {
            "cells": cells,
            "metadata": {"kernelspec": {}},
            "nbformat": 4,
            "nbformat_minor": 5,
        }
    )


# --- notebook stripping ----------------------------------------------------


def test_strip_notebook_keeps_source_drops_outputs():
    stripped = cd.strip_notebook(_notebook())
    assert "def evolve(pop):" in stripped
    assert "# Super feature GA" in stripped
    assert "A" * 100 not in stripped  # the base64 image is gone
    assert "[outputs omitted]" in stripped
    assert "execution_count" not in stripped
    assert "kernelspec" not in stripped


def test_strip_notebook_is_dramatically_smaller():
    raw = _notebook()
    stripped = cd.strip_notebook(raw)
    assert len(stripped) < len(raw) / 10


def test_strip_notebook_handles_string_source():
    raw = json.dumps({"cells": [{"cell_type": "code", "source": "print(1)\n"}]})
    assert "print(1)" in cd.strip_notebook(raw)


def test_strip_notebook_skips_empty_cells():
    raw = json.dumps({"cells": [{"cell_type": "code", "source": ["   \n"]}]})
    assert cd.strip_notebook(raw) == "[notebook contains no non-empty cells]"


def test_strip_notebook_returns_none_for_non_notebook():
    assert cd.strip_notebook("not json at all") is None
    assert cd.strip_notebook(json.dumps({"no": "cells"})) is None


# --- validity check (the commit guard's basis) -----------------------------


def test_is_valid_notebook():
    assert cd.is_valid_notebook(_notebook()) is True
    assert cd.is_valid_notebook("# --- cell 1 (code) ---\nprint(1)") is False
    assert cd.is_valid_notebook("") is False


@pytest.mark.parametrize(
    "content, expected",
    [
        ('{"cells": []}', "nbformat"),
        ('{"cells": [], "nbformat": 4, "nbformat_minor": 5}', "metadata"),
        ('{"metadata": {}, "nbformat": 4, "nbformat_minor": 5}', "cells"),
        ("[]", "top level"),
        ("not json", "not valid JSON"),
    ],
)
def test_malformed_notebooks_are_rejected(content, expected):
    errors = cd.notebook_structure_errors(content)
    assert errors, f"expected {content!r} to be rejected"
    assert expected in " ".join(errors)


def test_a_fully_formed_empty_notebook_is_valid():
    # An empty notebook is legitimate nbformat; only missing structure is not.
    empty = json.dumps(
        {"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
    )
    assert cd.notebook_structure_errors(empty) == []


@pytest.mark.parametrize(
    "cell, expected",
    [
        ({"source": ["x\n"], "metadata": {}}, "invalid cell_type"),
        ({"cell_type": "sql", "source": ["x\n"], "metadata": {}}, "invalid cell_type"),
        ({"cell_type": "markdown", "metadata": {}}, "source must be"),
        ({"cell_type": "markdown", "source": [1, 2], "metadata": {}}, "source must be"),
        ({"cell_type": "markdown", "source": "x"}, "missing object metadata"),
        (
            {"cell_type": "code", "source": "x", "metadata": {}, "execution_count": 1},
            "missing a list of outputs",
        ),
        (
            {"cell_type": "code", "source": "x", "metadata": {}, "outputs": []},
            "missing execution_count",
        ),
        (
            {
                "cell_type": "code",
                "source": "x",
                "metadata": {},
                "outputs": [],
                "execution_count": "1",
            },
            "non-integer execution_count",
        ),
        ("not-a-cell", "not an object"),
    ],
)
def test_malformed_cells_are_rejected(cell, expected):
    content = json.dumps(
        {"cells": [cell], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}
    )
    errors = cd.notebook_structure_errors(content)
    assert errors, f"expected cell {cell!r} to be rejected"
    assert expected in " ".join(errors)


def test_permissive_parsing_still_strips_irregular_notebooks():
    # Reading must not inherit the commit guard's strictness: a notebook missing
    # nbformat should still render as source rather than fall back to truncation.
    irregular = json.dumps({"cells": [{"cell_type": "code", "source": ["print(1)\n"]}]})
    assert cd.notebook_structure_errors(irregular)  # not committable
    assert "print(1)" in cd.strip_notebook(irregular)  # still readable


def test_stripped_view_is_never_a_valid_notebook():
    # This is what makes "read stripped, write original" enforceable.
    stripped = cd.strip_notebook(_notebook())
    assert cd.is_valid_notebook(stripped) is False


def test_is_notebook_path():
    assert cd.is_notebook("a/b/GA.ipynb") is True
    assert cd.is_notebook("org/repo:a/b/GA.IPYNB") is True
    assert cd.is_notebook("a/b/module.py") is False


# --- downsize_for_model ----------------------------------------------------


def test_downsize_notebook_reports_transform():
    text, info = cd.downsize_for_model("GA.ipynb", _notebook())
    assert info["kind"] == "notebook"
    assert "def evolve" in text
    assert info["returned_chars"] < info["original_chars"]
    assert "Do not commit this reduced view" in info["notice"]


def test_downsize_plain_file_untouched_when_small():
    text, info = cd.downsize_for_model("m.py", "print(1)")
    assert text == "print(1)"
    assert info["kind"] == "none"
    assert "notice" not in info


def test_downsize_truncates_large_plain_file():
    text, info = cd.downsize_for_model("m.py", "x" * 5000, budget_chars=1000)
    assert info["kind"] == "truncated"
    assert len(text) <= 1000
    assert "TRUNCATED" in text


def test_downsize_truncation_keeps_the_file_tail_as_well_as_the_head():
    raw = "START_SYMBOL\n" + ("x" * 5000) + "\nEND_SYMBOL\n"
    text, info = cd.downsize_for_model("m.py", raw, budget_chars=1000)

    assert info["kind"] == "truncated"
    assert "START_SYMBOL" in text
    assert "END_SYMBOL" in text
    assert "TRUNCATED" in text


def test_downsize_notebook_then_truncates_when_still_large():
    raw = _notebook(with_outputs=False, extra_cells=400)
    text, info = cd.downsize_for_model("GA.ipynb", raw, budget_chars=500)
    assert info["kind"] == "notebook+truncated"
    assert len(text) <= 500
    assert "neither complete" in info["notice"]


def test_downsize_malformed_notebook_falls_back_to_truncation():
    text, info = cd.downsize_for_model("broken.ipynb", "x" * 5000, budget_chars=1000)
    assert info["kind"] == "truncated"
    assert len(text) <= 1000
