"""Jira comment write-back client independent of FastAPI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests
from requests.auth import HTTPBasicAuth


@dataclass(frozen=True)
class JiraPostResult:
    """Normalized result returned after posting a Jira comment."""

    issue_key: str
    comment_id: str | None
    response: dict[str, Any]


class JiraClient:
    """Post Atlassian Document Format comments to Jira Cloud."""

    def __init__(
        self,
        base_url: str,
        email: str,
        api_token: str,
        timeout: int = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = HTTPBasicAuth(email, api_token)
        self.timeout = timeout

    def post_comment(
        self,
        issue_key: str,
        adf_body: dict[str, Any],
    ) -> JiraPostResult:
        """Post an ADF document to the supplied Jira issue."""

        response = requests.post(
            f"{self.base_url}/rest/api/3/issue/{issue_key}/comment",
            auth=self.auth,
            json={"body": adf_body},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()

        return JiraPostResult(
            issue_key=issue_key,
            comment_id=str(data["id"]) if data.get("id") else None,
            response=data,
        )
