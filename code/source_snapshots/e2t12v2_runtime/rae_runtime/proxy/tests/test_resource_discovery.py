import sys
from pathlib import Path

PROXY_ROOT = Path(__file__).resolve().parents[1]
if str(PROXY_ROOT) not in sys.path:
    sys.path.insert(0, str(PROXY_ROOT))

import resource_discovery as rd


_TREE_A = [
    {"path": "src/ga/genetic_algorithm.py", "size": 4200},
    {"path": "src/ga/operators.py", "size": 1800},
    {"path": "src/data/processed_dataset.csv", "size": 5_000_000},
    {"path": "notebooks/GA_2025.ipynb", "size": 900_000},
    {"path": "assets/logo.png", "size": 30_000},
    {"path": "__pycache__/ga.cpython-311.pyc", "size": 900},
    {"path": "README.md", "size": 500},
]
_TREE_B = [
    {"path": "handlers/loader.py", "size": 2100},
    {"path": "README.md", "size": 400},
]


def _lister(trees):
    def lister(*, branch, repo_name):
        if repo_name not in trees:
            raise RuntimeError(f"unreachable repo {repo_name}")
        return trees[repo_name]

    return lister


def _specs(*names, allowed=None):
    return [
        {
            "repo_full_name": name,
            "branch": "develop",
            "allowed_directories": allowed or [],
        }
        for name in names
    ]


# --- filtering -------------------------------------------------------------


def test_filter_tree_drops_binary_data_and_vendored():
    kept = {r["path"] for r in rd.filter_tree(_TREE_A)}
    assert {"src/ga/genetic_algorithm.py", "src/ga/operators.py", "README.md"} <= kept
    assert "notebooks/GA_2025.ipynb" in kept  # surfaced, not dropped
    assert "src/data/processed_dataset.csv" not in kept
    assert "assets/logo.png" not in kept
    assert "__pycache__/ga.cpython-311.pyc" not in kept


def test_filter_tree_respects_scope():
    kept = {r["path"] for r in rd.filter_tree(_TREE_A, allowed_directories=["src/ga"])}
    assert kept == {"src/ga/genetic_algorithm.py", "src/ga/operators.py"}


def test_filter_tree_dot_scope_is_unrestricted():
    kept = rd.filter_tree(_TREE_A, allowed_directories=["."])
    assert any(r["path"] == "README.md" for r in kept)


# --- multi-repo discovery --------------------------------------------------


def test_discover_entries_spans_repositories():
    entries = rd.discover_entries(
        _specs("org/a", "org/b"),
        tree_lister=_lister({"org/a": _TREE_A, "org/b": _TREE_B}),
    )
    keys = {rd.entry_key(e) for e in entries}
    assert "org/a:src/ga/operators.py" in keys
    assert "org/b:handlers/loader.py" in keys
    # same path in two repos stays distinguishable
    assert "org/a:README.md" in keys and "org/b:README.md" in keys


def test_discover_entries_applies_per_repository_scope():
    specs = [
        {"repo_full_name": "org/a", "branch": "develop", "allowed_directories": ["src/ga"]},
        {"repo_full_name": "org/b", "branch": "develop", "allowed_directories": ["handlers"]},
    ]
    entries = rd.discover_entries(
        specs, tree_lister=_lister({"org/a": _TREE_A, "org/b": _TREE_B})
    )
    keys = {rd.entry_key(e) for e in entries}
    assert keys == {
        "org/a:src/ga/genetic_algorithm.py",
        "org/a:src/ga/operators.py",
        "org/b:handlers/loader.py",
    }
    # org/b's scope must not leak into org/a and vice versa
    assert "org/a:README.md" not in keys
    assert "org/b:README.md" not in keys


def test_discover_entries_skips_unreachable_repository():
    entries = rd.discover_entries(
        _specs("org/a", "org/missing"),
        tree_lister=_lister({"org/a": _TREE_A}),
    )
    repos = {e["repo_full_name"] for e in entries}
    assert repos == {"org/a"}  # one bad repo does not sink the run


def test_discover_entries_ignores_specs_without_repo_or_branch():
    specs = [{"repo_full_name": "", "branch": "develop"}, {"repo_full_name": "org/a"}]
    assert rd.discover_entries(specs, tree_lister=_lister({"org/a": _TREE_A})) == []


def test_discover_entries_carries_per_repository_branch():
    specs = [
        {"repo_full_name": "org/a", "branch": "quant/T-1", "allowed_directories": []},
        {"repo_full_name": "org/b", "branch": "develop", "allowed_directories": []},
    ]
    entries = rd.discover_entries(
        specs, tree_lister=_lister({"org/a": _TREE_A, "org/b": _TREE_B})
    )
    branches = {e["repo_full_name"]: e["branch"] for e in entries}
    assert branches == {"org/a": "quant/T-1", "org/b": "develop"}


# --- rendering / budget ----------------------------------------------------


def test_format_entry_map_groups_by_repo_and_flags_large():
    entries = rd.discover_entries(
        _specs("org/a", "org/b"),
        tree_lister=_lister({"org/a": _TREE_A, "org/b": _TREE_B}),
    )
    block = rd.format_entry_map(entries)
    assert "Repository org/a (branch develop):" in block
    assert "Repository org/b (branch develop):" in block
    assert "org/a:src/ga/operators.py" in block
    assert "large" in block  # the ~900KB notebook
    assert "processed_dataset.csv" not in block


def test_format_entry_map_shares_budget_across_repositories():
    many_a = [{"path": f"a/f{i}.py", "size": 10} for i in range(50)]
    many_b = [{"path": f"b/f{i}.py", "size": 10} for i in range(50)]
    entries = rd.discover_entries(
        _specs("org/a", "org/b"),
        tree_lister=_lister({"org/a": many_a, "org/b": many_b}),
    )
    block = rd.format_entry_map(entries, max_entries=10)
    listed = [line for line in block.splitlines() if line.startswith("- org/")]
    assert len(listed) == 10  # global cap respected
    # neither repository is starved out entirely
    assert any(line.startswith("- org/a:") for line in listed)
    assert any(line.startswith("- org/b:") for line in listed)
    assert "more files in this repository not shown" in block


def test_format_entry_map_empty():
    assert "no candidate files" in rd.format_entry_map([])


# --- selection -------------------------------------------------------------


def test_select_relevant_entries_validates_against_map():
    entries = rd.discover_entries(
        _specs("org/a", "org/b"),
        tree_lister=_lister({"org/a": _TREE_A, "org/b": _TREE_B}),
    )

    def fake_llm(prompt):
        # one real entry, one hallucinated, one in an unlisted repository
        return '["org/b:handlers/loader.py", "org/a:nope.py", "org/evil:secret.py"]'

    chosen = rd.select_relevant_entries("find it", entries, llm=fake_llm)
    assert [rd.entry_key(e) for e in chosen] == ["org/b:handlers/loader.py"]
    assert chosen[0]["repo_full_name"] == "org/b"
    assert chosen[0]["branch"] == "develop"


def test_select_relevant_entries_falls_back_to_smallest():
    entries = rd.discover_entries(
        _specs("org/b"), tree_lister=_lister({"org/b": _TREE_B})
    )
    chosen = rd.select_relevant_entries(
        "x", entries, llm=lambda p: "no idea", max_files=1
    )
    assert [rd.entry_key(e) for e in chosen] == ["org/b:README.md"]


def test_select_relevant_entries_empty():
    assert rd.select_relevant_entries("x", [], llm=lambda p: "[]") == []


# --- payload -> specs ------------------------------------------------------


def test_repository_specs_from_payload_uses_source_branches_by_default():
    payload = {
        "repositories": [
            {
                "repo_full_name": "org/a",
                "source_branch": "develop",
                "target_branch": "quant/T-1",
                "allowed_directories": ["src"],
            }
        ]
    }
    specs = rd.repository_specs_from_payload(payload)
    assert specs == [
        {"repo_full_name": "org/a", "branch": "develop", "allowed_directories": ["src"]}
    ]


def test_repository_specs_prefers_prepared_target_branch():
    payload = {
        "repositories": [
            {
                "repo_full_name": "org/a",
                "source_branch": "develop",
                "target_branch": "quant/T-1",
            },
            {
                "repo_full_name": "org/b",
                "source_branch": "main",
                "target_branch": "quant/T-1",
            },
        ]
    }
    specs = rd.repository_specs_from_payload(
        payload,
        prefer_target_branch=True,
        available_target_branches={("org/a", "quant/T-1")},
    )
    branches = {s["repo_full_name"]: s["branch"] for s in specs}
    # org/a's target branch exists; org/b's does not, so it falls back to source
    assert branches == {"org/a": "quant/T-1", "org/b": "main"}


def test_repository_specs_skips_malformed_records():
    payload = {"repositories": [{"repo_full_name": ""}, "not-a-dict", {"x": 1}]}
    assert rd.repository_specs_from_payload(payload) == []
