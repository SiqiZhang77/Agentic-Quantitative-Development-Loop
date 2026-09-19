"""Known repository aliases for Jira quant workflow routing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


GITHUB_OWNER = "bankingscience"
DEFAULT_ALIAS = "BSLAgenticQuantDevLoop"
DEFAULT_REPO_FULL_NAME = f"{GITHUB_OWNER}/BSLAgenticQuantDevLoop"


@dataclass(frozen=True)
class RepositoryCatalogEntry:
    alias: str
    repo_full_name: str
    default_source_branch: str
    runtime_role: str
    aliases: tuple[str, ...]

    @property
    def clone_url(self) -> str:
        return f"https://github.com/{self.repo_full_name}.git"


CATALOG: tuple[RepositoryCatalogEntry, ...] = (
    RepositoryCatalogEntry(
        alias=DEFAULT_ALIAS,
        repo_full_name=DEFAULT_REPO_FULL_NAME,
        default_source_branch="main",
        runtime_role="default",
        aliases=(
            "bslagenticquantdevloop",
            DEFAULT_REPO_FULL_NAME,
            "https://github.com/bankingscience/BSLAgenticQuantDevLoop",
            "https://github.com/bankingscience/BSLAgenticQuantDevLoop.git",
        ),
    ),
    RepositoryCatalogEntry(
        alias="ATPConnectorsRepo",
        repo_full_name=f"{GITHUB_OWNER}/ATPConnectorsRepo",
        default_source_branch="develop",
        runtime_role="repository",
        aliases=(
            "ATPConnectorsRepo",
            f"{GITHUB_OWNER}/ATPConnectorsRepo",
            "https://github.com/bankingscience/ATPConnectorsRepo",
            "https://github.com/bankingscience/ATPConnectorsRepo.git",
        ),
    ),
    RepositoryCatalogEntry(
        alias="ATPDataHandlersRepo",
        repo_full_name=f"{GITHUB_OWNER}/ATPDataHandlersRepo",
        default_source_branch="develop",
        runtime_role="repository",
        aliases=(
            "ATPDataHandlersRepo",
            f"{GITHUB_OWNER}/ATPDataHandlersRepo",
            "https://github.com/bankingscience/ATPDataHandlersRepo",
            "https://github.com/bankingscience/ATPDataHandlersRepo.git",
        ),
    ),
    RepositoryCatalogEntry(
        alias="ATPSiftingAnalyticsRepo",
        repo_full_name=f"{GITHUB_OWNER}/ATPSiftingAnalyticsRepo",
        default_source_branch="develop",
        runtime_role="repository",
        aliases=(
            "ATPSiftingAnalyticsRepo",
            f"{GITHUB_OWNER}/ATPSiftingAnalyticsRepo",
            "https://github.com/bankingscience/ATPSiftingAnalyticsRepo",
            "https://github.com/bankingscience/ATPSiftingAnalyticsRepo.git",
        ),
    ),
    RepositoryCatalogEntry(
        alias="ATPSiftingPreTradeRepo",
        repo_full_name=f"{GITHUB_OWNER}/ATPSiftingPreTradeRepo",
        default_source_branch="develop",
        runtime_role="repository",
        aliases=(
            "ATPSiftingPreTradeRepo",
            f"{GITHUB_OWNER}/ATPSiftingPreTradeRepo",
            "https://github.com/bankingscience/ATPSiftingPreTradeRepo",
            "https://github.com/bankingscience/ATPSiftingPreTradeRepo.git",
        ),
    ),
)


def _normalize(value: str) -> str:
    normalized = value.strip().lower()
    normalized = re.sub(r"^https://github\.com/", "", normalized)
    normalized = re.sub(r"\.git$", "", normalized)
    normalized = normalized.replace("-", "_")
    normalized = re.sub(r"[_\s]+", " ", normalized)
    return normalized.strip()


def _aliases_by_normalized() -> dict[str, RepositoryCatalogEntry]:
    aliases: dict[str, RepositoryCatalogEntry] = {}
    for entry in CATALOG:
        aliases[_normalize(entry.alias)] = entry
        aliases[_normalize(entry.repo_full_name)] = entry
        aliases[_normalize(entry.repo_full_name.split("/", 1)[1])] = entry
        for alias in entry.aliases:
            aliases[_normalize(alias)] = entry
    return aliases


ALIASES_BY_NORMALIZED = _aliases_by_normalized()


def accepted_aliases() -> list[str]:
    return sorted({alias for entry in CATALOG for alias in (entry.alias, *entry.aliases)})


def resolve_repository(value: Any) -> RepositoryCatalogEntry | None:
    if value is None:
        return None
    return ALIASES_BY_NORMALIZED.get(_normalize(str(value)))


def default_repository() -> RepositoryCatalogEntry:
    entry = resolve_repository(DEFAULT_ALIAS)
    assert entry is not None
    return entry


def prose_aliases_in(text: str) -> list[RepositoryCatalogEntry]:
    lowered = f" {_normalize(text)} "
    found: list[RepositoryCatalogEntry] = []
    seen: set[str] = set()
    for entry in CATALOG:
        probes = {_normalize(entry.alias), _normalize(entry.repo_full_name.split("/", 1)[1])}
        probes.update(_normalize(alias) for alias in entry.aliases)
        if any(f" {probe} " in lowered for probe in probes if probe):
            if entry.alias not in seen:
                found.append(entry)
                seen.add(entry.alias)
    return found
