"""Shared Jira and sandbox helpers for the quant Airflow DAGs."""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

import requests
try:
    from .adf_utils.builder import build_runtime_workflow_report_adf
except ImportError:
    from adf_utils.builder import build_runtime_workflow_report_adf
try:
    from airflow.providers.http.hooks.http import HttpHook
except ImportError:
    HttpHook = None
try:
    from airflow.sdk import BaseHook, Variable
except ImportError:
    from airflow.hooks.base import BaseHook
    try:
        from airflow.models import Variable
    except ImportError:
        from airflow.sdk import Variable
from requests.auth import HTTPBasicAuth


BOT_MARKER = "[quant-loop-bot]"
LEGACY_BOT_RESULT_HEADER = "Automated quant loop result"
COMMAND_MARKER = "/quant"
DEFAULT_JIRA_CONNECTION_ID = "jira_cloud"
DEFAULT_LITELLM_CONNECTION_ID = "litellm_default"
DEFAULT_OPENAI_CONNECTION_ID = "openai_default"
GITHUB_USER_ACCESS_MATRIX_VARIABLE = "QUANT_GITHUB_USER_ACCESS_MATRIX"

_LOCAL_ENV_LOADED = False
_AIRFLOW_VARIABLE_NAMESPACE: ContextVar[str] = ContextVar(
    "airflow_variable_namespace",
    default="",
)
_VARIABLE_NAMESPACE_RE = re.compile(r"^[A-Z][A-Z0-9_]*_$")


def load_local_env() -> None:
    """Load a local .env file for non-Airflow development runs."""

    global _LOCAL_ENV_LOADED
    if _LOCAL_ENV_LOADED:
        return

    env_path = Path(__file__).parents[1] / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue

            key, value = stripped.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

    _LOCAL_ENV_LOADED = True


def get_airflow_variable(name: str, default: str | None = None) -> str | None:
    """Resolve non-sensitive config from Airflow Variables or local env.

    Candidate deployments may activate a bounded namespace such as
    ``EXP2_SI_``.  A namespaced value takes precedence when present; otherwise
    the shared setting remains available for deliberately reused connections
    and non-experiment-specific configuration.
    """

    load_local_env()
    namespace = _AIRFLOW_VARIABLE_NAMESPACE.get()
    if namespace:
        namespaced_name = f"{namespace}{name}"
        namespaced_value = Variable.get(
            namespaced_name,
            os.environ.get(namespaced_name),
        )
        if namespaced_value is not None:
            return namespaced_value
    return Variable.get(name, os.environ.get(name, default))


@contextmanager
def airflow_variable_namespace(prefix: str) -> Iterator[None]:
    """Temporarily prefer an approved Airflow Variable prefix in one task.

    ``ContextVar`` keeps the override local to the current task context.  The
    production DAG never enters this context and therefore continues to read
    the original unprefixed settings.
    """

    normalized = str(prefix or "").strip().upper()
    if not _VARIABLE_NAMESPACE_RE.fullmatch(normalized):
        raise ValueError(
            "Airflow Variable namespace must be uppercase alphanumerics/underscores "
            "and end with an underscore"
        )
    token = _AIRFLOW_VARIABLE_NAMESPACE.set(normalized)
    try:
        yield
    finally:
        _AIRFLOW_VARIABLE_NAMESPACE.reset(token)


def require_airflow_variable(name: str) -> str:
    value = get_airflow_variable(name)
    if not value:
        raise ValueError(
            f"Missing {name}: configure Airflow Variable {name} or set local "
            f"environment variable {name}"
        )
    return value


def get_airflow_connection(conn_id: str):
    """Resolve an Airflow Connection by id.

    Airflow 3.2 task-runtime hooks and CLI/database access can see different
    backends in this cluster, so try BaseHook first and fall back to the
    metadata DB Connection row used by the CLI.
    """

    try:
        return BaseHook.get_connection(conn_id)
    except Exception:
        from airflow.models.connection import Connection
        from airflow.settings import Session

        session = Session()
        try:
            conn = (
                session.query(Connection)
                .filter(Connection.conn_id == conn_id)
                .one_or_none()
            )
            if conn is None:
                raise
            session.expunge(conn)
            return conn
        finally:
            session.close()


def get_jira_connection_id() -> str:
    return (
        get_airflow_variable("JIRA_CONNECTION_ID", DEFAULT_JIRA_CONNECTION_ID)
        or DEFAULT_JIRA_CONNECTION_ID
    )


def get_litellm_connection_id() -> str:
    return (
        get_airflow_variable("LITELLM_CONNECTION_ID", DEFAULT_LITELLM_CONNECTION_ID)
        or DEFAULT_LITELLM_CONNECTION_ID
    )


def get_openai_connection_id() -> str:
    return (
        get_airflow_variable("OPENAI_CONNECTION_ID", DEFAULT_OPENAI_CONNECTION_ID)
        or DEFAULT_OPENAI_CONNECTION_ID
    )


def get_jira_config() -> dict[str, str | bool]:
    """Resolve Jira config from an Airflow Connection and project Variable."""

    load_local_env()
    try:
        conn = get_airflow_connection(get_jira_connection_id())
        base_url = (conn.host or "").rstrip("/")
        email = conn.login or ""
        api_token = conn.password or ""
        uses_connection = True
    except Exception:
        base_url = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
        email = os.environ.get("JIRA_EMAIL", "")
        api_token = os.environ.get("JIRA_API_TOKEN", "")
        uses_connection = False

    project_key = require_airflow_variable("JIRA_PROJECT_KEY")

    missing = [
        name
        for name, value in {
            "host": base_url,
            "login": email,
            "password": api_token,
            "JIRA_PROJECT_KEY": project_key,
        }.items()
        if not value
    ]
    if missing:
        raise ValueError(
            f"Jira connection/config is missing required field(s): "
            f"{', '.join(missing)}"
        )

    return {
        "base_url": base_url,
        "email": email,
        "api_token": api_token,
        "project_key": project_key,
        "uses_connection": uses_connection,
    }


def get_connection_password(conn_id: str, env_name: str | None = None) -> str:
    """Resolve a credential from an Airflow Connection password or local env."""

    load_local_env()
    try:
        conn = get_airflow_connection(conn_id)
        extra = getattr(conn, "extra_dejson", {}) or {}
        password = conn.password or extra.get("api_key") or extra.get("token")
    except Exception:
        password = None

    value = password or (os.environ.get(env_name) if env_name else None)
    if not value:
        raise ValueError(
            f"Missing credential: configure Airflow Connection {conn_id}"
            + (f" or set local environment variable {env_name}" if env_name else "")
        )

    return value


def get_optional_github_credentials(
    connection_id: str | None = None,
) -> dict[str, str]:
    """Return GitHub env vars only when a connection/env fallback is configured."""

    load_local_env()
    conn_id = connection_id or get_airflow_variable("GITHUB_CONNECTION_ID")

    if conn_id:
        conn = get_airflow_connection(conn_id)
        credentials = {}
        if conn.login:
            credentials["GITHUB_USERNAME"] = conn.login
        if conn.password:
            credentials["GITHUB_TOKEN"] = conn.password
        if not credentials.get("GITHUB_TOKEN"):
            raise ValueError(
                f"GitHub Connection {conn_id} must store the token in Password"
            )
        return credentials

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return {}

    credentials = {"GITHUB_TOKEN": token}
    username = os.environ.get("GITHUB_USERNAME")
    if username:
        credentials["GITHUB_USERNAME"] = username
    return credentials


def normalize_identity_value(value: Any) -> str:
    return str(value or "").strip().lower()


def load_github_user_access_matrix(
    variable_name: str = GITHUB_USER_ACCESS_MATRIX_VARIABLE,
) -> list[dict[str, Any]]:
    """Load non-secret Jira-user to GitHub-credential authorization rows."""

    raw = get_airflow_variable(variable_name)
    if not raw:
        return []

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Airflow Variable {variable_name} must contain valid JSON: {exc.msg}"
        ) from exc

    rows = parsed.get("users") if isinstance(parsed, dict) else parsed
    if not isinstance(rows, list):
        raise ValueError(
            f"Airflow Variable {variable_name} must be a JSON list or an object "
            "with a users list."
        )

    normalized_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"{variable_name} row {index + 1} must be an object.")

        github_username = str(row.get("github_username") or "").strip()
        connection_id = str(
            row.get("github_connection_id")
            or row.get("secret_name")
            or row.get("github_pat_secret_name")
            or ""
        ).strip()
        repositories = [
            str(repo).strip()
            for repo in (
                row.get("authorized_repositories")
                or row.get("authorised_repositories")
                or row.get("repositories")
                or row.get("authorized_repo")
                or row.get("authorised_repo")
                or []
            )
            if str(repo).strip()
        ]
        if isinstance(
            row.get("authorized_repositories")
            or row.get("authorised_repositories")
            or row.get("repositories")
            or row.get("authorized_repo")
            or row.get("authorised_repo"),
            str,
        ):
            raw_repos = (
                row.get("authorized_repositories")
                or row.get("authorised_repositories")
                or row.get("repositories")
                or row.get("authorized_repo")
                or row.get("authorised_repo")
            )
            repositories = [
                part.strip()
                for part in re.split(r"[,;]", str(raw_repos))
                if part.strip()
            ]

        identity = row.get("jira") if isinstance(row.get("jira"), dict) else {}
        jira_email = row.get("jira_email") or identity.get("email")
        jira_account_id = row.get("jira_account_id") or identity.get("account_id")
        jira_display_name = (
            row.get("jira_display_name")
            or row.get("jira_username")
            or identity.get("display_name")
            or identity.get("username")
        )
        display_aliases = row.get("jira_display_aliases") or row.get("display_aliases") or []
        if isinstance(display_aliases, str):
            display_aliases = [
                part.strip() for part in re.split(r"[,;]", display_aliases) if part.strip()
            ]

        normalized_rows.append(
            {
                "full_name": str(row.get("full_name") or "").strip(),
                "jira_email": str(jira_email or "").strip(),
                "jira_account_id": str(jira_account_id or "").strip(),
                "jira_display_name": str(jira_display_name or "").strip(),
                "jira_display_aliases": [
                    str(alias).strip() for alias in display_aliases if str(alias).strip()
                ],
                "github_username": github_username,
                "github_connection_id": connection_id,
                "authorized_repositories": repositories,
                "permission_level": str(row.get("permission_level") or "").strip(),
            }
        )

    return normalized_rows


def match_github_user_access_row(
    requester: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    email = normalize_identity_value(requester.get("emailAddress") or requester.get("email"))
    account_id = normalize_identity_value(
        requester.get("accountId") or requester.get("account_id")
    )
    display_name = normalize_identity_value(
        requester.get("displayName")
        or requester.get("display_name")
        or requester.get("author")
    )

    if email:
        for row in rows:
            if normalize_identity_value(row.get("jira_email")) == email:
                return row
    if account_id:
        for row in rows:
            if normalize_identity_value(row.get("jira_account_id")) == account_id:
                return row
    if display_name:
        for row in rows:
            names = [row.get("jira_display_name"), *(row.get("jira_display_aliases") or [])]
            if display_name in {normalize_identity_value(name) for name in names if name}:
                return row
    return None


def resolve_user_scoped_github_credentials(
    requester: dict[str, Any],
    repositories: list[dict[str, Any]],
    *,
    matrix: list[dict[str, Any]] | None = None,
    explicitly_read_only: bool = False,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Authorize a Jira requester and return sandbox GitHub credentials.

    Token values are returned only in the credentials dict for environment
    injection. The metadata dict is intentionally token-free and log-safe.
    """

    rows = load_github_user_access_matrix() if matrix is None else matrix
    row = match_github_user_access_row(requester, rows)
    if row is None:
        raise ValueError("No GitHub credential mapping configured for Jira user")

    permission_level = normalize_identity_value(row.get("permission_level"))
    if permission_level not in {"read", "write"}:
        raise ValueError(
            "GitHub permission_level must be configured as 'read' or 'write'"
        )
    if permission_level == "read" and not explicitly_read_only:
        raise ValueError(
            "Jira user has read-only GitHub permission; set read_only=true or "
            "zero_code_modifications=true explicitly"
        )

    github_username = str(row.get("github_username") or "").strip()
    if not github_username:
        raise ValueError("GitHub username is not configured for Jira user")

    authorized_repos = {
        normalize_identity_value(repo)
        for repo in (row.get("authorized_repositories") or [])
    }
    requested_repos = [
        repo.get("repo_full_name")
        for repo in repositories
        if isinstance(repo, dict) and repo.get("repo_full_name")
    ]
    for repo_full_name in requested_repos:
        if normalize_identity_value(repo_full_name) not in authorized_repos:
            raise ValueError("Jira user is not authorised for requested repository")

    connection_id = str(row.get("github_connection_id") or "").strip()
    if not connection_id:
        raise ValueError("GitHub token secret is not configured")

    try:
        credentials = get_optional_github_credentials(connection_id)
    except Exception as exc:
        raise ValueError("GitHub token secret is not configured") from exc
    if not credentials.get("GITHUB_TOKEN"):
        raise ValueError("GitHub token secret is not configured")
    credentials["GITHUB_USERNAME"] = github_username

    metadata = {
        "jira_user": {
            "email": requester.get("emailAddress") or requester.get("email"),
            "account_id": requester.get("accountId") or requester.get("account_id"),
            "display_name": requester.get("displayName")
            or requester.get("display_name")
            or requester.get("author"),
        },
        "github_username": github_username,
        "github_connection_id": connection_id,
        "requested_repositories": requested_repos,
        "permission_level": permission_level,
        "token_present": bool(credentials.get("GITHUB_TOKEN")),
        "token_length": len(credentials.get("GITHUB_TOKEN") or ""),
    }
    return credentials, metadata


def verify_user_github_access(
    credentials: dict[str, str],
    expected_username: str,
    repositories: list[dict[str, Any]],
) -> None:
    """Verify PAT identity and source-branch access without exposing credentials."""

    token = credentials.get("GITHUB_TOKEN") or ""
    if not token:
        raise ValueError("GitHub token secret is not configured")

    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        identity_response = requests.get(
            "https://api.github.com/user", headers=headers, timeout=15
        )
    except requests.RequestException as exc:
        raise ValueError(
            f"Could not verify GitHub credential identity: {type(exc).__name__}"
        ) from exc
    if identity_response.status_code >= 400:
        raise ValueError("GitHub credential could not authenticate the configured user")
    try:
        actual_username = str(identity_response.json().get("login") or "").strip()
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("GitHub credential identity response was invalid") from exc
    if normalize_identity_value(actual_username) != normalize_identity_value(
        expected_username
    ):
        raise ValueError(
            "GitHub credential identity does not match configured GitHub username"
        )

    for repository in repositories:
        repo_full_name = str(repository.get("repo_full_name") or "").strip()
        source_branch = str(repository.get("source_branch") or "").strip()
        branch_url = (
            f"https://api.github.com/repos/{repo_full_name}/branches/"
            f"{quote(source_branch, safe='')}"
        )
        try:
            response = requests.get(branch_url, headers=headers, timeout=15)
        except requests.RequestException as exc:
            raise ValueError(
                "Could not verify GitHub repository/source branch access: "
                f"{type(exc).__name__}"
            ) from exc
        if response.status_code >= 400:
            raise ValueError(
                "GitHub credential cannot access requested repository/source branch "
                f"'{repo_full_name}@{source_branch}'"
            )


def get_optional_ghcr_credentials(
    connection_id: str | None = None,
) -> dict[str, str]:
    """Return GHCR pull credentials from an Airflow Connection or local env."""

    load_local_env()
    conn_id = connection_id or get_airflow_variable("GHCR_CONNECTION_ID")

    if conn_id:
        conn = get_airflow_connection(conn_id)
        credentials = {}
        if conn.login:
            credentials["GHCR_USERNAME"] = conn.login
        if conn.password:
            credentials["GHCR_TOKEN"] = conn.password
        missing = [
            field
            for field, value in {
                "Login": credentials.get("GHCR_USERNAME"),
                "Password": credentials.get("GHCR_TOKEN"),
            }.items()
            if not value
        ]
        if missing:
            raise ValueError(
                f"GHCR Connection {conn_id} is missing required field(s): "
                f"{', '.join(missing)}"
            )
        return credentials

    token = os.environ.get("GHCR_TOKEN")
    username = os.environ.get("GHCR_USERNAME")
    if not token or not username:
        return {}

    return {"GHCR_USERNAME": username, "GHCR_TOKEN": token}


def get_litellm_api_key() -> str:
    return get_connection_password(get_litellm_connection_id(), "LITELLM_API_KEY")


def normalize_connection_url(conn) -> str:
    host = (conn.host or "").rstrip("/")
    if not host:
        return ""

    if "://" in host:
        return host

    scheme = conn.schema or "http"
    return f"{scheme}://{host}"


def get_litellm_config() -> dict[str, str]:
    """Resolve LiteLLM API key, base URL, and model from Airflow config."""

    load_local_env()
    conn_id = get_litellm_connection_id()
    try:
        conn = get_airflow_connection(conn_id)
        extra = getattr(conn, "extra_dejson", {}) or {}
        api_key = conn.password or extra.get("api_key") or extra.get("token")
        base_url = normalize_connection_url(conn) or extra.get("base_url")
        model = extra.get("model")
    except Exception:
        api_key = None
        base_url = None
        model = None

    api_key = api_key or os.environ.get("LITELLM_API_KEY")
    base_url = (
        base_url
        or get_airflow_variable("LITELLM_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or os.environ.get("OPENAI_API_BASE")
    )
    model = model or get_airflow_variable("LITELLM_MODEL", "nova-micro")

    missing = [
        name
        for name, value in {
            "api_key": api_key,
            "base_url": base_url,
            "model": model,
        }.items()
        if not value
    ]
    if missing:
        raise ValueError(
            f"LiteLLM connection/config is missing required field(s): "
            f"{', '.join(missing)}"
        )

    return {
        "api_key": api_key,
        "base_url": base_url.rstrip("/"),
        "model": model,
    }


def get_openai_config() -> dict[str, str]:
    """Resolve native OpenAI config without falling back to LiteLLM secrets."""

    load_local_env()
    conn_id = get_openai_connection_id()
    try:
        conn = get_airflow_connection(conn_id)
        extra = getattr(conn, "extra_dejson", {}) or {}
        api_key = conn.password or extra.get("api_key") or extra.get("token")
        base_url = normalize_connection_url(conn) or extra.get("base_url")
        model = extra.get("model")
    except Exception:
        api_key = None
        base_url = None
        model = None

    api_key = api_key or os.environ.get("OPENAI_API_KEY")
    base_url = (
        base_url
        or get_airflow_variable("OPENAI_BASE_URL", "https://api.openai.com/v1")
        or "https://api.openai.com/v1"
    )
    model = model or get_airflow_variable("OPENAI_MODEL")

    missing = [
        name
        for name, value in {
            "api_key": api_key,
            "base_url": base_url,
            "model": model,
        }.items()
        if not value
    ]
    if missing:
        raise ValueError(
            "OpenAI connection/config is missing required field(s): "
            f"{', '.join(missing)}"
        )

    return {
        "api_key": str(api_key),
        "base_url": str(base_url).rstrip("/"),
        "model": str(model).strip(),
    }


def jira_request(method: str, endpoint: str, **kwargs):
    """Run a Jira HTTP request through Airflow's HttpHook connection."""

    config = get_jira_config()
    headers = kwargs.pop("headers", {})
    url = f"{config['base_url']}/{endpoint.lstrip('/')}"
    timeout = kwargs.pop("timeout", 30)

    if config["uses_connection"]:
        if HttpHook is None:
            raise RuntimeError(
                "apache-airflow-providers-http is required for Jira Connection "
                "requests"
            )
        hook = HttpHook(method=method, http_conn_id=get_jira_connection_id())
        session = hook.get_conn(headers=headers)
        session.auth = HTTPBasicAuth(config["email"], config["api_token"])
        return session.request(method, url, timeout=timeout, **kwargs)

    return requests.request(
        method,
        url,
        auth=HTTPBasicAuth(config["email"], config["api_token"]),
        headers=headers,
        timeout=timeout,
        **kwargs,
    )


def normalize_jira_attachments(value: Any) -> list[dict[str, Any]]:
    """Keep the non-secret Jira metadata needed to fetch issue attachments later."""

    if not isinstance(value, list):
        return []

    attachments: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        attachment_id = item.get("id")
        filename = item.get("filename")
        if attachment_id is None or not isinstance(filename, str):
            continue
        attachments.append(
            {
                "id": str(attachment_id),
                "filename": filename,
                "size": item.get("size"),
                "mime_type": item.get("mimeType"),
                "created": item.get("created"),
            }
        )
    return attachments


def adf_text_node(text: str) -> dict[str, Any]:
    return {
        "type": "doc",
        "version": 1,
        "content": [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": text}],
            }
        ],
    }


def extract_plain_text_from_adf(node: Any) -> str:
    """Render Jira ADF into command-safe plain text.

    Jira comments can wrap commands in paragraphs, lists, tables, mentions, or
    code blocks. Keep meaningful block boundaries so multiline commands survive
    normalization before validation/execution.
    """

    def render(current: Any) -> str:
        if isinstance(current, dict):
            node_type = current.get("type")

            if node_type == "text":
                return str(current.get("text", ""))
            if node_type == "hardBreak":
                return "\n"
            if node_type == "mention":
                attrs = current.get("attrs") or {}
                return str(
                    attrs.get("text")
                    or attrs.get("displayName")
                    or attrs.get("id")
                    or ""
                )
            if node_type in {"emoji", "status"}:
                attrs = current.get("attrs") or {}
                return str(attrs.get("text") or attrs.get("shortName") or "")
            if node_type in {"inlineCard", "blockCard", "embedCard"}:
                attrs = current.get("attrs") or {}
                return str(attrs.get("url") or "")

            content = current.get("content", [])

            if node_type in {
                "doc",
                "paragraph",
                "heading",
                "blockquote",
                "panel",
                "expand",
                "nestedExpand",
                "bodiedExtension",
                "extension",
                "tableHeader",
            }:
                separator = "\n" if node_type != "paragraph" else ""
                return join_non_empty((render(child) for child in content), separator)

            if node_type == "codeBlock":
                return join_non_empty((render(child) for child in content), "")

            if node_type in {"bulletList", "orderedList", "taskList"}:
                return join_non_empty((render(child) for child in content), "\n")

            if node_type in {"listItem", "taskItem"}:
                return join_non_empty((render(child) for child in content), "\n")

            if node_type == "table":
                return join_non_empty((render(child) for child in content), "\n")

            if node_type == "tableRow":
                return join_non_empty((render(child) for child in content), "\n")

            if node_type == "tableCell":
                return join_non_empty((render(child) for child in content), "\n")

            if "text" in current and not content:
                return str(current.get("text") or "")

            return join_non_empty((render(child) for child in content), "")

        if isinstance(current, list):
            return join_non_empty((render(child) for child in current), "\n")

        return ""

    def join_non_empty(parts: Any, separator: str) -> str:
        rendered = [part for part in parts if part]
        return separator.join(rendered)

    return normalize_adf_plain_text(render(node))


def normalize_adf_plain_text(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    normalized_lines: list[str] = []

    for line in lines:
        if line or (normalized_lines and normalized_lines[-1]):
            normalized_lines.append(line)

    while normalized_lines and not normalized_lines[-1]:
        normalized_lines.pop()

    return "\n".join(normalized_lines)


_TICKET_SECRET_PATTERNS = (
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|token|auth(?:orization)?|password|secret)"
        r"([\"']?\s*[:=]\s*[\"']?)(?:bearer\s+)?[^\s,;\"'}]+"
    ),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
)


def sanitize_ticket_text(value: Any, max_chars: int) -> str:
    """Redact common credential shapes and bound Jira-authored prompt content."""

    text = normalize_adf_plain_text(str(value or ""))
    for pattern in _TICKET_SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(r"\1\2[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED]", text)

    marker = "\n[TRUNCATED]"
    if len(text) > max_chars:
        text = text[: max_chars - len(marker)].rstrip() + marker
    return text


def is_automated_result_comment(text: str) -> bool:
    stripped_text = text.strip()
    return (
        BOT_MARKER in stripped_text
        or stripped_text.startswith(LEGACY_BOT_RESULT_HEADER)
    )


def extract_quant_request(text: str) -> str | None:
    stripped_text = text.strip()
    parts = stripped_text.split(maxsplit=1)
    if len(parts) != 2 or parts[0] != COMMAND_MARKER:
        return None

    request_text = parts[1].strip()
    return request_text or None


def post_jira_comment(issue_key: str, text: str) -> None:
    post_jira_adf_comment(issue_key, adf_text_node(text))


def post_jira_adf_comment(issue_key: str, adf_body: dict[str, Any]) -> None:
    response = jira_request(
        "POST",
        f"/rest/api/3/issue/{issue_key}/comment",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
        json={"body": adf_body},
        timeout=30,
    )

    print(f"Jira writeback status for {issue_key}: {response.status_code}")
    print(response.text[:1000])
    response.raise_for_status()


def post_jira_attachments(
    issue_key: str,
    file_paths: list[Path],
) -> list[dict[str, Any]]:
    """Upload files to Jira as issue attachments."""

    if not file_paths:
        return []

    handles = []
    try:
        files = []
        for file_path in file_paths:
            handle = file_path.open("rb")
            handles.append(handle)
            files.append(("file", (file_path.name, handle)))

        response = jira_request(
            "POST",
            f"/rest/api/3/issue/{issue_key}/attachments",
            headers={
                "Accept": "application/json",
                "X-Atlassian-Token": "no-check",
            },
            files=files,
            timeout=60,
        )
    finally:
        for handle in handles:
            handle.close()

    print(f"Jira attachment upload status for {issue_key}: {response.status_code}")
    print(response.text[:1000])
    response.raise_for_status()

    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("Jira attachment response must be a JSON list")

    return [
        {
            "id": str(item.get("id") or ""),
            "filename": str(item.get("filename") or ""),
            "size": item.get("size"),
            "content": item.get("content"),
        }
        for item in payload
        if isinstance(item, dict)
    ]


def parse_final_json_stdout(stdout: Any) -> dict[str, Any]:
    """Parse JSON from the final non-empty line emitted by the sandbox."""

    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8")

    if isinstance(stdout, str):
        chunks = [stdout]
    elif isinstance(stdout, (list, tuple)):
        chunks = [
            chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
            for chunk in stdout
        ]
    else:
        raise TypeError(f"Unsupported Docker stdout type: {type(stdout).__name__}")

    lines = [
        line.strip()
        for chunk in chunks
        for line in chunk.splitlines()
        if line.strip()
    ]
    if not lines:
        raise ValueError("Sandbox produced no non-empty stdout lines")

    try:
        result = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Sandbox final non-empty stdout line is not valid JSON"
        ) from exc

    if not isinstance(result, dict):
        raise ValueError("Sandbox result JSON must be an object")

    return result


def format_result_comment(result: dict[str, Any]) -> str:
    execution_summary = result.get("execution_summary") or {}
    diagnostics = result.get("diagnostics") or {}
    metrics = result.get("performance_metrics") or result.get("metrics") or {}
    artifacts = result.get("generated_artifacts") or result.get("artifacts") or {}

    status = execution_summary.get("status") or result.get("status")
    command = result.get("command") or result.get("run_id")
    workflow_id = result.get("workflow_id")
    workflow_id_line = f"Workflow ID: {workflow_id}\n" if workflow_id else ""
    request_type = execution_summary.get("request_type")
    request_type_line = f"Request Type: {request_type}\n" if request_type else ""
    summary = (
        diagnostics.get("error_message")
        or result.get("summary")
        or latest_iteration_message(execution_summary.get("iteration_traces"))
    )

    modified_files = artifacts.get("modified_files")
    new_files = artifacts.get("new_files")
    modified_files_text = format_list_value(modified_files) or artifacts.get(
        "changed_file"
    )
    backtest_plot = artifacts.get("backtest_plots_path") or artifacts.get(
        "equity_curve"
    )
    artifact_ingestion = result.get("artifact_ingestion") or {}
    uploaded_artifacts = artifact_ingestion.get("uploaded") or []
    missing_artifacts = artifact_ingestion.get("missing") or []
    skipped_artifacts = artifact_ingestion.get("skipped") or []
    ingestion_section = format_artifact_ingestion_section(
        uploaded_artifacts,
        missing_artifacts,
        skipped_artifacts,
    )

    # Metrics/backtest-plot lines only apply to runs that actually ran a backtest;
    # rendering them unconditionally would print "Sharpe ratio: None" etc. on every
    # non-backtest ticket (refactor/analysis/ingestion/other).
    metrics_section = (
        "\n\nMetrics:\n"
        f"- Sharpe ratio: {metrics.get('sharpe_ratio')}\n"
        f"- Max drawdown: {metrics.get('max_drawdown')}\n"
        f"- Total return: {metrics.get('total_return')}"
    ) if metrics else ""
    backtest_plot_line = f"\n- Backtest plot: {backtest_plot}" if backtest_plot else ""

    return (
        f"{BOT_MARKER}\n"
        "Automated quant loop result\n\n"
        f"Status: {status}\n"
        f"Command: {command}\n"
        f"{workflow_id_line}"
        f"{request_type_line}"
        f"Summary: {summary}"
        f"{metrics_section}\n\n"
        "Artifacts:\n"
        f"- Modified files: {modified_files_text}\n"
        f"- New files: {format_list_value(new_files)}"
        f"{backtest_plot_line}"
        f"{ingestion_section}"
    )


def format_result_comment_adf(result: dict[str, Any]) -> dict[str, Any]:
    """Build the structured Jira ADF workflow report for a runtime response."""

    return build_runtime_workflow_report_adf(result)


def latest_iteration_message(iteration_traces: Any) -> str | None:
    if not isinstance(iteration_traces, list) or not iteration_traces:
        return None

    latest_trace = iteration_traces[-1]
    if not isinstance(latest_trace, dict):
        return None

    return latest_trace.get("message")


def format_list_value(value: Any) -> str | None:
    if not isinstance(value, list):
        return None
    if not value:
        return "None"
    return ", ".join(str(item) for item in value)


def format_artifact_ingestion_section(
    uploaded: list[Any],
    missing: list[Any],
    skipped: list[Any],
) -> str:
    if not uploaded and not missing and not skipped:
        return ""

    lines = ["", "", "Ingested artifacts:"]
    if uploaded:
        for item in uploaded:
            if not isinstance(item, dict):
                continue
            filename = item.get("filename") or item.get("source_path") or "artifact"
            size = item.get("size_bytes")
            attachment_id = item.get("jira_attachment_id") or item.get("id")
            content = item.get("jira_attachment_url") or item.get("content")
            details = []
            if size is not None:
                details.append(f"{size} bytes")
            if attachment_id:
                details.append(f"attachment {attachment_id}")
            if content:
                details.append(str(content))
            suffix = f" ({', '.join(details)})" if details else ""
            lines.append(f"- Uploaded: {filename}{suffix}")
    else:
        lines.append("- Uploaded: None")

    for label, items in (("Missing", missing), ("Skipped", skipped)):
        for item in items:
            if not isinstance(item, dict):
                continue
            path = item.get("path") or item.get("source_path") or "artifact"
            reason = item.get("reason")
            suffix = f" ({reason})" if reason else ""
            lines.append(f"- {label}: {path}{suffix}")

    return "\n".join(lines)


def format_failure_comment(
    issue_key: str,
    error: Exception,
    dag_run_id: str,
    workflow_id: str | None = None,
) -> str:
    workflow_id_line = f"Workflow ID: {workflow_id}\n" if workflow_id else ""
    return (
        f"{BOT_MARKER}\n"
        "Automated quant loop result\n\n"
        "Status: failed\n"
        "Command: docker_sandbox_runner\n"
        f"{workflow_id_line}"
        f"Summary: Sandbox execution failed for {issue_key}.\n\n"
        f"Airflow DAG run: {dag_run_id}\n"
        f"Error type: {type(error).__name__}"
    )


def format_validation_failure_comment(
    issue_key: str,
    errors: list[str],
    dag_run_id: str,
    workflow_id: str | None = None,
) -> str:
    error_lines = "\n".join(f"- {error}" for error in errors)
    workflow_id_line = f"Workflow ID: {workflow_id}\n" if workflow_id else ""
    return (
        f"{BOT_MARKER}\n"
        "Automated quant loop result\n\n"
        "Status: validation_failed\n"
        "Command: /quant\n"
        f"{workflow_id_line}"
        f"Summary: Jira command validation failed for {issue_key}.\n\n"
        "Fix the command and add a new Jira comment. Command shape:\n"
        "/quant Backtest the strategy strategy_type=backtest\n"
        "Optional date overrides (omitting them inherits strategy.request): "
        "start_date=YYYY-MM-DD end_date=YYYY-MM-DD\n\n"
        "Validation errors:\n"
        f"{error_lines}\n\n"
        f"Airflow DAG run: {dag_run_id}"
    )
