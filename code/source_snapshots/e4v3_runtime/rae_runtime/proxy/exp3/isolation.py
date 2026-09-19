"""Versioned, pure negative-ref isolation preflight for Experiment 3.

No GitHub client is imported here.  Existence checks are injected so local tests
cannot accidentally contact a remote.  The manifest enumerates the known
contamination classes while the readable-ref policy remains an allowlist:
anything other than the frozen source and the current target is denied,
including refs not known when the manifest was constructed.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from threading import RLock
from typing import Any, Final

from jsonschema import Draft202012Validator

from exp3.contracts import canonical_json, canonical_sha256


MANIFEST_VERSION: Final = "exp3-negative-ref-manifest-v1"
FORMAL_MANIFEST_VERSION: Final = "rae-negative-ref-manifest-v2"
EVIDENCE_VERSION: Final = "exp3-negative-ref-preflight-evidence-v1"
EXPERIMENT_ID: Final = "E3-manager-star-v1"
PRE_ORCHESTRATION: Final = "pre_orchestration"
PRE_MODEL_CALL: Final = "pre_model_call"
PREFLIGHT_PHASES: Final = frozenset({PRE_ORCHESTRATION, PRE_MODEL_CALL})
DENY_CATEGORIES: Final = (
    "v5_refs",
    "e2_output_refs",
    "earlier_e3_targets",
    "protected_refs",
    "arbitrary_probe_refs",
)
FORMAL_DENY_CATEGORIES: Final = (
    "prior_study_refs",
    "other_result_refs",
    "protected_refs",
    "arbitrary_probe_refs",
)

_SCHEMA_PATH = (
    Path(__file__).resolve().parent
    / "schemas"
    / "negative_ref_manifest_v1.schema.json"
)
_FORMAL_SCHEMA_PATH = (
    Path(__file__).resolve().parent
    / "schemas"
    / "negative_ref_manifest_v2.schema.json"
)


class IsolationManifestError(ValueError):
    """The deny manifest is incomplete, contradictory or malformed."""


class IsolationPreflightError(RuntimeError):
    """A content-free isolation failure that must stop the E3 run."""

    def __init__(self, failure_code: str):
        self.failure_code = failure_code
        super().__init__(f"Experiment 3 isolation preflight failed ({failure_code})")


class RefAccessDenied(PermissionError):
    """A repository/ref pair is outside the two-ref per-run allowlist."""

    def __init__(self) -> None:
        super().__init__("Experiment 3 readable-ref policy denied the requested ref")


@lru_cache(maxsize=2)
def _validator(version: str) -> Draft202012Validator:
    schema_path = {
        MANIFEST_VERSION: _SCHEMA_PATH,
        FORMAL_MANIFEST_VERSION: _FORMAL_SCHEMA_PATH,
    }.get(version)
    if schema_path is None:
        raise IsolationManifestError("negative-ref manifest version is unsupported")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _schema_reasons(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return ["<root>: must be an object"]
    version = value.get("schema_version")
    return [
        f"{'.'.join(str(part) for part in error.absolute_path) or '<root>'}: "
        f"violates {error.validator or 'schema'}"
        for error in sorted(
            _validator(version).iter_errors(value),
            key=lambda item: tuple(str(part) for part in item.absolute_path),
        )
    ]


@dataclass(frozen=True)
class RepositoryRefPolicy:
    repo_full_name: str
    source_ref: str
    current_target_ref: str
    paired_target_ref: str
    deny_refs: Mapping[str, tuple[str, ...]] = field(repr=False)

    @property
    def readable_refs(self) -> frozenset[str]:
        return frozenset({self.source_ref, self.current_target_ref})

    @property
    def complete_deny_set(self) -> frozenset[str]:
        return frozenset(
            {self.paired_target_ref}
            | {
                ref
                for refs in self.deny_refs.values()
                for ref in refs
            }
        )

    def can_read(self, ref: object) -> bool:
        return type(ref) is str and ref in self.readable_refs

    def require_readable(self, ref: object) -> None:
        if not self.can_read(ref):
            raise RefAccessDenied()


@dataclass(frozen=True)
class NegativeRefManifest:
    schema_version: str
    experiment_id: str
    run_id: str
    repositories: tuple[RepositoryRefPolicy, ...]
    sha256: str
    deny_ref_set_sha256: str
    _canonical: str = field(repr=False, compare=False)

    @classmethod
    def from_dict(cls, value: Any) -> "NegativeRefManifest":
        reasons = _schema_reasons(value)
        if reasons:
            raise IsolationManifestError("negative-ref manifest: " + "; ".join(reasons))
        canonical = canonical_json(value)
        parsed = json.loads(canonical)
        categories = (
            DENY_CATEGORIES
            if parsed["schema_version"] == MANIFEST_VERSION
            else FORMAL_DENY_CATEGORIES
        )
        policies: list[RepositoryRefPolicy] = []
        repo_names: set[str] = set()
        deny_identity: list[dict[str, Any]] = []

        for item in parsed["repositories"]:
            repo_name = item["repo_full_name"]
            if repo_name in repo_names:
                raise IsolationManifestError("repository entries must be unique")
            repo_names.add(repo_name)
            source = item["source_ref"]
            target = item["current_target_ref"]
            paired = item["paired_target_ref"]
            if source == "main":
                raise IsolationManifestError("the frozen E3 source ref must not be main")
            if target == source or paired in {source, target}:
                raise IsolationManifestError(
                    "source, current target and paired target must be distinct"
                )
            deny = {
                category: tuple(item["deny_refs"][category])
                for category in categories
            }
            if "main" not in deny["protected_refs"]:
                raise IsolationManifestError("protected_refs must include main")
            flattened = [
                ref
                for category in categories
                for ref in deny[category]
            ] + [paired]
            if len(flattened) != len(set(flattened)):
                raise IsolationManifestError(
                    "deny refs and the paired target must be globally unique"
                )
            if {source, target}.intersection(flattened):
                raise IsolationManifestError(
                    "readable source/current target refs must not appear in the deny set"
                )
            policy = RepositoryRefPolicy(
                repo_full_name=repo_name,
                source_ref=source,
                current_target_ref=target,
                paired_target_ref=paired,
                deny_refs=deny,
            )
            policies.append(policy)
            deny_identity.append(
                {
                    "repo_full_name": repo_name,
                    "deny_refs": sorted(policy.complete_deny_set),
                }
            )

        return cls(
            schema_version=parsed["schema_version"],
            experiment_id=parsed["experiment_id"],
            run_id=parsed["run_id"],
            repositories=tuple(policies),
            sha256=canonical_sha256(parsed),
            deny_ref_set_sha256=canonical_sha256(deny_identity),
            _canonical=canonical,
        )

    @property
    def value(self) -> dict[str, Any]:
        return json.loads(self._canonical)

    def policy_for(self, repo_full_name: object) -> RepositoryRefPolicy:
        if type(repo_full_name) is not str:
            raise RefAccessDenied()
        for policy in self.repositories:
            if policy.repo_full_name == repo_full_name:
                return policy
        raise RefAccessDenied()


RefExistsProbe = Callable[[str, str], bool]


def run_negative_ref_preflight(
    manifest: NegativeRefManifest,
    *,
    phase: str,
    ref_exists: RefExistsProbe | None = None,
    require_current_target_absent: bool = True,
) -> dict[str, Any]:
    """Validate deny policy and optional source/target existence invariants."""

    if not isinstance(manifest, NegativeRefManifest):
        raise TypeError("manifest must be a NegativeRefManifest")
    if phase not in PREFLIGHT_PHASES:
        raise IsolationPreflightError("INVALID_PREFLIGHT_PHASE")
    if phase == PRE_ORCHESTRATION and ref_exists is None:
        raise IsolationPreflightError("REF_EXISTENCE_PROBE_REQUIRED")
    if ref_exists is not None and not callable(ref_exists):
        raise TypeError("ref_exists must be callable or None")
    if type(require_current_target_absent) is not bool:
        raise TypeError("require_current_target_absent must be a boolean")

    source_checked = False
    target_absence_checked = False
    denied_count = 0
    for policy in manifest.repositories:
        if not policy.can_read(policy.source_ref):
            raise IsolationPreflightError("SOURCE_NOT_ALLOWLISTED")
        if not policy.can_read(policy.current_target_ref):
            raise IsolationPreflightError("CURRENT_TARGET_NOT_ALLOWLISTED")
        for denied_ref in policy.complete_deny_set:
            denied_count += 1
            if policy.can_read(denied_ref):
                raise IsolationPreflightError("DENY_REF_BECAME_READABLE")
        # An unlisted sentinel proves the policy is an allowlist, rather than a
        # finite blocklist that would permit newly invented refs.
        if policy.can_read("exp3-arbitrary-unlisted-sentinel"):
            raise IsolationPreflightError("ARBITRARY_REF_BECAME_READABLE")

        if ref_exists is not None:
            try:
                source_exists = ref_exists(policy.repo_full_name, policy.source_ref)
            except Exception as exc:
                raise IsolationPreflightError("REF_EXISTENCE_PROBE_FAILED") from exc
            if type(source_exists) is not bool or not source_exists:
                raise IsolationPreflightError("FROZEN_SOURCE_REF_ABSENT")
            source_checked = True
            if phase == PRE_ORCHESTRATION and require_current_target_absent:
                try:
                    target_exists = ref_exists(
                        policy.repo_full_name,
                        policy.current_target_ref,
                    )
                except Exception as exc:
                    raise IsolationPreflightError("REF_EXISTENCE_PROBE_FAILED") from exc
                if type(target_exists) is not bool:
                    raise IsolationPreflightError("REF_EXISTENCE_PROBE_INVALID")
                if target_exists:
                    raise IsolationPreflightError("CURRENT_TARGET_ALREADY_EXISTS")
                target_absence_checked = True

    return {
        "schema_version": EVIDENCE_VERSION,
        "phase": phase,
        "passed": True,
        "manifest_sha256": manifest.sha256,
        "deny_ref_set_sha256": manifest.deny_ref_set_sha256,
        "repository_count": len(manifest.repositories),
        "denied_ref_count": denied_count,
        "source_presence_checked": source_checked,
        "current_target_absence_checked": target_absence_checked,
    }


class NegativeRefPreflight:
    """Reusable pre-orchestration and per-provider-call preflight binding."""

    def __init__(
        self,
        manifest: NegativeRefManifest,
        *,
        ref_exists: RefExistsProbe | None = None,
        require_current_target_absent: bool = True,
    ) -> None:
        if not isinstance(manifest, NegativeRefManifest):
            raise TypeError("manifest must be a NegativeRefManifest")
        self.manifest = manifest
        self._ref_exists = ref_exists
        if type(require_current_target_absent) is not bool:
            raise TypeError("require_current_target_absent must be a boolean")
        self._require_current_target_absent = require_current_target_absent
        self._evidence: list[dict[str, Any]] = []
        self._lock = RLock()

    def _record(self, evidence: Mapping[str, Any]) -> dict[str, Any]:
        detached = copy.deepcopy(dict(evidence))
        with self._lock:
            self._evidence.append(detached)
        return copy.deepcopy(detached)

    def pre_orchestration(self) -> dict[str, Any]:
        return self._record(
            run_negative_ref_preflight(
                self.manifest,
                phase=PRE_ORCHESTRATION,
                ref_exists=self._ref_exists,
                require_current_target_absent=(
                    self._require_current_target_absent
                ),
            )
        )

    def before_provider_call(self, *, role: str, phase: str) -> dict[str, Any]:
        # Role/phase are intentionally not used to widen access; they are checked
        # only for bounded, content-free telemetry identity.
        if type(role) is not str or not role or type(phase) is not str or not phase:
            raise IsolationPreflightError("INVALID_CALL_IDENTITY")
        return self._record(
            run_negative_ref_preflight(
                self.manifest,
                phase=PRE_MODEL_CALL,
                ref_exists=self._ref_exists,
            )
        )

    def evidence(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._evidence)

    def mcp_environment_binding(self) -> dict[str, str]:
        """Return only sanitized hashes after a passing pre-orchestration gate."""

        with self._lock:
            passed = any(
                item.get("phase") == PRE_ORCHESTRATION and item.get("passed") is True
                for item in self._evidence
            )
        if not passed:
            raise IsolationPreflightError("PRE_ORCHESTRATION_EVIDENCE_MISSING")
        return {
            "E3_REF_SCOPE_PREFLIGHT_PASSED": "true",
            "E3_NEGATIVE_REF_MANIFEST_SHA256": self.manifest.sha256,
            "E3_NEGATIVE_REF_SET_SHA256": self.manifest.deny_ref_set_sha256,
        }
