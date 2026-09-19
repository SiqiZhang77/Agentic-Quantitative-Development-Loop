from __future__ import annotations

import copy
import hashlib
import json

import pytest

from exp3.contracts import (
    ARCHITECT_PLAN_MAX_CANONICAL_CHARS,
    DEVELOPER_RESULT_MAX_CANONICAL_CHARS,
    HandoffValidationError,
    canonical_json,
    canonical_sha256,
    validate_handoff,
)


def architect_plan() -> dict:
    return {
        "version": "architect_plan_v1",
        "producer": "architect",
        "risks": ["Changing the output contract could break legacy consumers."],
        "files": [
            {
                "path": "rae_runtime/sandbox/result_io.py",
                "purpose": "Preserve atomic result emission.",
            }
        ],
        "acceptance_mapping": [
            {
                "requirement_id": "REQ-1",
                "requirement": "Keep the result write atomic.",
                "planned_evidence": "Run the focused result I/O tests.",
                "files": ["rae_runtime/sandbox/result_io.py"],
            }
        ],
    }


def developer_result() -> dict:
    return {
        "version": "developer_result_v1",
        "producer": "developer",
        "implementation": [
            {
                "path": "rae_runtime/sandbox/result_io.py",
                "summary": "Kept the existing atomic replace path unchanged.",
            }
        ],
        "verification_evidence": [
            {
                "check": "focused result I/O tests",
                "status": "passed",
                "evidence": "2 tests passed locally.",
            }
        ],
    }


def manager_final() -> dict:
    return {
        "version": "manager_final_v1",
        "producer": "manager",
        "decision": "accepted",
        "failure_phase": None,
        "summary": "The implementation and verification evidence are complete.",
        "acceptance_mapping": [
            {
                "requirement_id": "REQ-1",
                "requirement": "Preserve the atomic result write.",
                "status": "passed",
                "evidence": "Focused result I/O tests passed.",
            }
        ],
    }


@pytest.mark.parametrize(
    ("value", "version", "producer"),
    [
        (architect_plan(), "architect_plan_v1", "architect"),
        (developer_result(), "developer_result_v1", "developer"),
        (manager_final(), "manager_final_v1", "manager"),
    ],
)
def test_valid_handoffs(value, version, producer) -> None:
    validated = validate_handoff(value, version, producer)

    assert validated.version == version
    assert validated.producer == producer
    assert validated.char_count == len(canonical_json(value))
    assert validated.value == value


@pytest.mark.parametrize(
    ("mutation", "expected_schema", "producer"),
    [
        (lambda value: value.update(version="developer_result_v1"), "architect_plan_v1", "architect"),
        (lambda value: value.update(producer="developer"), "architect_plan_v1", "architect"),
    ],
)
def test_wrong_version_or_body_producer_is_rejected(
    mutation,
    expected_schema,
    producer,
) -> None:
    value = architect_plan()
    mutation(value)

    with pytest.raises(HandoffValidationError):
        validate_handoff(value, expected_schema, producer)


def test_wrong_caller_producer_is_rejected() -> None:
    with pytest.raises(HandoffValidationError, match="caller producer role"):
        validate_handoff(architect_plan(), "architect_plan_v1", "developer")


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.pop("files"),
        lambda value: value.update(extra="not allowed"),
        lambda value: value.update(risks="not an array"),
        lambda value: value["files"][0].update(extra="not allowed"),
    ],
)
def test_missing_extra_and_wrong_type_are_rejected(mutation) -> None:
    value = architect_plan()
    mutation(value)

    with pytest.raises(HandoffValidationError):
        validate_handoff(value, "architect_plan_v1", "architect")


@pytest.mark.parametrize(
    ("factory", "version", "producer", "field_path", "limit"),
    [
        (
            architect_plan,
            "architect_plan_v1",
            "architect",
            ("risks", 0),
            ARCHITECT_PLAN_MAX_CANONICAL_CHARS,
        ),
        (
            developer_result,
            "developer_result_v1",
            "developer",
            ("implementation", 0, "summary"),
            DEVELOPER_RESULT_MAX_CANONICAL_CHARS,
        ),
    ],
)
def test_exact_character_limit_is_accepted_and_one_more_is_rejected(
    factory,
    version,
    producer,
    field_path,
    limit,
) -> None:
    value = factory()
    cursor = value
    for part in field_path[:-1]:
        cursor = cursor[part]
    final_key = field_path[-1]
    cursor[final_key] = ""
    filler_length = limit - len(canonical_json(value))
    assert filler_length > 0
    cursor[final_key] = "x" * filler_length
    assert len(canonical_json(value)) == limit

    assert validate_handoff(value, version, producer).char_count == limit

    cursor[final_key] += "x"
    with pytest.raises(HandoffValidationError, match="exceeds limit"):
        validate_handoff(value, version, producer)


def test_manager_final_has_no_invented_overall_character_limit() -> None:
    value = manager_final()
    value["summary"] = "m" * 30_000

    validated = validate_handoff(value, "manager_final_v1", "manager")

    assert validated.char_count > 24_000


def test_architect_plan_above_the_old_hidden_limit_is_accepted() -> None:
    value = architect_plan()
    value["risks"][0] = "x" * 12_464

    validated = validate_handoff(value, "architect_plan_v1", "architect")

    assert 12_000 < validated.char_count < ARCHITECT_PLAN_MAX_CANONICAL_CHARS


def test_manager_decision_and_failure_phase_must_agree() -> None:
    accepted = manager_final()
    accepted["failure_phase"] = "developer"
    with pytest.raises(HandoffValidationError):
        validate_handoff(accepted, "manager_final_v1", "manager")

    failed = manager_final()
    failed.update(decision="failed", failure_phase=None)
    with pytest.raises(HandoffValidationError):
        validate_handoff(failed, "manager_final_v1", "manager")

    failed["failure_phase"] = "developer"
    failed["acceptance_mapping"][0]["status"] = "failed"
    assert validate_handoff(failed, "manager_final_v1", "manager").value == failed


def test_manager_acceptance_mapping_is_required_and_acceptance_requires_all_passed() -> None:
    missing = manager_final()
    missing.pop("acceptance_mapping")
    with pytest.raises(HandoffValidationError):
        validate_handoff(missing, "manager_final_v1", "manager")

    incomplete = manager_final()
    incomplete["acceptance_mapping"][0]["status"] = "not_verified"
    with pytest.raises(HandoffValidationError):
        validate_handoff(incomplete, "manager_final_v1", "manager")


def test_canonical_json_and_hash_are_stable_and_utf8() -> None:
    left = architect_plan()
    left["risks"] = ["Unicode risk: café 数据"]
    right = {
        "acceptance_mapping": left["acceptance_mapping"],
        "files": left["files"],
        "risks": left["risks"],
        "producer": left["producer"],
        "version": left["version"],
    }

    canonical = canonical_json(left)
    assert canonical == canonical_json(right)
    assert "café 数据" in canonical
    assert "\\u00e9" not in canonical
    assert canonical_sha256(left) == canonical_sha256(right)
    assert canonical_sha256(left) == hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()


def test_validation_does_not_mutate_input_or_share_returned_value() -> None:
    value = developer_result()
    original = copy.deepcopy(value)

    validated = validate_handoff(value, "developer_result_v1", "developer")
    returned = validated.value
    returned["implementation"][0]["summary"] = "mutated by consumer"

    assert value == original
    assert validated.value == original


def test_audit_record_contains_only_content_free_metadata() -> None:
    value = manager_final()
    value["summary"] = "PRIVATE HANDOFF BODY"
    validated = validate_handoff(value, "manager_final_v1", "manager")

    audit = validated.audit_record()

    assert set(audit) == {"version", "hash", "char_count", "producer"}
    assert audit == {
        "version": "manager_final_v1",
        "hash": canonical_sha256(value),
        "char_count": len(canonical_json(value)),
        "producer": "manager",
    }
    assert "PRIVATE HANDOFF BODY" not in json.dumps(audit, sort_keys=True)
    assert "PRIVATE HANDOFF BODY" not in repr(validated)


def test_validation_errors_do_not_echo_private_body_values() -> None:
    value = architect_plan()
    value["producer"] = "PRIVATE INVALID PRODUCER"

    with pytest.raises(HandoffValidationError) as exc_info:
        validate_handoff(value, "architect_plan_v1", "architect")

    assert "PRIVATE INVALID PRODUCER" not in str(exc_info.value)
