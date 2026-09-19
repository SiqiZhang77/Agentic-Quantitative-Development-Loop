"""Regression tests for GitHub branch creation read-after-write consistency."""

from types import SimpleNamespace

import pytest

import github_client


class FakeGithub:
    def __init__(self, repo):
        self.repo = repo

    def get_repo(self, name):
        self.requested_repo = name
        return self.repo


class EventuallyVisibleBranchRepo:
    def __init__(self, readback_404_count):
        self.readback_404_count = readback_404_count
        self.target_reads_after_create = 0
        self.created_ref = None

    def get_branch(self, name):
        if name == "exp2/source":
            return SimpleNamespace(commit=SimpleNamespace(sha="source-sha"))
        if name != "quant/SCRUM-180":
            raise AssertionError(f"unexpected branch {name}")
        if self.created_ref is None:
            raise Exception("404 Branch not found")
        self.target_reads_after_create += 1
        if self.target_reads_after_create <= self.readback_404_count:
            raise Exception("404 Branch not found")
        return SimpleNamespace(commit=SimpleNamespace(sha="source-sha"))

    def create_git_ref(self, ref, sha):
        self.created_ref = (ref, sha)


def test_create_details_retries_transient_branch_readback_404(monkeypatch):
    repo = EventuallyVisibleBranchRepo(readback_404_count=2)
    sleeps = []
    monkeypatch.setattr(github_client, "get_github_client", lambda: FakeGithub(repo))
    monkeypatch.setattr(github_client.time, "sleep", sleeps.append)

    details = github_client.create_or_reuse_branch_details(
        repo_name="bankingscience/BSLAgenticQuantDevLoop",
        branch_name="quant/SCRUM-180",
        base_branch="exp2/source",
    )

    assert details == {
        "branch_action": "created",
        "message": "Created branch quant/SCRUM-180 from exp2/source",
        "commit_sha": "source-sha",
    }
    assert repo.created_ref == ("refs/heads/quant/SCRUM-180", "source-sha")
    assert sleeps == [0.25, 0.5]


def test_create_details_reports_repo_source_target_after_retry_exhaustion(monkeypatch):
    repo = EventuallyVisibleBranchRepo(readback_404_count=10)
    sleeps = []
    monkeypatch.setattr(github_client, "get_github_client", lambda: FakeGithub(repo))
    monkeypatch.setattr(github_client.time, "sleep", sleeps.append)

    with pytest.raises(
        github_client.GithubOperationError,
        match=(
            r"confirm_branch_readback\(repo=bankingscience/BSLAgenticQuantDevLoop, "
            r"source=exp2/source, target=quant/SCRUM-180, attempt=5/5\)"
        ),
    ):
        github_client.create_or_reuse_branch_details(
            repo_name="bankingscience/BSLAgenticQuantDevLoop",
            branch_name="quant/SCRUM-180",
            base_branch="exp2/source",
        )

    assert sleeps == [0.25, 0.5, 1.0, 2.0]


def test_get_branch_sha_does_not_retry_non_404(monkeypatch):
    class FailingRepo:
        def get_branch(self, name):
            raise Exception("500 upstream failure")

    monkeypatch.setattr(
        github_client,
        "get_github_client",
        lambda: FakeGithub(FailingRepo()),
    )

    with pytest.raises(
        github_client.GithubOperationError,
        match=(
            r"get_branch_sha\(repo=bankingscience/BSLAgenticQuantDevLoop, "
            r"branch=quant/SCRUM-180\) failed"
        ),
    ):
        github_client.get_branch_sha(
            "quant/SCRUM-180",
            repo_name="bankingscience/BSLAgenticQuantDevLoop",
        )
