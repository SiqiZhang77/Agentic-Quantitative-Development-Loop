from __future__ import annotations

from unittest.mock import Mock

from jira_client.client import JiraClient


def test_post_comment_sends_adf_to_jira(monkeypatch) -> None:
    response = Mock()
    response.json.return_value = {"id": "20001"}
    post = Mock(return_value=response)
    monkeypatch.setattr("jira_client.client.requests.post", post)
    adf = {"type": "doc", "version": 1, "content": []}

    result = JiraClient(
        base_url="https://example.atlassian.net/",
        email="airflow@example.com",
        api_token="secret",
    ).post_comment("ALPHA-101", adf)

    post.assert_called_once()
    call = post.call_args
    assert call.args[0] == (
        "https://example.atlassian.net/rest/api/3/"
        "issue/ALPHA-101/comment"
    )
    assert call.kwargs["json"] == {"body": adf}
    assert call.kwargs["timeout"] == 30
    response.raise_for_status.assert_called_once_with()
    assert result.issue_key == "ALPHA-101"
    assert result.comment_id == "20001"
