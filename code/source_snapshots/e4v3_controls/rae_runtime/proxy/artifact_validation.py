"""Deterministic validation for repository artifacts produced by coding runs."""

from __future__ import annotations

import json
import re
import warnings
from pathlib import PurePosixPath
from typing import Callable


# Matched with fullmatch against the whole stripped artifact, so these only fire
# when the entire file is the placeholder -- a docstring mentioning "see above"
# in a real file is never affected. Extensions here are cheap; the durable check
# for source files is parsing, which does not depend on the wording.
_PLACEHOLDER_ONLY = re.compile(
    r"^(?:"
    r"the above (?:draft )?content|placeholder(?: content)?|"
    r"tbd|todo|coming soon|insert content here|"
    r"(?:detailed\s+)?content\s+.+\s+(?:will be|to be)\s+"
    r"(?:inserted|added|written)\s+here"
    # A whole artifact that only points at content stated somewhere else:
    # "Full implementation as described above", "python code from above".
    r"|[^\n]{0,120}?\b(?:as\s+(?:described|shown|specified|implemented)|from|see)"
    r"\s+(?:above|below|earlier|previously)\b"
    # An unsubstituted template token: "<<file_content>>", "{{content}}".
    r"|[<{]{1,2}\s*[\w .-]+\s*[>}]{1,2}"
    r")[.!\s]*$",
    re.IGNORECASE,
)
# A model that fumbles a tool call can pass its own call syntax as the content
# argument. The SDK hands us that string verbatim, so it is caught here rather
# than in argument parsing: a trailing "], commit_message=" is a fragment of the
# call, never the end of a source file.
_LEAKED_TOOL_ARGUMENT = re.compile(
    r"(?:^|\n)[ \t]*(?:```[\w]*)?[ \t]*[\]\}\)][ \t]*,[ \t]*"
    r"[A-Za-z_][A-Za-z0-9_]*[ \t]*=[ \t]*\S*[ \t]*$"
)
# Extensions where a leading ``` fence is legitimate content rather than the
# model wrapping the file in markdown before handing it over.
_FENCE_ALLOWED_SUFFIXES = {".md", ".markdown", ".rst", ".txt"}
_DOCUMENT_PATH_NAMES = {
    "dockerfile",
    "license",
    "license.md",
    "license.txt",
    "makefile",
    "pom.xml",
    "pyproject.toml",
    "requirements.txt",
}


def _is_not_found(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        getattr(exc, "status", None) == 404
        or "404" in message
        or "not found" in message
    )


def _readable_file(
    *,
    path: str,
    source_branch: str,
    target_branch: str,
    repo_name: str,
    reader: Callable[..., str],
) -> bool:
    for branch in (source_branch, target_branch):
        try:
            reader(branch=branch, path=path, repo_name=repo_name)
            return True
        except Exception as exc:
            if not _is_not_found(exc):
                raise
    return False


def readme_repository_issues(
    *,
    content: str,
    source_branch: str,
    target_branch: str,
    repo_name: str,
    reader: Callable[..., str],
) -> list[str]:
    """Validate README file references and build working directories."""

    issues: list[str] = []
    referenced_paths: set[str] = set()
    inline_values = re.findall(r"`([^`\n]+)`", content)
    for value in inline_values:
        candidate = value.strip().strip("./")
        if not candidate or any(character.isspace() for character in candidate):
            continue
        basename = PurePosixPath(candidate).name.lower()
        if basename in _DOCUMENT_PATH_NAMES or (
            "/" in candidate and "." in basename and ":" not in candidate
        ):
            referenced_paths.add(candidate)

    for path in sorted(referenced_paths):
        try:
            exists = _readable_file(
                path=path,
                source_branch=source_branch,
                target_branch=target_branch,
                repo_name=repo_name,
                reader=reader,
            )
        except Exception as exc:
            issues.append(f"Could not verify README reference {path}: {exc}")
            continue
        if not exists:
            issues.append(
                f"README references {path}, but that file does not exist in {repo_name}."
            )

    for command in re.findall(r"`([^`\n]*(?:mvn|mvnw)[^`\n]*)`", content):
        command = command.strip()
        pom_path = "pom.xml"
        explicit_pom = re.search(r"(?:^|\s)-f\s+([^\s]+)", command)
        working_dir = re.search(r"(?:^|&&\s*)cd\s+([^\s&]+)\s*&&", command)
        if explicit_pom:
            pom_path = explicit_pom.group(1).strip("'\"")
        elif working_dir:
            pom_path = f"{working_dir.group(1).strip('/').strip(chr(39) + chr(34))}/pom.xml"
        try:
            valid_pom = _readable_file(
                path=pom_path,
                source_branch=source_branch,
                target_branch=target_branch,
                repo_name=repo_name,
                reader=reader,
            )
        except Exception as exc:
            issues.append(f"Could not verify Maven command {command!r}: {exc}")
            continue
        if not valid_pom:
            issues.append(
                f"README Maven command {command!r} assumes {pom_path}, but that "
                "build file does not exist. Use the verified module path or -f option."
            )

    return sorted(set(issues))


def transport_artefact_issues(path: str, content: str) -> list[str]:
    """Reject content carrying the mechanics of how it was delivered.

    Both shapes here are the model leaking its own tool call into the file: a
    fragment of the argument list, or the file wrapped in a markdown fence.
    Neither is ever intended file content.
    """

    issues: list[str] = []
    text = str(content)
    if _LEAKED_TOOL_ARGUMENT.search(text):
        issues.append(
            f"Committed artifact {path} ends with a fragment of a tool call "
            "rather than file content. Pass only the file body as content."
        )
    if (
        PurePosixPath(path).suffix.lower() not in _FENCE_ALLOWED_SUFFIXES
        and text.strip().startswith("```")
    ):
        issues.append(
            f"Committed artifact {path} is wrapped in a markdown code fence. "
            "Pass the file body itself, without surrounding backticks."
        )
    return issues


def python_syntax_error(path: str, content: str) -> str | None:
    """Return a description when ``content`` is not parseable as Python.

    A model that emits prose, a truncated file, or a fragment of its own tool
    call produces something that cannot be imported. Phrase matching cannot
    catch that in general -- parsing can, whatever the wording.
    """

    if PurePosixPath(path).suffix.lower() != ".py":
        return None
    try:
        # Warnings are suppressed so the verdict does not depend on the ambient
        # filter: under -W error a SyntaxWarning (a non-raw "\d" in a regex, say)
        # would otherwise fail a file that imports perfectly well.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            compile(content, PurePosixPath(path).name, "exec")
    except SyntaxError as exc:
        detail = exc.msg or "invalid syntax"
        return f"line {exc.lineno}: {detail}" if exc.lineno else detail
    except ValueError as exc:
        # compile() rejects e.g. source containing null bytes with ValueError.
        return str(exc) or "source could not be parsed"
    return None


def json_syntax_error(path: str, content: str) -> str | None:
    """Return a location when a proposed ``.json`` file cannot be parsed."""

    if PurePosixPath(path).suffix.lower() != ".json":
        return None
    try:
        json.loads(content)
    except json.JSONDecodeError as exc:
        return f"line {exc.lineno}, column {exc.colno}: {exc.msg}"
    except (RecursionError, ValueError) as exc:
        return str(exc) or "JSON could not be parsed"
    return None


def content_quality_issues(path: str, content: str | None) -> list[str]:
    """Return actionable quality failures for one committed text artifact."""

    if content is None:
        return [f"Committed artifact {path} could not be read from the target branch."]

    text = str(content).strip()
    if not text:
        return [f"Committed artifact {path} is empty."]
    if _PLACEHOLDER_ONLY.fullmatch(text):
        return [f"Committed artifact {path} contains placeholder-only content."]

    # Checked before parsing: "content is a fragment of your tool call" is a
    # far more actionable message than the SyntaxError it also causes.
    transport_issues = transport_artefact_issues(path, str(content))
    if transport_issues:
        return transport_issues

    syntax_error = python_syntax_error(path, str(content))
    if syntax_error:
        return [
            f"Committed artifact {path} is not valid Python ({syntax_error}). "
            "Write the complete file contents."
        ]

    json_error = json_syntax_error(path, str(content))
    if json_error:
        return [
            f"Committed artifact {path} is not valid JSON ({json_error}). "
            "Write one complete JSON value."
        ]

    if PurePosixPath(path).name.lower() == "readme.md":
        issues = []
        if len(text) < 200:
            issues.append(f"README artifact {path} is too short to be substantive.")
        if not re.search(r"(?m)^#{1,6}\s+\S", text):
            issues.append(f"README artifact {path} has no Markdown heading structure.")
        return issues

    return []


def proposed_artifact_issues(
    *,
    path: str,
    content: str | None,
    source_branch: str,
    target_branch: str,
    repo_name: str,
    reader: Callable[..., str],
) -> list[str]:
    """Validate generated content before a GitHub write is attempted."""

    issues = content_quality_issues(path, content)
    if (
        content is not None
        and str(content).strip()
        and PurePosixPath(path).name.lower() == "readme.md"
    ):
        issues.extend(
            readme_repository_issues(
                content=str(content),
                source_branch=source_branch,
                target_branch=target_branch,
                repo_name=repo_name,
                reader=reader,
            )
        )
    return sorted(set(issues))


def inspect_committed_artifacts(
    *,
    committed_paths: list[str],
    source_branch: str,
    target_branch: str,
    repo_name: str,
    reader: Callable[..., str],
    required_target_path: str | None = None,
) -> tuple[list[str], list[str], list[str]]:
    """Read committed files, classify them, and return deterministic failures."""

    paths = sorted(set(path for path in committed_paths if path))
    issues: list[str] = []
    modified_files: list[str] = []
    new_files: list[str] = []

    if required_target_path and required_target_path not in paths:
        issues.append(
            f"Required target_path {required_target_path} was not included in committed paths."
        )

    for path in paths:
        try:
            target_content = reader(
                branch=target_branch,
                path=path,
                repo_name=repo_name,
            )
        except Exception as exc:  # GitHub clients expose several 404 exception types.
            issues.append(
                f"Committed artifact {path} could not be read from {repo_name}@"
                f"{target_branch}: {exc}"
            )
            continue

        issues.extend(content_quality_issues(path, target_content))
        if PurePosixPath(path).name.lower() == "readme.md":
            issues.extend(
                readme_repository_issues(
                    content=target_content,
                    source_branch=source_branch,
                    target_branch=target_branch,
                    repo_name=repo_name,
                    reader=reader,
                )
            )

        try:
            reader(branch=source_branch, path=path, repo_name=repo_name)
        except Exception as exc:
            if _is_not_found(exc):
                new_files.append(path)
            else:
                issues.append(
                    f"Could not classify {path} against {repo_name}@{source_branch}: {exc}"
                )
        else:
            modified_files.append(path)

    return modified_files, new_files, issues
