import sys
from pathlib import Path

PROXY_ROOT = Path(__file__).resolve().parents[1]
if str(PROXY_ROOT) not in sys.path:
    sys.path.insert(0, str(PROXY_ROOT))

import github_client
import review_agent


def _payload(repositories=None, **strategy):
    return {
        "issue_key": "SCRUM-1",
        "execution_objectives": {},
        "jira_context": {"summary": "where does the GA live?"},
        "repositories": repositories
        if repositories is not None
        else [
            {
                "repo_full_name": "org/a",
                "source_branch": "develop",
                "target_branch": "quant/SCRUM-1",
                "allowed_directories": [],
            },
            {
                "repo_full_name": "org/b",
                "source_branch": "main",
                "target_branch": "quant/SCRUM-1",
                "allowed_directories": [],
            },
        ],
        "strategy": {"repo_full_name": "org/a", "ref": "develop", **strategy},
    }


def _trees():
    return {
        "org/a": [{"path": "src/ga.py", "size": 1200}],
        "org/b": [{"path": "handlers/loader.py", "size": 800}],
    }


def _install_lister(monkeypatch, trees):
    def lister(*, branch, repo_name):
        return trees[repo_name]

    monkeypatch.setattr(github_client, "list_repo_tree", lister)


def test_discovery_spans_repositories_and_reads_from_the_right_one(monkeypatch):
    _install_lister(monkeypatch, _trees())
    reads = []

    def code_reader(*, branch, path, repo_name):
        reads.append((repo_name, branch, path))
        return f"# contents of {repo_name}:{path}"

    def llm(prompt):
        if "JSON array" in prompt:
            # pick the file in the *second* repository
            return '["org/b:handlers/loader.py"]'
        return '{"summary": "The loader lives in org/b."}'

    out = review_agent.analyse_read_only_task(
        _payload(), code_reader=code_reader, llm=llm
    )
    # read from org/b at its own source branch, not the primary repo's
    assert reads == [("org/b", "main", "handlers/loader.py")]
    assert out["recommended_action"] == "review"
    assert "org/b:handlers/loader.py" in out["evaluation"]["summary"]
    assert "The loader lives in org/b" in out["evaluation"]["summary"]


def test_discovery_read_only_uses_source_branches(monkeypatch):
    seen = []

    def lister(*, branch, repo_name):
        seen.append((repo_name, branch))
        return _trees()[repo_name]

    monkeypatch.setattr(github_client, "list_repo_tree", lister)

    review_agent.analyse_read_only_task(
        _payload(),
        code_reader=lambda **kw: "code",
        llm=lambda p: '["org/a:src/ga.py"]' if "JSON array" in p else '{"summary": "ok"}',
    )
    # a read-only run creates no branches, so never the quant/* target
    assert seen == [("org/a", "develop"), ("org/b", "main")]


def test_named_path_still_bypasses_discovery(monkeypatch):
    def boom(**kw):
        raise AssertionError("discovery should not run when a path is named")

    monkeypatch.setattr(github_client, "list_repo_tree", boom)

    def code_reader(*, branch, path, repo_name):
        assert path == "src/named.py"
        return "code"

    out = review_agent.analyse_read_only_task(
        _payload(path="src/named.py"),
        code_reader=code_reader,
        llm=lambda p: '{"summary": "ok"}',
    )
    assert "src/named.py" in out["evaluation"]["summary"]


def test_reports_when_no_repository_can_be_listed(monkeypatch):
    def empty(**kw):
        return []

    monkeypatch.setattr(github_client, "list_repo_tree", empty)
    out = review_agent.analyse_read_only_task(
        _payload(), code_reader=lambda **kw: "x", llm=lambda p: '{"summary": "unused"}'
    )
    assert "could not be listed" in out["evaluation"]["summary"]
    assert "org/a@develop" in out["evaluation"]["summary"]


def test_unreadable_candidate_is_skipped(monkeypatch):
    _install_lister(monkeypatch, _trees())

    def code_reader(*, branch, path, repo_name):
        if repo_name == "org/b":
            raise RuntimeError("404 not found")
        return "# ga source"

    def llm(prompt):
        if "JSON array" in prompt:
            return '["org/b:handlers/loader.py", "org/a:src/ga.py"]'
        return '{"summary": "found it"}'

    out = review_agent.analyse_read_only_task(
        _payload(), code_reader=code_reader, llm=llm
    )
    # the unreadable one is dropped, the readable one still analysed
    assert "org/a:src/ga.py" in out["evaluation"]["summary"]
    assert "org/b" not in out["evaluation"]["summary"].split(":")[0]
