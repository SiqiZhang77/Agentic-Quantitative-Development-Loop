import pipeline


def test_scripted_pipeline_blocks_placeholder_before_github_write(monkeypatch):
    writes = []
    monkeypatch.setattr(
        pipeline,
        "create_feature_branch",
        lambda **kwargs: "Created branch quant/SCRUM-115 from develop",
    )
    monkeypatch.setattr(
        pipeline,
        "get_strategy_code",
        lambda **kwargs: "Existing repository notes",
    )
    monkeypatch.setattr(
        pipeline,
        "call_llm",
        lambda *args, **kwargs: (
            "Detailed content for the comprehensive README will be inserted here."
        ),
    )
    monkeypatch.setattr(
        pipeline,
        "push_strategy_code",
        lambda **kwargs: writes.append(kwargs) or "unexpected write",
    )

    result = pipeline.run_pipeline(
        {
            "issue_key": "SCRUM-115",
            "command": "other",
            "strategy": {
                "ref": "develop",
                "path": "atp-handlers/src/main/resources/readme",
                "source_path": "atp-handlers/src/main/resources/readme",
                "target_path": "README.md",
                "target_path_explicit": True,
                "target_branch": "quant/SCRUM-115",
                "repo_full_name": "bankingscience/ATPDataHandlersRepo",
            },
            "iteration_controls": {},
        }
    )

    assert result["status"] == "no_changes"
    assert result["artifacts"]["validation_issues"]
    assert result["diagnostics"]["retry_safe"]["commit"]["action"] == (
        "validation_failed"
    )
    assert writes == []


def test_scripted_pipeline_marks_text_free_delivery_at_coding_model_boundary(
    monkeypatch,
):
    captured_prompts = []
    payload = {
        "issue_key": "SCRUM-115",
        "command": "other",
        "strategy": {
            "ref": "develop",
            "path": "README.md",
            "source_path": "README.md",
            "target_path": "README.md",
            "target_branch": "quant/SCRUM-115",
            "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
        },
        "iteration_controls": {},
        "retrieval_context": {
            "enabled": True,
            "status": "ok",
            "memories": [
                {
                    "memory_id": "MEM-01",
                    "source_ticket_id": "SCRUM-4",
                    "source_type": "comment",
                    "source_id": "comment:10001",
                    "source_timestamp": "2026-06-01T00:00:00+00:00",
                    "rank": 1,
                    "score": 2.5,
                    "text": "PRIVATE-RAG-MEMORY-SENTINEL",
                }
            ],
        },
    }
    monkeypatch.setattr(
        pipeline,
        "create_feature_branch",
        lambda **kwargs: "Created branch quant/SCRUM-115 from develop",
    )
    monkeypatch.setattr(
        pipeline,
        "get_strategy_code",
        lambda **kwargs: "Existing repository notes",
    )

    def fake_llm(prompt, **kwargs):
        captured_prompts.append(prompt)
        assert payload["_retrieval_delivery"]["prompt_injected"] is True
        return "Detailed content for the comprehensive README will be inserted here."

    monkeypatch.setattr(pipeline, "call_llm", fake_llm)
    monkeypatch.setattr(
        pipeline,
        "push_strategy_code",
        lambda **kwargs: "unexpected write",
    )

    pipeline.run_pipeline(payload)

    assert len(captured_prompts) == 1
    assert "PRIVATE-RAG-MEMORY-SENTINEL" in captured_prompts[0]
    assert "PRIVATE-RAG-MEMORY-SENTINEL" not in str(
        payload["_retrieval_delivery"]
    )
    assert payload["_retrieval_delivery"]["injected_memory_ids"] == ["MEM-01"]
