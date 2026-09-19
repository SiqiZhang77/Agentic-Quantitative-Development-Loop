"""Repository file discovery for runs that name no specific resource.

A ticket may omit ``resource_path`` entirely. Rather than falling back to a
hardcoded demo file, the runtime lists the repositories once (names and sizes,
no contents — cheap) and lets the model pick the relevant file(s) from that map.

Discovery spans every repository the request selected, so the model can find a
file wherever it actually lives. The repository list and each repository's path
scope are taken from the request's validated ``repository_details`` — the same
records ``pipeline_mcp`` uses to build ``ALLOWED_DIRECTORIES_MAP`` for the MCP
server. Discovery therefore sees exactly the repositories the gateway already
authorised for this user, and honours the same per-repository directory scopes,
without duplicating or reimplementing that authorisation.

This module only decides what the model is *told exists*. Every read and write
still goes through the existing tools and their enforcement.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable


# Extensions that are binary, opaque, or large-data: their contents never help as
# text context and would be expensive or impossible to read.
_EXCLUDED_EXTENSIONS = frozenset(
    {
        # images / media
        "png", "jpg", "jpeg", "gif", "bmp", "svg", "ico", "webp", "tif", "tiff",
        "mp4", "mov", "avi", "mp3", "wav", "pdf",
        # archives
        "zip", "gz", "tar", "tgz", "bz2", "xz", "7z", "rar",
        # compiled / binary artifacts
        "pyc", "pyo", "so", "dll", "dylib", "o", "a", "class", "jar", "war",
        "exe", "bin", "wasm",
        # data / model blobs
        "csv", "tsv", "parquet", "feather", "orc", "avro", "pkl", "pickle",
        "npy", "npz", "pt", "pth", "ckpt", "h5", "hdf5", "db", "sqlite",
        "sqlite3", "mat",
        # fonts
        "ttf", "otf", "woff", "woff2",
    }
)

# Path segments holding vendored or generated files rather than source.
_SKIPPED_SEGMENTS = frozenset(
    {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".mypy_cache"}
)

# Total entries across all repositories, so adding repositories widens coverage
# without multiplying prompt cost. Split fairly between them below.
MAX_MAP_ENTRIES = 300
# Files at or above this size are surfaced with a warning marker: reading them
# whole may exhaust a small model's context (a notebook with outputs, say).
LARGE_FILE_BYTES = 200_000


def _extension(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _in_skipped_dir(path: str) -> bool:
    return any(segment in _SKIPPED_SEGMENTS for segment in path.split("/")[:-1])


def _within_scope(path: str, allowed_directories: list[str] | None) -> bool:
    scope = [
        str(directory).strip().strip("/")
        for directory in allowed_directories or []
        if str(directory).strip().strip("/") not in ("", ".")
    ]
    if not scope:
        return True
    return any(path == directory or path.startswith(directory + "/") for directory in scope)


def entry_key(entry: dict) -> str:
    """Stable "repo:path" identifier used to name an entry to the model."""

    return f"{entry.get('repo_full_name')}:{entry.get('path')}"


def filter_tree(
    records: list[dict],
    *,
    allowed_directories: list[str] | None = None,
) -> list[dict]:
    """Drop binary/data/vendored files and anything outside the allowed scope."""

    kept: list[dict] = []
    for record in records or []:
        path = str((record or {}).get("path") or "").strip()
        if not path or _in_skipped_dir(path):
            continue
        if _extension(path) in _EXCLUDED_EXTENSIONS:
            continue
        if not _within_scope(path, allowed_directories):
            continue
        kept.append({"path": path, "size": int((record or {}).get("size") or 0)})
    kept.sort(key=lambda item: item["path"])
    return kept


def _human_size(size: int) -> str:
    if size >= 1_000_000:
        return f"{size / 1_000_000:.1f} MB"
    if size >= 1_000:
        return f"{size / 1_000:.1f} KB"
    return f"{size} B"


def discover_entries(
    repositories: list[dict],
    *,
    tree_lister: Callable[..., list[dict]] | None = None,
) -> list[dict]:
    """List every selected repository and return one flat, scoped entry list.

    ``repositories`` are specs of ``{repo_full_name, branch, allowed_directories}``.
    A repository that cannot be listed is skipped rather than failing the run: the
    agent still has its own tools, and one unreachable repository must not sink a
    multi-repository task.
    """

    if tree_lister is None:
        from github_client import list_repo_tree as tree_lister  # noqa: N813

    entries: list[dict] = []
    for repository in repositories or []:
        repo_name = str((repository or {}).get("repo_full_name") or "").strip()
        branch = str((repository or {}).get("branch") or "").strip()
        if not repo_name or not branch:
            continue
        try:
            raw = tree_lister(branch=branch, repo_name=repo_name)
        except Exception:  # noqa: BLE001 - best-effort; tools remain available
            continue
        for record in filter_tree(
            raw, allowed_directories=repository.get("allowed_directories")
        ):
            entries.append(
                {
                    "repo_full_name": repo_name,
                    "branch": branch,
                    "path": record["path"],
                    "size": record["size"],
                }
            )
    return entries


def _budget_by_repo(entries: list[dict], max_entries: int) -> dict[str, int]:
    """Split the entry budget fairly across repositories, reusing any slack."""

    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry["repo_full_name"]] = counts.get(entry["repo_full_name"], 0) + 1
    if not counts:
        return {}

    share = max(1, max_entries // len(counts))
    quota = {repo: min(total, share) for repo, total in counts.items()}
    remaining = max_entries - sum(quota.values())
    for repo, total in counts.items():
        if remaining <= 0:
            break
        extra = min(remaining, total - quota[repo])
        if extra > 0:
            quota[repo] += extra
            remaining -= extra
    return quota


def format_entry_map(entries: list[dict], *, max_entries: int = MAX_MAP_ENTRIES) -> str:
    """Render entries grouped by repository, within a shared entry budget."""

    if not entries:
        return "(no candidate files found in scope)"

    quota = _budget_by_repo(entries, max_entries)
    by_repo: dict[str, list[dict]] = {}
    for entry in entries:
        by_repo.setdefault(entry["repo_full_name"], []).append(entry)

    blocks: list[str] = []
    for repo_name in sorted(by_repo):
        repo_entries = by_repo[repo_name]
        branch = repo_entries[0]["branch"]
        allowed = quota.get(repo_name, len(repo_entries))
        lines = [f"Repository {repo_name} (branch {branch}):"]
        for entry in repo_entries[:allowed]:
            marker = (
                "  [large — read only if necessary]"
                if entry["size"] >= LARGE_FILE_BYTES
                else ""
            )
            lines.append(f"- {entry_key(entry)} ({_human_size(entry['size'])}){marker}")
        hidden = len(repo_entries) - allowed
        if hidden > 0:
            lines.append(
                f"- ... and {hidden} more files in this repository not shown; "
                "list its directories with the tools to see them."
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def build_file_map_block(
    repositories: list[dict],
    *,
    tree_lister: Callable[..., list[dict]] | None = None,
    max_entries: int = MAX_MAP_ENTRIES,
) -> str:
    """Convenience: list every repository and render the map in one call."""

    return format_entry_map(
        discover_entries(repositories, tree_lister=tree_lister),
        max_entries=max_entries,
    )


def _parse_key_list(raw: str, known_keys: set[str]) -> list[str]:
    """Pull a JSON array of "repo:path" keys from a reply, keeping known ones."""

    for candidate in (raw, *re.findall(r"\[.*?\]", raw or "", re.DOTALL)):
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, list):
            chosen: list[str] = []
            for item in parsed:
                key = str(item).strip()
                if key in known_keys and key not in chosen:
                    chosen.append(key)
            return chosen
    return []


def select_relevant_entries(
    task_text: str,
    entries: list[dict],
    *,
    llm: Callable[..., str],
    max_files: int = 5,
    max_entries: int = MAX_MAP_ENTRIES,
) -> list[dict]:
    """Ask the model which files across the repositories are worth reading.

    Returns entries drawn only from ``entries`` (so the model cannot invent a
    path or reach an unlisted repository), capped at ``max_files``.
    """

    if not entries:
        return []
    by_key = {entry_key(entry): entry for entry in entries}
    prompt = f"""You are selecting which repository files to read to satisfy a task.
From the file list below, choose up to {max_files} files whose contents are most
relevant. The files span several repositories; each is named "repository:path".
Reply with ONLY a JSON array of exact "repository:path" strings from the list,
most relevant first. Do not invent paths.

TASK:
{task_text}

FILES:
{format_entry_map(entries, max_entries=max_entries)}
"""
    try:
        raw = llm(prompt)
    except Exception:  # noqa: BLE001 - fall back to the smallest files below
        raw = ""

    chosen = [by_key[key] for key in _parse_key_list(raw, set(by_key))][:max_files]
    if chosen:
        return chosen
    # No usable reply: fall back to the smallest in-scope files, which are the
    # cheapest to read and often the entry points (configs, small modules).
    return sorted(entries, key=lambda entry: entry["size"])[:max_files]


def repository_specs_from_payload(
    payload: dict[str, Any],
    *,
    prefer_target_branch: bool = False,
    available_target_branches: set[tuple[str, str]] | None = None,
) -> list[dict]:
    """Build discovery specs from the request's authorised repository records.

    The records come from the gateway-validated ``repository_details``, so this
    can only ever describe repositories the requester was authorised for, with
    each repository's own directory scope.

    ``prefer_target_branch`` reads from a repository's target branch when that
    branch is known to exist, so later iterations see earlier commits; otherwise
    the source branch is used.
    """

    specs: list[dict] = []
    for repository in payload.get("repositories") or []:
        if not isinstance(repository, dict):
            continue
        repo_name = str(repository.get("repo_full_name") or "").strip()
        if not repo_name:
            continue
        source_branch = str(repository.get("source_branch") or "").strip() or "main"
        target_branch = str(repository.get("target_branch") or "").strip()
        branch = source_branch
        if (
            prefer_target_branch
            and target_branch
            and (
                available_target_branches is None
                or (repo_name, target_branch) in available_target_branches
            )
        ):
            branch = target_branch
        specs.append(
            {
                "repo_full_name": repo_name,
                "branch": branch,
                "allowed_directories": repository.get("allowed_directories") or [],
            }
        )
    return specs
