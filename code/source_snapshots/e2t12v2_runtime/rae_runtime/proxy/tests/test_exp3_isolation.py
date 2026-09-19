from __future__ import annotations

import copy
import json

import pytest

from exp3.isolation import (
    PRE_MODEL_CALL,
    PRE_ORCHESTRATION,
    IsolationManifestError,
    IsolationPreflightError,
    NegativeRefManifest,
    NegativeRefPreflight,
    RefAccessDenied,
    run_negative_ref_preflight,
)


def _manifest_value() -> dict:
    return {
        "schema_version": "exp3-negative-ref-manifest-v1",
        "experiment_id": "E3-manager-star-v1",
        "run_id": "E3-T1-R1-M1",
        "repositories": [
            {
                "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
                "source_ref": "exp3/frozen-source",
                "current_target_ref": "quant/E3-T1-R1-M1",
                "paired_target_ref": "quant/E3-T1-R1-M0",
                "deny_refs": {
                    "v5_refs": ["exp2/si-smoke-source-v5"],
                    "e2_output_refs": ["quant/E2-T1-R1-C0"],
                    "earlier_e3_targets": ["quant/E3-T1-R0-M0"],
                    "protected_refs": ["main"],
                    "arbitrary_probe_refs": [
                        "arbitrary/not-allowed-a",
                        "arbitrary/not-allowed-b",
                    ],
                },
            }
        ],
    }


def test_manifest_is_versioned_canonical_and_deny_complete() -> None:
    value = _manifest_value()
    manifest = NegativeRefManifest.from_dict(value)

    assert manifest.schema_version == "exp3-negative-ref-manifest-v1"
    assert manifest.value == value
    assert len(manifest.sha256) == 64
    assert len(manifest.deny_ref_set_sha256) == 64
    policy = manifest.repositories[0]
    assert policy.can_read("exp3/frozen-source")
    assert policy.can_read("quant/E3-T1-R1-M1")
    assert not policy.can_read("main")
    assert not policy.can_read("completely/new/ref")
    assert policy.paired_target_ref in policy.complete_deny_set


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(schema_version="wrong"),
        lambda value: value["repositories"][0].update(source_ref="main"),
        lambda value: value["repositories"][0]["deny_refs"].update(protected_refs=[]),
        lambda value: value["repositories"][0]["deny_refs"].update(v5_refs=[]),
        lambda value: value["repositories"][0]["deny_refs"].update(e2_output_refs=[]),
        lambda value: value["repositories"][0].update(
            paired_target_ref="quant/E3-T1-R1-M1"
        ),
        lambda value: value["repositories"][0]["deny_refs"].update(
            arbitrary_probe_refs=["only-one"]
        ),
    ],
)
def test_manifest_rejects_incomplete_or_contradictory_deny_inputs(mutate) -> None:
    value = _manifest_value()
    mutate(value)
    with pytest.raises(IsolationManifestError):
        NegativeRefManifest.from_dict(value)


def test_pre_orchestration_proves_source_present_and_target_absent_only() -> None:
    manifest = NegativeRefManifest.from_dict(_manifest_value())
    probes = []

    def exists(repo, ref):
        probes.append((repo, ref))
        return ref == "exp3/frozen-source"

    evidence = run_negative_ref_preflight(
        manifest,
        phase=PRE_ORCHESTRATION,
        ref_exists=exists,
    )

    assert probes == [
        ("bankingscience/BSLAgenticQuantDevLoop", "exp3/frozen-source"),
        ("bankingscience/BSLAgenticQuantDevLoop", "quant/E3-T1-R1-M1"),
    ]
    assert evidence["passed"] is True
    assert evidence["source_presence_checked"] is True
    assert evidence["current_target_absence_checked"] is True
    serialized = json.dumps(evidence)
    assert "exp2/si-smoke-source-v5" not in serialized
    assert "quant/E2-T1-R1-C0" not in serialized


def test_pre_orchestration_stops_when_current_target_already_exists() -> None:
    manifest = NegativeRefManifest.from_dict(_manifest_value())

    with pytest.raises(IsolationPreflightError) as raised:
        run_negative_ref_preflight(
            manifest,
            phase=PRE_ORCHESTRATION,
            ref_exists=lambda _repo, _ref: True,
        )

    assert raised.value.failure_code == "CURRENT_TARGET_ALREADY_EXISTS"


def test_model_call_preflight_rechecks_source_and_all_deny_policy() -> None:
    manifest = NegativeRefManifest.from_dict(_manifest_value())
    preflight = NegativeRefPreflight(
        manifest,
        ref_exists=lambda _repo, ref: ref == "exp3/frozen-source",
    )
    preflight.pre_orchestration()
    first = preflight.before_provider_call(role="manager", phase="manager_to_architect")
    second = preflight.before_provider_call(role="architect", phase="architect_to_manager")

    assert first["phase"] == PRE_MODEL_CALL
    assert second["phase"] == PRE_MODEL_CALL
    assert len(preflight.evidence()) == 3
    binding = preflight.mcp_environment_binding()
    assert binding == {
        "E3_REF_SCOPE_PREFLIGHT_PASSED": "true",
        "E3_NEGATIVE_REF_MANIFEST_SHA256": manifest.sha256,
        "E3_NEGATIVE_REF_SET_SHA256": manifest.deny_ref_set_sha256,
    }


def test_policy_rejects_every_listed_and_unlisted_ref() -> None:
    manifest = NegativeRefManifest.from_dict(_manifest_value())
    policy = manifest.repositories[0]

    for ref in (*policy.complete_deny_set, "newly-invented-ref"):
        with pytest.raises(RefAccessDenied):
            policy.require_readable(ref)


def test_preflight_input_and_evidence_are_detached() -> None:
    original = _manifest_value()
    before = copy.deepcopy(original)
    manifest = NegativeRefManifest.from_dict(original)
    preflight = NegativeRefPreflight(
        manifest,
        ref_exists=lambda _repo, ref: ref == "exp3/frozen-source",
    )
    evidence = preflight.pre_orchestration()
    evidence.clear()

    assert original == before
    assert preflight.evidence()[0]["passed"] is True


def test_mcp_binding_requires_passing_pre_orchestration_evidence() -> None:
    preflight = NegativeRefPreflight(NegativeRefManifest.from_dict(_manifest_value()))
    with pytest.raises(IsolationPreflightError) as raised:
        preflight.mcp_environment_binding()
    assert raised.value.failure_code == "PRE_ORCHESTRATION_EVIDENCE_MISSING"
