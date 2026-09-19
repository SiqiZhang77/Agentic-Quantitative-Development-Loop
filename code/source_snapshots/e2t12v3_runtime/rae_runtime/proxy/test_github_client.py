from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import github_client


class FakeContent:
    sha = "file-sha"

    def __init__(self, text):
        self.decoded_content = text.encode("utf-8")


class FakeRepo:
    def __init__(self, current_text):
        self.current_text = current_text
        self.updated = False
        self.created = False

    def get_contents(self, path, ref):
        return FakeContent(self.current_text)

    def update_file(self, **kwargs):
        self.updated = True
        return {"commit": {"sha": "commit-sha"}}

    def create_file(self, **kwargs):
        self.created = True
        return {"commit": {"sha": "commit-sha"}}


class FakeBranch:
    class Commit:
        sha = "base-sha"

    commit = Commit()


class FakeBranchRepo:
    def __init__(self, *, existing_branches=None, fail_existing=None):
        self.existing_branches = set(existing_branches or [])
        self.fail_existing = fail_existing
        self.created_ref = None

    def get_branch(self, name):
        if name in self.existing_branches:
            if self.fail_existing:
                raise self.fail_existing
            return FakeBranch()
        if name == "main":
            return FakeBranch()
        raise Exception("404 Not Found")

    def create_git_ref(self, ref, sha):
        self.created_ref = (ref, sha)


class FakeGithub:
    def __init__(self, repo):
        self.repo = repo
        self.requested_repo = None

    def get_repo(self, name):
        self.requested_repo = name
        return self.repo


def test_push_strategy_code_rejects_unchanged_content(monkeypatch):
    repo = FakeRepo("same code")
    monkeypatch.setattr(github_client, "get_github_client", lambda: FakeGithub(repo))

    message = github_client.push_strategy_code(
        code="same code",
        branch="quant/SCRUM-9",
        path="rae_runtime/proxy/strategy.py",
        commit_message="SCRUM-9: no-op",
    )

    assert message == "No changes for rae_runtime/proxy/strategy.py on quant/SCRUM-9; skipped commit"
    assert repo.updated is False
    assert repo.created is False


def test_push_strategy_code_updates_when_content_changes(monkeypatch):
    repo = FakeRepo("old code")
    fake_github = FakeGithub(repo)
    monkeypatch.setattr(github_client, "get_github_client", lambda: fake_github)

    message = github_client.push_strategy_code(
        code="new code",
        branch="quant/SCRUM-9",
        path="rae_runtime/proxy/strategy.py",
        commit_message="SCRUM-9: update strategy",
        repo_name="bankingscience/ATPConnectorsRepo",
    )

    assert repo.updated is True
    assert repo.created is False
    assert fake_github.requested_repo == "bankingscience/ATPConnectorsRepo"
    assert message == "Updated rae_runtime/proxy/strategy.py on quant/SCRUM-9"


def test_get_github_client_reports_missing_token(monkeypatch):
    monkeypatch.setattr(github_client, "Github", object())
    monkeypatch.setattr(github_client, "Auth", object())
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    with pytest.raises(RuntimeError, match="GITHUB_TOKEN is not set"):
        github_client.get_github_client()


def test_get_github_token_reads_current_environment(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert github_client.get_github_token() is None

    monkeypatch.setenv("GITHUB_TOKEN", "token-after-import")
    assert github_client.get_github_token() == "token-after-import"


def test_create_feature_branch_reuses_existing_branch(monkeypatch):
    repo = FakeBranchRepo(existing_branches={"quant/SCRUM-9"})
    monkeypatch.setattr(github_client, "get_github_client", lambda: FakeGithub(repo))

    message = github_client.create_feature_branch("quant/SCRUM-9")

    assert message == "Reused existing branch quant/SCRUM-9"
    assert repo.created_ref is None


def test_create_feature_branch_wraps_existing_branch_assertion(monkeypatch):
    repo = FakeBranchRepo(
        existing_branches={"quant/SCRUM-9"},
        fail_existing=AssertionError(),
    )
    monkeypatch.setattr(github_client, "get_github_client", lambda: FakeGithub(repo))

    with pytest.raises(
        github_client.GithubOperationError,
        match=r"check_existing_branch\(quant/SCRUM-9\) failed: AssertionError",
    ):
        github_client.create_feature_branch("quant/SCRUM-9")
