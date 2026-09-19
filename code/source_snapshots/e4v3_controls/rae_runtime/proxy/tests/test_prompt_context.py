import hashlib
import json

import pytest

from prompt_context import (
    MAX_CONTEXT_CHARS,
    MAX_RETRIEVED_CONTEXT_CHARS,
    MAX_RETRIEVED_MEMORY_CHARS,
    build_code_prompt,
    build_mcp_code_prompt,
    build_retrieved_memory_context,
    build_ticket_context,
    freeze_retrieval_prompt_delivery,
    mark_retrieval_prompt_delivery,
)


def _payload(strategy_type="backtest", path="rae_runtime/proxy/strategy.py"):
    return {
        "issue_key": "SCRUM-90",
        "command": strategy_type,
        "execution_objectives": {
            "strategy_type": strategy_type,
            "parsed_task_parameters": {
                "objective": "Run the requested research change"
            },
        },
        "strategy": {"path": path},
        "jira_context": {
            "ticket_id": "SCRUM-90",
            "summary": "Use the ticket as source of truth",
            "description": "Acceptance criteria and meeting notes live here.",
            "triggering_comment": {
                "timestamp": "2026-07-08T09:00:00Z",
                "author": "Example User",
                "text": "/quant Run the requested research change",
            },
            "events_history": [
                {
                    "event_type": "comment",
                    "timestamp": "2026-07-07T09:00:00Z",
                    "author": "Analyst",
                    "text": "Current behaviour is incorrect for branch mappings.",
                }
            ],
        },
    }


def _with_retrieval(payload, texts=None):
    texts = texts or ["History remains chronological and capped."]
    payload["retrieval_context"] = {
        "enabled": True,
        "status": "ok" if texts else "empty",
        "memories": [
            {
                "memory_id": f"MEM-{index:02d}",
                "source_ticket_id": f"SCRUM-{index + 10}",
                "source_type": "comment",
                "source_id": f"comment:{index}",
                "source_timestamp": "2026-06-01T00:00:00+00:00",
                "rank": index,
                "score": 1.0 / index,
                "text": text,
            }
            for index, text in enumerate(texts, start=1)
        ],
    }
    return payload


def test_code_prompt_contains_complete_ticket_context():
    prompt = build_code_prompt(_payload(), "def strategy(): pass")

    assert "Use the ticket as source of truth" in prompt
    assert "Acceptance criteria and meeting notes" in prompt
    assert "Canonical task request" in prompt
    assert "Run the requested research change" in prompt
    assert "/quant" not in prompt
    assert "Current behaviour is incorrect for branch mappings" in prompt
    assert "UNTRUSTED USER-AUTHORED CONTENT" in prompt
    assert "def strategy(): pass" in prompt


def test_coding_prompts_receive_retrieved_memory_but_shared_ticket_context_does_not():
    payload = _with_retrieval(_payload("refactor"))

    scripted = build_code_prompt(payload, "def retry(): pass")
    mcp = build_mcp_code_prompt(
        payload,
        read_ref="main",
        branch_name="quant/SCRUM-90",
    )
    shared = build_ticket_context(payload)

    for prompt in (scripted, mcp):
        assert "RETRIEVED JIRA MEMORY" in prompt
        assert "History remains chronological and capped." in prompt
        assert '"memory_id":"MEM-01"' in prompt
    assert "RETRIEVED JIRA MEMORY" not in shared
    assert "History remains chronological and capped." not in shared


def test_c0_coding_prompts_do_not_gain_a_retrieval_section():
    prompt = build_code_prompt(_payload("refactor"), "def retry(): pass")

    assert "RETRIEVED JIRA MEMORY" not in prompt


def test_retrieved_memory_enforces_per_item_and_total_prompt_limits():
    payload = _with_retrieval(
        _payload("refactor"),
        [str(index) * (MAX_RETRIEVED_MEMORY_CHARS + 500) for index in range(1, 6)],
    )

    rendered = build_retrieved_memory_context(payload)
    jsonl = rendered.split("BEGIN RETRIEVED JIRA MEMORY JSONL\n", 1)[1].split(
        "\nEND RETRIEVED JIRA MEMORY JSONL", 1
    )[0]
    records = [json.loads(line) for line in jsonl.splitlines()]

    assert len(rendered) <= MAX_RETRIEVED_CONTEXT_CHARS
    assert records
    assert all(len(record["text"]) <= MAX_RETRIEVED_MEMORY_CHARS for record in records)
    assert [record["rank"] for record in records] == list(
        range(1, len(records) + 1)
    )


def test_delivery_marker_contains_hashes_and_ids_but_no_memory_text():
    payload = _with_retrieval(_payload("refactor"))
    rendered = build_retrieved_memory_context(payload)

    delivery = mark_retrieval_prompt_delivery(payload)

    assert delivery == payload["_retrieval_delivery"]
    assert delivery["delivery_mode"] == "generator_prompt"
    assert delivery["prompt_injected"] is True
    assert delivery["injected_memory_ids"] == ["MEM-01"]
    assert delivery["evidence_text_sha256"] == hashlib.sha256(
        rendered.encode("utf-8")
    ).hexdigest()
    serialized = json.dumps(delivery)
    assert "History remains chronological and capped." not in serialized


def test_frozen_delivery_returns_one_exact_block_and_matching_receipt():
    payload = _with_retrieval(_payload("refactor"))

    rendered, delivery = freeze_retrieval_prompt_delivery(payload)

    assert delivery is not None
    assert delivery["evidence_text_sha256"] == hashlib.sha256(
        rendered.encode("utf-8")
    ).hexdigest()
    assert delivery["injected_memory_ids"] == ["MEM-01"]
    assert "History remains chronological and capped." in rendered
    assert "History remains chronological and capped." not in json.dumps(delivery)


def test_retrieved_memory_redacts_secrets_and_email_before_prompt_delivery():
    payload = _with_retrieval(
        _payload("refactor"),
        ["Contact analyst@example.com with token=private-token-value"],
    )

    rendered = build_retrieved_memory_context(payload)

    assert "analyst@example.com" not in rendered
    assert "private-token-value" not in rendered
    assert "[REDACTED_EMAIL]" in rendered
    assert "token=[REDACTED]" in rendered


def test_empty_c1_result_is_explicitly_delivered_without_memory_ids():
    payload = _payload("refactor")
    payload["retrieval_context"] = {
        "enabled": True,
        "status": "empty",
        "memories": [],
    }

    rendered = build_retrieved_memory_context(payload)
    delivery = mark_retrieval_prompt_delivery(payload)

    assert '"status":"empty"' in rendered
    assert delivery["injected_memory_ids"] == []


def test_code_prompt_follows_the_strategy_type():
    # Regression: this prompt used to open "You are a quantitative developer.
    # Modify the Python trading strategy" for every ticket type.
    prompt = build_code_prompt(
        _payload("refactor", "rae_runtime/proxy/github_client.py"),
        "def retry(): pass",
    )

    assert "software engineer" in prompt
    assert "trading strategy" not in prompt.lower()
    assert "Preserve the existing behaviour" in prompt
    assert "rae_runtime/proxy/github_client.py" in prompt


def test_backtest_prompt_targeting_a_request_file_gets_engine_rules():
    prompt = build_code_prompt(
        _payload("backtest", "rae_runtime/proxy/strategy.request"),
        "NPORT = 50",
    )

    assert "quantitative developer" in prompt
    assert "atrade engine .request" in prompt
    assert "Do NOT add new keys" in prompt


def test_operator_instruction_override_is_appended_when_set():
    payload = _payload("refactor")
    payload["execution_objectives"]["system_instruction_override"] = (
        "Prefer the smallest possible diff."
    )

    prompt = build_code_prompt(payload, "def retry(): pass")

    assert "Prefer the smallest possible diff." in prompt


def test_ticket_context_redacts_secrets_and_is_bounded():
    payload = _payload()
    payload["jira_context"]["description"] = (
        "api_key=super-secret-value " + "x" * (MAX_CONTEXT_CHARS + 100)
    )

    context = build_ticket_context(payload)

    assert "super-secret-value" not in context
    assert "api_key=[REDACTED]" in context
    assert len(context) <= MAX_CONTEXT_CHARS
    assert context.endswith("[OLDER OR EXCESS TICKET CONTEXT TRUNCATED]")


@pytest.mark.parametrize(
    "secret_text",
    [
        '{"api_key":"super-secret-value"}',
        "token: super-secret-value",
        "Authorization: Bearer super-secret-value",
        "Bearer super-secret-value",
    ],
)
def test_ticket_context_redacts_additional_secret_shapes(secret_text):
    payload = _payload()
    payload["jira_context"]["description"] = secret_text

    context = build_ticket_context(payload)

    assert "super-secret-value" not in context
    assert "[REDACTED]" in context


def test_ticket_context_hides_rag_controls_from_generator_and_reviewer_context():
    payload = _payload("refactor")
    payload["jira_context"]["triggering_comment"]["text"] = (
        "/quant\nRun the requested research change\n"
        "rag_enabled: true\nrag_top_k: 5"
    )
    payload["jira_context"]["events_history"].extend(
        [
            {
                "event_type": "comment",
                "timestamp": "2026-07-07T10:00:00Z",
                "author": "Analyst",
                "text": "/quant Older run\nrag_enabled: false",
            },
            {
                "event_type": "comment",
                "timestamp": "2026-07-07T11:00:00Z",
                "author": "Analyst",
                "text": "Preserve malformed-comment handling.",
            },
        ]
    )

    context = build_ticket_context(payload)

    assert "Run the requested research change" in context
    assert "Preserve malformed-comment handling." in context
    assert "/quant" not in context
    assert "rag_enabled" not in context
    assert "rag_top_k" not in context


def test_ticket_context_lists_verified_input_datasets():
    payload = _payload("analysis")
    payload["input_datasets"] = [
        {
            "dataset_id": "experiment_2_input",
            "container_path": "/workspace/input/datasets/experiment_2_input.csv",
            "format": "csv",
            "size_bytes": 9620,
            "sha256": "a" * 64,
        }
    ]

    context = build_ticket_context(payload)

    assert "Verified read-only input datasets" in context
    assert "/workspace/input/datasets/experiment_2_input.csv" in context
    assert "format=csv" in context
    assert "size_bytes=9620" in context


def test_shared_mcp_prompt_names_repository_source_target_and_quality_rules():
    payload = _payload("other", "atp-handlers/src/main/resources/readme")
    payload["strategy"].update(
        {
            "repo_full_name": "bankingscience/ATPDataHandlersRepo",
            "ref": "develop",
            "target_branch": "quant/SCRUM-90",
            "source_path": "atp-handlers/src/main/resources/readme",
            "target_path": "README.md",
            "allowed_directories": ["."],
        }
    )

    prompt = build_mcp_code_prompt(
        payload,
        read_ref="develop",
        branch_name="quant/SCRUM-90",
    )

    assert "Repository: bankingscience/ATPDataHandlersRepo" in prompt
    assert "Source branch: develop" in prompt
    assert "Source context path: atp-handlers/src/main/resources/readme" in prompt
    assert "Required target path: README.md" in prompt
    assert "inspect the repository structure" in prompt
    assert "Verify every command, path, dependency" in prompt
    assert "never commit placeholders" in prompt
    assert "call validate_content" in prompt
    assert "replace_in_file" in prompt
    assert "truncated existing text file" in prompt


def test_calculator_profile_prompt_does_not_add_architecture_specific_completion_rule():
    payload = _payload("refactor", "experiments/shared/input.csv")
    payload["execution_objectives"]["parsed_task_parameters"].update(
        {
            "quant_calculator_enabled": True,
            "quant_calculator_schema_path": "experiments/shared/output_schema.json",
        }
    )
    payload["strategy"].update(
        {
            "target_path": "experiments/shared/submissions/result.json",
            "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
        }
    )

    prompt = build_mcp_code_prompt(
        payload,
        read_ref="exp3/source-q5",
        branch_name="quant/E3V2T3Q5-T3-R1-M0",
    )

    assert "frozen task text exactly" in prompt
    assert "sole source of answer-\n   capture" in prompt
    assert "Do not add an outer prompt completion rule" in prompt
    assert "does not make a target-file commit a condition" in prompt
    assert 'Finish exactly "experiments/shared/submissions/result.json"' not in prompt
    assert "The commit message must begin" not in prompt
    assert "correct the reported mapping and retry" not in prompt
    assert "call validate_content with its complete proposed" not in prompt


def test_mcp_prompt_includes_every_repository_branch_and_scope():
    payload = _payload("refactor", "rae_runtime/proxy/run.py")
    payload["strategy"].update(
        {
            "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
            "ref": "main",
            "target_branch": "quant/SCRUM-90",
            "allowed_directories": ["rae_runtime"],
        }
    )
    payload["repositories"] = [
        {
            "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
            "source_branch": "main",
            "target_branch": "quant/SCRUM-90",
            "allowed_directories": ["rae_runtime"],
        },
        {
            "repo_full_name": "bankingscience/ATPDataHandlersRepo",
            "source_branch": "develop",
            "target_branch": "quant/SCRUM-90",
            "allowed_directories": ["."],
        },
    ]

    prompt = build_mcp_code_prompt(
        payload,
        read_ref="main",
        branch_name="quant/SCRUM-90",
    )

    assert (
        "bankingscience/BSLAgenticQuantDevLoop: source=main; "
        "target=quant/SCRUM-90; scope=rae_runtime" in prompt
    )
    assert (
        "bankingscience/ATPDataHandlersRepo: source=develop; "
        "target=quant/SCRUM-90; scope=." in prompt
    )
    assert "apply only to the primary" in prompt
    assert "including secondary repos" in prompt
