import hashlib
import os
import time
from dotenv import load_dotenv
try:
    from github import Github, Auth
except ImportError:  # pragma: no cover - exercised only in lightweight local envs
    Github = None
    Auth = None
from pathlib import Path

load_dotenv()

REPO_NAME = "bankingscience/BSLAgenticQuantDevLoop"
_VERIFIED_CREDENTIAL_IDENTITIES: set[tuple[str, str]] = set()
_BRANCH_READBACK_ATTEMPTS = 5
_BRANCH_READBACK_INITIAL_DELAY_SECONDS = 0.25


class NoChangesError(RuntimeError):
    """Backward-compatible no-op marker for callers that still import it."""


class GithubOperationError(RuntimeError):
    """Adds safe context to otherwise opaque GitHub client failures."""


def _github_error(step: str, exc: Exception) -> GithubOperationError:
    return GithubOperationError(f"{step} failed: {type(exc).__name__}: {exc}")


def _is_not_found_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "not found" in text or "404" in text


def get_github_token() -> str | None:
    return os.getenv("GITHUB_TOKEN")


def get_github_client():
    if Github is None or Auth is None:
        raise RuntimeError("PyGithub is not installed; GitHub operations are unavailable")
    github_token = get_github_token()
    if not github_token:
        raise RuntimeError("GITHUB_TOKEN is not set; GitHub operations are unavailable")
    client = Github(auth=Auth.Token(github_token))
    expected_username = str(os.getenv("GITHUB_USERNAME") or "").strip()
    if not expected_username:
        return client

    credential_key = (
        hashlib.sha256(github_token.encode("utf-8")).hexdigest(),
        expected_username.casefold(),
    )
    if credential_key in _VERIFIED_CREDENTIAL_IDENTITIES:
        return client

    try:
        authenticated_username = str(client.get_user().login or "").strip()
    except Exception as exc:
        raise RuntimeError(
            "Unable to verify the authenticated GitHub user for this workflow"
        ) from exc

    if authenticated_username.casefold() != expected_username.casefold():
        raise RuntimeError(
            "GitHub credential identity mismatch: configured user "
            f"'{expected_username}' but the PAT authenticates as "
            f"'{authenticated_username or '<unknown>'}'"
        )

    _VERIFIED_CREDENTIAL_IDENTITIES.add(credential_key)
    return client


def get_strategy_code(branch: str, path: str, repo_name: str = REPO_NAME) -> str:
    if os.getenv("RAE_OFFLINE") == "1":
        return (Path(__file__).resolve().parent / "strategy.py").read_text("utf-8")
    g = get_github_client()
    repo = g.get_repo(repo_name)
    file = repo.get_contents(path, ref=branch)
    return file.decoded_content.decode("utf-8")


def push_strategy_code(
    code: str,
    branch: str,
    path: str,
    commit_message: str,
    repo_name: str = REPO_NAME,
) -> str:
    if os.getenv("RAE_OFFLINE") == "1":
        out = Path(os.getenv("OFFLINE_PUSH_DIR", "/workspace/output"))
        out.mkdir(parents=True, exist_ok=True)
        target = out / "pushed_strategy.py"
        if target.exists() and target.read_text("utf-8") == code:        # <-- NEW
            return f"[offline] no changes for {target}"                   # <-- NEW
        target.write_text(code, "utf-8")                                  # (existing)
        return f"[offline] wrote edited strategy to {target}"
    g = get_github_client()
    repo = g.get_repo(repo_name)
    try:
        file = repo.get_contents(path, ref=branch)
        current = file.decoded_content.decode("utf-8")                    # <-- NEW
        if current == code:                                               # <-- NEW
            return f"No changes for {path} on {branch}; skipped commit"   # <-- NEW
        repo.update_file(
            path=path, message=commit_message, content=code, sha=file.sha, branch=branch
        )
        return f"Updated {path} on {branch}"                              # <-- NEW (clearer msg)
    except Exception:
        repo.create_file(path=path, message=commit_message, content=code, branch=branch)
    return f"Created {path} on {branch}"                                  # <-- NEW


def create_feature_branch(
    branch_name: str,
    base_branch: str = "main",
    repo_name: str = REPO_NAME,
) -> str:
    if os.getenv("RAE_OFFLINE") == "1":
        return f"[offline] skipped branch creation for {branch_name}"
    g = get_github_client()
    try:
        repo = g.get_repo(repo_name)
    except Exception as e:
        raise _github_error(f"get_repo({repo_name})", e)

    try:
        repo.get_branch(branch_name)
        return f"Reused existing branch {branch_name}"
    except Exception as e:
        if not _is_not_found_error(e):
            raise _github_error(f"check_existing_branch({branch_name})", e)

    try:
        base = repo.get_branch(base_branch)
    except Exception as e:
        raise _github_error(f"get_base_branch({base_branch})", e)

    try:
        repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=base.commit.sha)
    except Exception as e:
        if "already exists" in str(e).lower():
            return f"Reused existing branch {branch_name}"
        raise _github_error(f"create_git_ref(refs/heads/{branch_name})", e)
    return f"Created branch {branch_name} from {base_branch}"


def get_branch_sha(branch: str, repo_name: str = REPO_NAME) -> str | None:
    if os.getenv("RAE_OFFLINE") == "1":
        return None
    try:
        g = get_github_client()
        repo = g.get_repo(repo_name)
        return repo.get_branch(branch).commit.sha
    except Exception as exc:
        raise _github_error(
            f"get_branch_sha(repo={repo_name}, branch={branch})",
            exc,
        ) from exc


def _confirm_branch_sha(
    *,
    branch_name: str,
    base_branch: str,
    repo_name: str,
) -> str | None:
    """Read a newly prepared branch with bounded GitHub consistency retries."""

    delay_seconds = _BRANCH_READBACK_INITIAL_DELAY_SECONDS
    for attempt in range(1, _BRANCH_READBACK_ATTEMPTS + 1):
        try:
            return get_branch_sha(branch_name, repo_name=repo_name)
        except GithubOperationError as exc:
            if not _is_not_found_error(exc) or attempt == _BRANCH_READBACK_ATTEMPTS:
                raise GithubOperationError(
                    "confirm_branch_readback("
                    f"repo={repo_name}, source={base_branch}, target={branch_name}, "
                    f"attempt={attempt}/{_BRANCH_READBACK_ATTEMPTS}) failed: {exc}"
                ) from exc
            time.sleep(delay_seconds)
            delay_seconds *= 2

    raise AssertionError("branch readback retry loop exited unexpectedly")


def create_or_reuse_branch_details(
    *,
    branch_name: str,
    base_branch: str = "main",
    repo_name: str = REPO_NAME,
) -> dict[str, str | None]:
    message = create_feature_branch(
        branch_name=branch_name,
        base_branch=base_branch,
        repo_name=repo_name,
    )
    action = "reused" if "reused" in message.lower() else "created"
    return {
        "branch_action": action,
        "message": message,
        "commit_sha": _confirm_branch_sha(
            branch_name=branch_name,
            base_branch=base_branch,
            repo_name=repo_name,
        ),
    }


def delete_feature_branch(branch_name: str, repo_name: str = REPO_NAME):
    g = get_github_client()
    repo = g.get_repo(repo_name)
    ref = repo.get_git_ref(f"heads/{branch_name}")
    ref.delete()

    return f"Deleted branch {branch_name}"


def list_files(branch: str, directory: str = "", repo_name: str = REPO_NAME) -> list[str]:
    """List files in a directory on a given branch. Empty directory lists repo root."""
    g = get_github_client()
    repo = g.get_repo(repo_name)
    contents = repo.get_contents(directory, ref=branch)
    return [item.path for item in contents]


def list_repo_tree(branch: str, repo_name: str = REPO_NAME) -> list[dict]:
    """Return every file blob on a branch as {"path", "size"} records.

    One recursive git-tree call, names and sizes only — no file contents — so it
    stays cheap enough to feed a discovery step. Sizes let callers surface or skip
    files that would be too large to read. Directories (tree entries) are omitted;
    only blobs are returned. Empty on offline runs.
    """
    if os.getenv("RAE_OFFLINE") == "1":
        return []
    g = get_github_client()
    repo = g.get_repo(repo_name)
    sha = repo.get_branch(branch).commit.sha
    tree = repo.get_git_tree(sha, recursive=True)
    records: list[dict] = []
    for element in tree.tree:
        if element.type != "blob" or not element.path:
            continue
        records.append(
            {"path": element.path, "size": int(element.size or 0)}
        )
    return records
