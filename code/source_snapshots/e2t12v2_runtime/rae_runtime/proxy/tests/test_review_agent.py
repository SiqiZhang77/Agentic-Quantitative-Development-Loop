"""The non-metric evaluator that lets general (non-backtest) requests iterate,
and the read-only analysis that answers a ticket without changing anything."""

from pathlib import Path

import pytest

from review_agent import analyse_read_only_task, review_general_task


def _payload(**overrides):
    payload = {
        "issue_key": "SCRUM-9",
        "command": "refactor",
        "strategy": {"path": "proxy/github_client.py"},
        "execution_objectives": {"strategy_type": "refactor"},
        "jira_context": {
            "ticket_id": "SCRUM-9",
            "summary": "Add retry backoff",
            "description": "Retries hammer the API.",
            "triggering_comment": {"timestamp": "t", "author": "luc", "text": "/quant"},
            "events_history": [],
        },
        "pipeline_out": {
            "status": "succeeded",
            "artifacts": {"feature_branch": "quant/SCRUM-9"},
        },
    }
    payload.update(overrides)
    return payload


def _reader(code="def retry(): pass"):
    return lambda branch, path, repo_name: code


def _llm(reply):
    return lambda prompt: reply


@pytest.fixture(autouse=True)
def _online(monkeypatch):
    monkeypatch.delenv("RAE_OFFLINE", raising=False)


def test_verdict_met_accepts():
    out = review_general_task(
        _payload(),
        code_reader=_reader(),
        llm=_llm('{"met_criteria": true, "summary": "Backoff is implemented."}'),
    )

    assert out["recommended_action"] == "accept"
    assert out["evaluation"]["met_criteria"] is True
    assert "Backoff is implemented." in out["evaluation"]["summary"]


def test_review_agent_never_receives_retrieved_jira_memory():
    prompts = []
    payload = _payload()
    payload["retrieval_context"] = {
        "enabled": True,
        "status": "ok",
        "memories": [
            {
                "memory_id": "MEM-SENTINEL",
                "source_ticket_id": "SCRUM-4",
                "source_type": "comment",
                "source_id": "comment:10001",
                "source_timestamp": "2026-06-01T00:00:00+00:00",
                "rank": 1,
                "score": 2.5,
                "text": "PRIVATE-RAG-MEMORY-SENTINEL",
            }
        ],
    }

    review_general_task(
        payload,
        code_reader=_reader(),
        llm=lambda prompt: prompts.append(prompt)
        or '{"met_criteria": true, "summary": "Backoff is implemented."}',
    )

    assert len(prompts) == 1
    assert "PRIVATE-RAG-MEMORY-SENTINEL" not in prompts[0]
    assert "RETRIEVED JIRA MEMORY" not in prompts[0]


def test_read_only_analysis_agent_does_not_receive_retrieved_jira_memory():
    prompts = []
    payload = _payload(command="analysis")
    payload["retrieval_context"] = {
        "enabled": True,
        "status": "ok",
        "memories": [
            {
                "memory_id": "MEM-SENTINEL",
                "text": "PRIVATE-RAG-MEMORY-SENTINEL",
            }
        ],
    }

    analyse_read_only_task(
        payload,
        code_reader=_reader(),
        llm=lambda prompt: prompts.append(prompt)
        or '{"summary": "Analysis complete."}',
    )

    assert len(prompts) == 1
    assert "PRIVATE-RAG-MEMORY-SENTINEL" not in prompts[0]
    assert "RETRIEVED JIRA MEMORY" not in prompts[0]


def test_accepted_verdict_is_overridden_when_the_artifact_is_unparseable():
    # Observed on SCRUM-108: the reviewer read a file containing only prose and
    # reported that it implemented the request.
    out = review_general_task(
        _payload(strategy={"path": "pkg/multi_horizon_nsga2.py"}),
        code_reader=_reader("Full implementation as described above"),
        llm=_llm(
            '{"met_criteria": true, "summary": "The file now contains the '
            'required primitives and unit tests."}'
        ),
    )

    assert out["recommended_action"] == "iterate"
    assert out["evaluation"]["met_criteria"] is False
    assert "deterministic artifact validation rejected it" in (
        out["evaluation"]["summary"]
    )
    # The reviewer's own wording is dropped in favour of the concrete reason.
    assert "required primitives" not in out["evaluation"]["summary"]


def test_accepted_verdict_is_overridden_when_the_pass_threw_away_previous_work():
    # SCRUM-108: pass 1 wrote real signatures, pass 2 replaced them with a line
    # of prose, and the reviewer accepted the second pass.
    payload = _payload(strategy={"path": "pkg/multi_horizon_nsga2.py"})
    payload["_previous_reviewed_sizes"] = {
        "bankingscience/BSLAgenticQuantDevLoop:pkg/multi_horizon_nsga2.py": 1200
    }

    out = review_general_task(
        payload,
        code_reader=_reader("def sort_population(pop):\n    return pop\n"),
        llm=_llm('{"met_criteria": true, "summary": "Looks complete."}'),
    )

    assert out["recommended_action"] == "iterate"
    assert "shrank from 1200 to" in out["evaluation"]["summary"]


def test_growing_a_file_across_iterations_is_not_a_regression():
    payload = _payload(strategy={"path": "pkg/operators.py"})
    payload["_previous_reviewed_sizes"] = {
        "bankingscience/BSLAgenticQuantDevLoop:pkg/operators.py": 120
    }

    out = review_general_task(
        payload,
        code_reader=_reader("def cs_rank(frame):\n    return frame\n" * 20),
        llm=_llm('{"met_criteria": true, "summary": "Implemented."}'),
    )

    assert out["recommended_action"] == "accept"


def test_reviewed_sizes_are_reported_for_the_next_iteration():
    out = review_general_task(
        _payload(strategy={"path": "pkg/operators.py"}),
        code_reader=_reader("def cs_rank(frame):\n    return frame\n"),
        llm=_llm('{"met_criteria": true, "summary": "Implemented."}'),
    )

    # Repository-qualified, so two repos holding the same path stay distinct.
    assert out["reviewed_sizes"] == {
        "bankingscience/BSLAgenticQuantDevLoop:pkg/operators.py": len(
            "def cs_rank(frame):\n    return frame"
        )
    }


def test_reviewed_sizes_keep_same_path_in_two_repositories_distinct():
    payload = _payload(
        strategy={"path": "shared/util.py"},
        pipeline_out={
            "status": "succeeded",
            "artifacts": {
                "feature_branch": "quant/SCRUM-9",
                "repository_branches": [
                    {
                        "repo_full_name": "bankingscience/RepoA",
                        "target_branch": "quant/SCRUM-9",
                        "modified_files": ["shared/util.py"],
                        "new_files": [],
                    },
                    {
                        "repo_full_name": "bankingscience/RepoB",
                        "target_branch": "quant/SCRUM-9",
                        "modified_files": ["shared/util.py"],
                        "new_files": [],
                    },
                ],
            },
        },
    )
    bodies = {
        "bankingscience/RepoA": "def a():\n    return 1\n" * 30,
        "bankingscience/RepoB": "def b():\n    return 2\n",
    }

    out = review_general_task(
        payload,
        code_reader=lambda branch, path, repo_name: bodies[repo_name],
        llm=_llm('{"met_criteria": true, "summary": "ok"}'),
    )

    assert set(out["reviewed_sizes"]) == {
        "bankingscience/RepoA:shared/util.py",
        "bankingscience/RepoB:shared/util.py",
    }


def test_accepted_verdict_stands_when_the_artifact_is_valid():
    out = review_general_task(
        _payload(strategy={"path": "pkg/operators.py"}),
        code_reader=_reader("def cs_rank(frame):\n    return frame\n"),
        llm=_llm('{"met_criteria": true, "summary": "cs_rank is implemented."}'),
    )

    assert out["recommended_action"] == "accept"
    assert out["evaluation"]["met_criteria"] is True


def test_verdict_not_met_iterates_and_feeds_back_what_is_missing():
    out = review_general_task(
        _payload(),
        code_reader=_reader(),
        llm=_llm('{"met_criteria": false, "summary": "No backoff between retries."}'),
    )

    assert out["recommended_action"] == "iterate"
    assert out["evaluation"]["met_criteria"] is False
    # The loop feeds this summary into the next attempt's brief.
    assert "No backoff between retries." in out["evaluation"]["summary"]


def test_verdict_is_labelled_advisory_not_a_measurement():
    out = review_general_task(
        _payload(),
        code_reader=_reader(),
        llm=_llm('{"met_criteria": true, "summary": "Looks right."}'),
    )

    assert "advisory" in out["evaluation"]["summary"]
    # A prose judgement is not numerically scorable, so it must not claim to be.
    assert out["evaluation"]["confidence"] == 0.0
    assert out["evaluation"]["criteria_results"] == []


def test_verdict_wrapped_in_prose_or_fences_is_still_read():
    reply = 'Here is my review:\n```json\n{"met_criteria": false, "summary": "Nope."}\n```'
    out = review_general_task(_payload(), code_reader=_reader(), llm=_llm(reply))

    assert out["recommended_action"] == "iterate"


@pytest.mark.parametrize(
    "reply",
    [
        "not json at all",
        '{"summary": "no verdict field"}',
        '{"met_criteria": "yes"}',  # not a boolean
        "",
    ],
)
def test_unusable_verdict_asks_for_review_rather_than_guessing(reply):
    out = review_general_task(_payload(), code_reader=_reader(), llm=_llm(reply))

    assert out["recommended_action"] == "review"
    assert out["evaluation"]["met_criteria"] is None


def test_no_code_changes_is_reviewed_not_iterated():
    # Re-running the same edit that already declined to change anything cannot help.
    payload = _payload(
        pipeline_out={"status": "no_changes", "artifacts": {"no_code_changes": True}}
    )

    def _fail(*a, **k):
        raise AssertionError("must not call the model when nothing changed")

    out = review_general_task(payload, code_reader=_fail, llm=_fail)

    assert out["recommended_action"] == "review"
    assert out["evaluation"]["met_criteria"] is None


def test_deterministic_artifact_failures_iterate_without_model_or_extra_reads():
    payload = _payload(
        pipeline_out={
            "status": "succeeded",
            "artifacts": {
                "feature_branch": "quant/SCRUM-9",
                "new_files": ["README.md"],
                "validation_issues": ["README artifact README.md is too short."],
            },
        }
    )

    def _fail(*args, **kwargs):
        raise AssertionError("validation must short-circuit review reads and model calls")

    out = review_general_task(payload, code_reader=_fail, llm=_fail)

    assert out["recommended_action"] == "iterate"
    assert "README.md is too short" in out["evaluation"]["summary"]


def test_reviewer_reads_new_atp_file_from_selected_repository():
    reads = []
    payload = _payload(
        strategy={
            "path": "atp-handlers/src/main/resources/readme",
            "target_path": "README.md",
            "repo_full_name": "bankingscience/ATPDataHandlersRepo",
        },
        pipeline_out={
            "status": "succeeded",
            "artifacts": {
                "feature_branch": "quant/SCRUM-115",
                "modified_files": [],
                "new_files": ["README.md"],
            },
        },
    )

    def reader(branch, path, repo_name):
        reads.append((branch, path, repo_name))
        # Substantive enough to pass the deterministic README checks, which now
        # also gate an accepted verdict.
        return "# ATP Data Handlers\n\n" + ("Verified handler documentation. " * 12)

    out = review_general_task(
        payload,
        code_reader=reader,
        llm=_llm('{"met_criteria": true, "summary": "ok"}'),
    )

    assert out["recommended_action"] == "accept"
    assert reads == [
        ("quant/SCRUM-115", "README.md", "bankingscience/ATPDataHandlersRepo")
    ]


def test_a_broken_reviewer_never_fails_the_run():
    # The edit already succeeded; a reviewer outage must not turn it into a failure.
    def _boom(*a, **k):
        raise RuntimeError("litellm unreachable")

    out = review_general_task(_payload(), code_reader=_reader(), llm=_boom)

    assert out["recommended_action"] == "review"
    assert out["evaluation"]["met_criteria"] is None
    assert "could not be reached" in out["evaluation"]["summary"]


def test_unreadable_file_degrades_to_review():
    def _boom(*a, **k):
        raise RuntimeError("404 not found")

    out = review_general_task(_payload(), code_reader=_boom, llm=_llm("{}"))

    assert out["recommended_action"] == "review"
    assert out["evaluation"]["met_criteria"] is None


def test_offline_runs_are_not_reviewed(monkeypatch):
    monkeypatch.setenv("RAE_OFFLINE", "1")

    def _fail(*a, **k):
        raise AssertionError("must not touch the network offline")

    out = review_general_task(_payload(), code_reader=_fail, llm=_fail)

    assert out["recommended_action"] == "review"


def test_reviewer_reads_the_committed_file_from_the_ticket_branch():
    seen = {}

    def reader(branch, path, repo_name):
        seen["branch"], seen["path"], seen["repo_name"] = branch, path, repo_name
        return "code"

    review_general_task(
        _payload(),
        code_reader=reader,
        llm=_llm('{"met_criteria": true, "summary": "ok"}'),
    )

    assert seen == {
        "branch": "quant/SCRUM-9",
        "path": "proxy/github_client.py",
        "repo_name": "bankingscience/BSLAgenticQuantDevLoop",
    }


def test_reviewer_reads_every_modified_file_and_includes_them_in_the_prompt():
    reads = []
    prompts = []
    payload = _payload(
        pipeline_out={
            "status": "succeeded",
            "artifacts": {
                "feature_branch": "quant/SCRUM-9",
                "modified_files": ["proxy/a.py", "proxy/b.py"],
            },
        }
    )

    def reader(branch, path, repo_name):
        reads.append((branch, path, repo_name))
        return f"# contents of {path}\n"

    def llm(prompt):
        prompts.append(prompt)
        return '{"met_criteria": true, "summary": "ok"}'

    out = review_general_task(payload, code_reader=reader, llm=llm)

    assert out["recommended_action"] == "accept"
    assert reads == [
        ("quant/SCRUM-9", "proxy/a.py", "bankingscience/BSLAgenticQuantDevLoop"),
        ("quant/SCRUM-9", "proxy/b.py", "bankingscience/BSLAgenticQuantDevLoop"),
    ]
    assert "CURRENT CONTENTS OF proxy/a.py" in prompts[0]
    assert "CURRENT CONTENTS OF proxy/b.py" in prompts[0]
    assert "CURRENT CONTENTS OF proxy/github_client.py" not in prompts[0]


def test_reviewer_reads_changes_from_every_reported_repository():
    reads = []
    prompts = []
    payload = _payload(
        pipeline_out={
            "status": "succeeded",
            "artifacts": {
                "feature_branch": "quant/SCRUM-9",
                "modified_files": ["rae_runtime/proxy/run.py"],
                "repository_branches": [
                    {
                        "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
                        "target_branch": "quant/SCRUM-9",
                        "modified_files": ["rae_runtime/proxy/run.py"],
                        "new_files": [],
                    },
                    {
                        "repo_full_name": "bankingscience/ATPDataHandlersRepo",
                        "target_branch": "quant/SCRUM-9",
                        "modified_files": [],
                        "new_files": ["SCRUM-9-canary.txt"],
                    },
                ],
            },
        }
    )

    def reader(branch, path, repo_name):
        reads.append((repo_name, branch, path))
        return f"# contents of {repo_name}:{path}\n"

    def llm(prompt):
        prompts.append(prompt)
        return '{"met_criteria": true, "summary": "ok"}'

    out = review_general_task(payload, code_reader=reader, llm=llm)

    assert out["recommended_action"] == "accept"
    assert reads == [
        (
            "bankingscience/BSLAgenticQuantDevLoop",
            "quant/SCRUM-9",
            "rae_runtime/proxy/run.py",
        ),
        (
            "bankingscience/ATPDataHandlersRepo",
            "quant/SCRUM-9",
            "SCRUM-9-canary.txt",
        ),
    ]
    assert "bankingscience/BSLAgenticQuantDevLoop@quant/SCRUM-9" in prompts[0]
    assert "bankingscience/ATPDataHandlersRepo@quant/SCRUM-9" in prompts[0]


class TestReadOnlyAnalysis:
    """A zero_code_modifications run has no edit to score: the findings are the
    whole result, so they must reach the ticket."""

    def _payload(self, **overrides):
        payload = {
            "issue_key": "SCRUM-9",
            "command": "analysis",
            "strategy": {"path": "proxy/github_client.py", "ref": "main"},
            "execution_objectives": {
                "strategy_type": "analysis",
                "zero_code_modifications": True,
            },
            "jira_context": {
                "ticket_id": "SCRUM-9",
                "summary": "Does the client retry safely?",
                "description": "Checking retry behaviour.",
                "triggering_comment": {"timestamp": "t", "author": "luc", "text": "/q"},
                "events_history": [],
            },
        }
        payload.update(overrides)
        return payload

    def test_findings_are_reported_for_a_human_to_read(self):
        out = analyse_read_only_task(
            self._payload(),
            code_reader=_reader("def retry(): ..."),
            llm=_llm('{"summary": "retry() has no backoff between attempts."}'),
        )

        assert "retry() has no backoff between attempts." in out["evaluation"]["summary"]
        # Nothing was scored and nothing was changed, so a human decides.
        assert out["recommended_action"] == "review"
        assert out["evaluation"]["met_criteria"] is None

    def test_findings_are_labelled_advisory(self):
        out = analyse_read_only_task(
            self._payload(),
            code_reader=_reader(),
            llm=_llm('{"summary": "Looks fine."}'),
        )

        assert "advisory" in out["evaluation"]["summary"]

    def test_csv_input_dataset_is_measured_without_reading_repository_code(
        self,
        tmp_path,
    ):
        dataset = tmp_path / "experiment_2_input.csv"
        dataset.write_text(
            "date,fund_return,benchmark_return\n"
            "2024-01-31,0.01,0.008\n"
            "2024-02-29,-0.02,-0.015\n",
            encoding="utf-8",
        )
        prompts = []
        replies = iter(
            [
                '{"operations":[{"type":"row_count","dataset_id":"experiment_2_input"},'
                '{"type":"missing_counts","dataset_id":"experiment_2_input"}]}',
                '{"summary":"The complete dataset has two rows and no missing values."}',
            ]
        )

        def no_repo_read(*args, **kwargs):
            raise AssertionError("dataset analysis must not read repository code")

        def llm(prompt):
            prompts.append(prompt)
            return next(replies)

        payload = self._payload(
            input_datasets=[
                {
                    "dataset_id": "experiment_2_input",
                    "source_kind": "hdfs",
                    "original_filename": dataset.name,
                    "source_uri": "hdfs://namenode:8020/approved/input.csv",
                    "container_path": str(dataset),
                    "format": "csv",
                    "read_only": True,
                    "size_bytes": dataset.stat().st_size,
                    "sha256": "a" * 64,
                }
            ]
        )

        out = analyse_read_only_task(
            payload,
            code_reader=no_repo_read,
            llm=llm,
        )

        assert "Measured read-only dataset analysis" in out["evaluation"]["summary"]
        assert out["analysis_results"]["operations"][0]["result"]["row_count"] == 2
        assert out["analysis_results"]["operations"][1]["result"]["missing_counts"] == {
            "date": 0,
            "fund_return": 0,
            "benchmark_return": 0,
        }
        assert "fund_return" in prompts[0]
        assert "complete mounted datasets" in prompts[0]
        assert "0.01,0.008" not in prompts[0]
        assert len(prompts) == 2

    def test_it_reads_the_source_ref_since_a_read_only_run_has_no_branch(self):
        seen = {}

        def reader(branch, path, repo_name):
            seen["branch"], seen["path"], seen["repo_name"] = branch, path, repo_name
            return "code"

        analyse_read_only_task(
            self._payload(),
            code_reader=reader,
            llm=_llm('{"summary": "ok"}'),
        )

        assert seen == {
            "branch": "main",
            "path": "proxy/github_client.py",
            "repo_name": "bankingscience/BSLAgenticQuantDevLoop",
        }

    def test_prose_outside_the_json_envelope_is_still_reported(self):
        # Findings are prose; losing them to a formatting slip wastes the whole run.
        out = analyse_read_only_task(
            self._payload(),
            code_reader=_reader(),
            llm=_llm("The retry loop has no backoff."),
        )

        assert "The retry loop has no backoff." in out["evaluation"]["summary"]

    def test_an_unreachable_model_degrades_to_review(self):
        def _boom(*a, **k):
            raise RuntimeError("litellm unreachable")

        out = analyse_read_only_task(
            self._payload(), code_reader=_reader(), llm=_boom
        )

        assert out["recommended_action"] == "review"
        assert "could not be run" in out["evaluation"]["summary"]

    def test_an_unreadable_file_degrades_to_review(self):
        def _boom(*a, **k):
            raise RuntimeError("404")

        out = analyse_read_only_task(
            self._payload(), code_reader=_boom, llm=_llm('{"summary": "x"}')
        )

        assert out["recommended_action"] == "review"
        assert "Could not read" in out["evaluation"]["summary"]

    def test_no_target_file_degrades_to_review(self):
        def _fail(*a, **k):
            raise AssertionError("nothing to read")

        out = analyse_read_only_task(
            self._payload(strategy={}), code_reader=_fail, llm=_fail
        )

        assert out["recommended_action"] == "review"

    def test_offline_runs_are_not_analysed(self, monkeypatch):
        monkeypatch.setenv("RAE_OFFLINE", "1")

        def _fail(*a, **k):
            raise AssertionError("must not touch the network offline")

        out = analyse_read_only_task(self._payload(), code_reader=_fail, llm=_fail)

        assert out["recommended_action"] == "review"


def test_pipeline_out_is_read_from_the_payload_when_not_passed():
    # run_iteration_loop hands this pass's edit result through the payload.
    payload = _payload(
        pipeline_out={"status": "no_changes", "artifacts": {"no_code_changes": True}}
    )

    def _fail(*a, **k):
        raise AssertionError("should have short-circuited on no_changes")

    out = review_general_task(payload, code_reader=_fail, llm=_fail)

    assert out["recommended_action"] == "review"


def test_reused_valid_target_is_reviewed_without_forcing_a_commit():
    payload = _payload(
        pipeline_out={
            "status": "no_changes",
            "artifacts": {
                "no_code_changes": True,
                "repository_branches": [
                    {
                        "repo_full_name": "bankingscience/BSLAgenticQuantDevLoop",
                        "target_branch": "quant/T-1",
                        "existing_files": ["README.md"],
                    }
                ],
            },
        }
    )

    out = review_general_task(
        payload,
        code_reader=lambda **kwargs: "# Verified README\n\n" + ("Evidence. " * 30),
        llm=_llm('{"met_criteria": true, "summary": "Existing target satisfies the ticket."}'),
    )

    assert out["recommended_action"] == "accept"
