"""Credential identity checks for user-scoped GitHub operations."""

import pytest

import github_client


class FakeAuth:
    @staticmethod
    def Token(token):
        return token


def test_get_github_client_verifies_pat_owner_once(monkeypatch):
    class AuthenticatedUser:
        login = "student-gh"

    class AuthenticatedGithub:
        def __init__(self):
            self.user_checks = 0

        def get_user(self):
            self.user_checks += 1
            return AuthenticatedUser()

    client = AuthenticatedGithub()
    monkeypatch.setattr(github_client, "Auth", FakeAuth)
    monkeypatch.setattr(github_client, "Github", lambda auth: client)
    monkeypatch.setattr(github_client, "_VERIFIED_CREDENTIAL_IDENTITIES", set())
    monkeypatch.setenv("GITHUB_TOKEN", "student-token")
    monkeypatch.setenv("GITHUB_USERNAME", "Student-GH")

    assert github_client.get_github_client() is client
    assert github_client.get_github_client() is client
    assert client.user_checks == 1


def test_get_github_client_rejects_pat_for_different_user(monkeypatch):
    class AuthenticatedGithub:
        def get_user(self):
            return type("AuthenticatedUser", (), {"login": "Rah9742"})()

    monkeypatch.setattr(github_client, "Auth", FakeAuth)
    monkeypatch.setattr(github_client, "Github", lambda auth: AuthenticatedGithub())
    monkeypatch.setattr(github_client, "_VERIFIED_CREDENTIAL_IDENTITIES", set())
    monkeypatch.setenv("GITHUB_TOKEN", "wrong-user-token")
    monkeypatch.setenv("GITHUB_USERNAME", "student-gh")

    with pytest.raises(
        RuntimeError,
        match="configured user 'student-gh' but the PAT authenticates as 'Rah9742'",
    ):
        github_client.get_github_client()
