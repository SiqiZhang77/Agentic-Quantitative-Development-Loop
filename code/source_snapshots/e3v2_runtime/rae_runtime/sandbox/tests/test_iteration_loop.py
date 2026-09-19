"""Unit tests for the RAE-18 iteration loop (no network; conftest puts sandbox on sys.path)."""

import json

import pytest

import iteration_loop
from budget_guard import RunTimeout
from budget_guard import BudgetExceeded
from iteration_loop import run_iteration_loop, _with_feedback


class _Tracer:
    """Minimal stand-in for TraceCollector; iteration count == number of records."""

    def __init__(self):
        self.records = []

    def record(self, agent, tool_call, status, message):
        self.records.append((agent, tool_call, status, message))


def _payload(**extra):
    # command=backtest by default: these cases exercise the iterating path, and
    # iteration now defaults on only for backtest runs (a general run's verdict is
    # an advisory review, so it is single-pass unless it opts in).
    p = {
        "issue_key": "T-1",
        "command": "backtest",
        "args": {"strategy": "improve the strategy"},
    }
    p.update(extra)
    return p


def _run(actions, max_iterations=5):
    """Run the loop against a scripted recommended_action sequence; return (tracer, backtest)."""
    seq = iter(actions)

    def backtest_fn(payload):
        action = next(seq)
        return {
            "metrics": {"sharpe_ratio": 1.0},
            "recommended_action": action,
            "evaluation": {"summary": f"outcome for action={action}"},
        }

    tracer = _Tracer()
    out = run_iteration_loop(
        _payload(execution_objectives={"max_iterations": max_iterations}),
        lambda pl: {},
        backtest_fn,
        tracer=tracer,
    )
    return tracer, out["backtest"]


def _run_with_controls(actions, iteration_controls):
    """As _run, but drives the loop through iteration_controls."""
    seq = iter(actions)

    def backtest_fn(payload):
        return {"recommended_action": next(seq)}

    tracer = _Tracer()
    out = run_iteration_loop(
        _payload(iteration_controls=iteration_controls),
        lambda pl: {},
        backtest_fn,
        tracer=tracer,
    )
    return tracer, out["backtest"]


def test_stops_on_accept():
    tracer, backtest = _run(["iterate", "iterate", "accept"])
    assert len(tracer.records) == 3
    assert backtest["recommended_action"] == "accept"
    assert tracer.records[0][0] == "iteration_loop"  # each iteration surfaces in iteration_traces


def test_retryable_failed_attempt_does_not_consume_completed_iteration_budget():
    attempts = []

    def edit(payload):
        attempts.append(payload)
        if len(attempts) == 1:
            raise RuntimeError("Max turns (30) exceeded")
        return {"status": "succeeded", "artifacts": {}}

    out = run_iteration_loop(
        _payload(iteration_controls={"allow_iteration": True, "max_iterations": 1, "max_failed_iterations": 2}),
        edit,
        lambda payload: {"recommended_action": "accept"},
    )

    assert len(attempts) == 2
    assert out["backtest"]["recommended_action"] == "accept"


def test_token_budget_exhaustion_is_terminal_even_when_failed_attempts_remain():
    calls = []

    def edit(payload):
        calls.append(payload)
        return {"status": "succeeded", "usage": {"total_tokens": 11}}

    with pytest.raises(BudgetExceeded):
        run_iteration_loop(
            _payload(
                iteration_controls={
                    "allow_iteration": True,
                    "max_iterations": 2,
                    "max_failed_iterations": 2,
                    "max_token_budget_per_run": 10,
                }
            ),
            edit,
            lambda payload: {"recommended_action": "accept"},
        )

    assert len(calls) == 1


def test_token_budget_failure_preserves_usage_and_committed_artifacts():
    pipeline_out = {
        "status": "succeeded",
        "usage": {
            "model": "qwen3-coder",
            "calls": 23,
            "prompt_tokens": 198000,
            "completion_tokens": 3001,
            "total_tokens": 201001,
            "cost_usd": None,
        },
        "artifacts": {
            "modified_files": ["src/example.py"],
            "new_files": ["tests/test_example.py"],
            "repository_branches": [
                {
                    "repo_full_name": "bankingscience/RepoA",
                    "target_branch": "quant/T-1",
                    "commit_sha": "committed-sha",
                    "modified_files": ["src/example.py"],
                    "new_files": ["tests/test_example.py"],
                }
            ],
        },
    }

    with pytest.raises(BudgetExceeded) as raised:
        run_iteration_loop(
            _payload(
                iteration_controls={
                    "allow_iteration": False,
                    "max_token_budget_per_run": 200000,
                }
            ),
            lambda payload: pipeline_out,
            lambda payload: {"recommended_action": "accept"},
        )

    assert raised.value.consumed_tokens == 201001
    assert raised.value.pipeline_out["usage"] == pipeline_out["usage"]
    assert raised.value.pipeline_out["artifacts"] == pipeline_out["artifacts"]


def test_later_budget_failure_preserves_cumulative_usage_and_artifacts():
    edits = iter(
        [
            {
                "status": "succeeded",
                "usage": {
                    "model": "qwen3-coder",
                    "calls": 2,
                    "prompt_tokens": 60,
                    "completion_tokens": 10,
                    "total_tokens": 70,
                    "cost_usd": 0.1,
                },
                "artifacts": {
                    "modified_files": ["src/first.py"],
                    "new_files": [],
                },
            },
            {
                "status": "succeeded",
                "usage": {
                    "model": "qwen3-coder",
                    "calls": 3,
                    "prompt_tokens": 30,
                    "completion_tokens": 11,
                    "total_tokens": 41,
                    "cost_usd": 0.2,
                },
                "artifacts": {
                    "modified_files": ["src/second.py"],
                    "new_files": [],
                },
            },
        ]
    )
    actions = iter(["iterate"])

    with pytest.raises(BudgetExceeded) as raised:
        run_iteration_loop(
            _payload(
                iteration_controls={
                    "allow_iteration": True,
                    "max_iterations": 2,
                    "max_token_budget_per_run": 100,
                }
            ),
            lambda payload: next(edits),
            lambda payload: {"recommended_action": next(actions), "evaluation": {}},
        )

    assert raised.value.pipeline_out["usage"] == {
        "model": "qwen3-coder",
        "calls": 5,
        "prompt_tokens": 90,
        "completion_tokens": 21,
        "total_tokens": 111,
        "cost_usd": 0.3,
    }
    assert raised.value.pipeline_out["artifacts"]["modified_files"] == [
        "src/first.py",
        "src/second.py",
    ]


def test_bound_holds():
    tracer, _ = _run(["iterate"] * 10, max_iterations=3)
    assert len(tracer.records) == 3  # never exceeds the cap


def test_allow_iteration_false_pins_the_run_to_one_pass():
    seq = iter(["iterate"] * 5)
    passes = []

    def backtest_fn(payload):
        return {"recommended_action": next(seq)}

    def edit_fn(payload):
        passes.append(1)
        return {}

    run_iteration_loop(
        _payload(
            iteration_controls={"allow_iteration": False, "max_iterations": 5}
        ),
        edit_fn,
        backtest_fn,
    )

    # The evaluator kept saying "iterate"; the flag is the explicit control and wins.
    assert len(passes) == 1


def test_allow_iteration_true_still_iterates():
    tracer, _ = _run_with_controls(
        ["iterate", "accept"], {"allow_iteration": True, "max_iterations": 5}
    )

    assert len(tracer.records) == 2


def test_backtest_runs_iterate_by_default_when_the_flag_is_absent():
    seq = iter(["iterate", "accept"])
    tracer = _Tracer()
    run_iteration_loop(
        _payload(command="backtest", iteration_controls={"max_iterations": 5}),
        lambda pl: {},
        lambda p: {"recommended_action": next(seq)},
        tracer=tracer,
    )

    assert len(tracer.records) == 2


@pytest.mark.parametrize("command", ["refactor", "ingestion", "analysis", "other"])
def test_general_runs_are_single_pass_by_default_when_the_flag_is_absent(command):
    # Their verdict is an advisory prose review, not a measurement, so they do not
    # spend further passes on it unless the request opts in.
    passes = []
    run_iteration_loop(
        _payload(command=command, iteration_controls={"max_iterations": 5}),
        lambda pl: passes.append(1) or {},
        lambda p: {"recommended_action": "iterate"},
    )

    assert len(passes) == 1


def test_general_runs_iterate_when_the_request_opts_in():
    seq = iter(["iterate", "accept"])
    tracer = _Tracer()
    run_iteration_loop(
        _payload(
            command="refactor",
            iteration_controls={"allow_iteration": True, "max_iterations": 5},
        ),
        lambda pl: {},
        lambda p: {"recommended_action": next(seq)},
        tracer=tracer,
    )

    assert len(tracer.records) == 2


def test_allow_iteration_false_returns_a_failure_instead_of_retrying():
    calls = []

    def edit_fn(payload):
        calls.append(1)
        raise RuntimeError("edit blew up")

    with pytest.raises(RuntimeError, match="edit blew up"):
        run_iteration_loop(
            _payload(
                iteration_controls={
                    "allow_iteration": False,
                    "max_iterations": 5,
                    # Would normally buy two retries; the flag overrides it.
                    "max_failed_iterations": 3,
                }
            ),
            edit_fn,
            lambda p: {"recommended_action": "accept"},
        )

    assert len(calls) == 1


def test_a_run_whose_every_pass_failed_is_not_reported_as_success():
    # Reachable whenever max_failed_iterations > max_iterations: the failure budget
    # is never exhausted, so the loop used to fall out of the for and return
    # {"pipeline_out": None}, which build_response reported as "succeeded" with no
    # error message.
    def edit_fn(payload):
        raise RuntimeError("edit blew up")

    with pytest.raises(RuntimeError, match="edit blew up"):
        run_iteration_loop(
            _payload(
                iteration_controls={"max_iterations": 2, "max_failed_iterations": 3}
            ),
            edit_fn,
            lambda p: {"recommended_action": "accept"},
        )


def test_evaluator_failures_after_successful_edits_are_not_reported_as_success():
    # edit_fn returning a value does not complete an iteration: the change has not
    # been evaluated yet.  This used to leave pipeline_out populated and bypass the
    # all-failures guard below the loop.
    with pytest.raises(RuntimeError, match="evaluator blew up"):
        run_iteration_loop(
            _payload(
                iteration_controls={"max_iterations": 2, "max_failed_iterations": 3}
            ),
            lambda payload: {"status": "succeeded"},
            lambda payload: (_ for _ in ()).throw(
                RuntimeError("evaluator blew up")
            ),
        )


def test_a_failed_final_pass_does_not_return_a_stale_earlier_verdict():
    calls = 0

    def evaluator(payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"recommended_action": "iterate", "evaluation": {"summary": "more"}}
        raise RuntimeError("second evaluation failed")

    with pytest.raises(RuntimeError, match="second evaluation failed"):
        run_iteration_loop(
            _payload(
                iteration_controls={"max_iterations": 2, "max_failed_iterations": 3}
            ),
            lambda payload: {"status": "succeeded"},
            evaluator,
        )


def test_evaluator_receives_this_passes_pipeline_output():
    # The review agent scores the edit this pass just made.
    seen = {}

    def backtest_fn(payload):
        seen["pipeline_out"] = payload.get("pipeline_out")
        return {"recommended_action": "accept"}

    run_iteration_loop(
        _payload(iteration_controls={"max_iterations": 1}),
        lambda pl: {"status": "succeeded", "artifacts": {"feature_branch": "quant/T-1"}},
        backtest_fn,
    )

    assert seen["pipeline_out"] == {
        "status": "succeeded",
        "artifacts": {"feature_branch": "quant/T-1"},
    }


def test_later_no_op_preserves_prior_repository_artifacts_and_sha():
    edits = iter(
        [
            {
                "status": "succeeded",
                "artifacts": {
                    "modified_files": [],
                    "new_files": ["canary/T-1.txt"],
                    "repository_branches": [
                        {
                            "repo_full_name": "bankingscience/RepoA",
                            "target_branch": "quant/T-1",
                            "branch_action": "created",
                            "commit_sha": "sha-after-commit",
                            "modified_files": [],
                            "new_files": ["canary/T-1.txt"],
                        }
                    ],
                },
            },
            {
                "status": "no_changes",
                "artifacts": {
                    "modified_files": [],
                    "new_files": [],
                    "no_code_changes": True,
                    "repository_branches": [
                        {
                            "repo_full_name": "bankingscience/RepoA",
                            "target_branch": "quant/T-1",
                            "branch_action": "reused",
                            "commit_sha": "sha-after-commit",
                            "modified_files": [],
                            "new_files": [],
                        }
                    ],
                },
            },
        ]
    )
    actions = iter(["iterate", "accept"])
    reviewed = []

    def evaluate(payload):
        reviewed.append(payload["pipeline_out"])
        return {"recommended_action": next(actions), "evaluation": {}}

    out = run_iteration_loop(
        _payload(iteration_controls={"allow_iteration": True, "max_iterations": 2}),
        lambda payload: next(edits),
        evaluate,
    )

    final_artifacts = out["pipeline_out"]["artifacts"]
    assert final_artifacts["new_files"] == ["canary/T-1.txt"]
    assert "no_code_changes" not in final_artifacts
    assert final_artifacts["repository_branches"][0] == {
        "repo_full_name": "bankingscience/RepoA",
        "target_branch": "quant/T-1",
        "branch_action": "created",
        "commit_sha": "sha-after-commit",
        "modified_files": [],
        "new_files": ["canary/T-1.txt"],
    }
    assert reviewed[-1]["artifacts"] == final_artifacts


def test_iterations_union_files_across_repositories():
    def output(repo_name, path, sha):
        return {
            "status": "succeeded",
            "artifacts": {
                "modified_files": [path] if repo_name.endswith("RepoA") else [],
                "new_files": [],
                "repository_branches": [
                    {
                        "repo_full_name": repo_name,
                        "target_branch": "quant/T-1",
                        "branch_action": "reused",
                        "commit_sha": sha,
                        "modified_files": [path],
                        "new_files": [],
                    }
                ],
            },
        }

    edits = iter(
        [
            output("bankingscience/RepoA", "src/a.py", "sha-a"),
            output("bankingscience/RepoB", "src/b.py", "sha-b"),
        ]
    )
    actions = iter(["iterate", "accept"])
    out = run_iteration_loop(
        _payload(iteration_controls={"allow_iteration": True, "max_iterations": 2}),
        lambda payload: next(edits),
        lambda payload: {"recommended_action": next(actions), "evaluation": {}},
    )

    branches = out["pipeline_out"]["artifacts"]["repository_branches"]
    assert [(item["repo_full_name"], item["modified_files"]) for item in branches] == [
        ("bankingscience/RepoA", ["src/a.py"]),
        ("bankingscience/RepoB", ["src/b.py"]),
    ]


def test_iteration_controls_max_iterations_is_canonical():
    seq = iter(["iterate"] * 10)
    tracer = _Tracer()
    run_iteration_loop(
        _payload(
            iteration_controls={"max_iterations": 2},
            execution_objectives={"max_iterations": 5},
        ),
        lambda pl: {},
        lambda pl: {"recommended_action": next(seq), "evaluation": {}},
        tracer=tracer,
    )
    assert len(tracer.records) == 2


def test_timeout_seconds_stops_loop(monkeypatch):
    ticks = iter([0.0, 2.0])
    monkeypatch.setattr(iteration_loop.time, "monotonic", lambda: next(ticks))

    with pytest.raises(RunTimeout, match="timeout_seconds"):
        run_iteration_loop(
            _payload(iteration_controls={"max_iterations": 3, "timeout_seconds": 1}),
            lambda pl: {},
            lambda pl: {"recommended_action": "accept", "evaluation": {}},
        )


def test_failure_threshold_allows_configured_retry(tmp_path):
    calls = {"edit": 0}

    def edit_fn(payload):
        calls["edit"] += 1
        if calls["edit"] == 1:
            raise RuntimeError("temporary compile failure")
        return {}

    tracer = _Tracer()
    out = run_iteration_loop(
        _payload(
            iteration_controls={"max_iterations": 2, "max_failed_iterations": 2},
            output_paths={"artifact_dir": str(tmp_path)},
        ),
        edit_fn,
        lambda pl: {"recommended_action": "accept", "evaluation": {}},
        tracer=tracer,
    )

    state = json.loads((tmp_path / "iteration_state.json").read_text())
    assert out["backtest"]["recommended_action"] == "accept"
    assert calls["edit"] == 2
    assert state["failed_iterations"] == 1
    assert [item["status"] for item in state["iterations"]] == ["failed", "succeeded"]


def test_iteration_state_persisted(tmp_path):
    run_iteration_loop(
        _payload(
            run_id="run-1",
            iteration_controls={"max_iterations": 1},
            output_paths={"artifact_dir": str(tmp_path)},
        ),
        lambda pl: {},
        lambda pl: {
            "recommended_action": "accept",
            "evaluation": {"summary": "criteria met"},
        },
    )

    state = json.loads((tmp_path / "iteration_state.json").read_text())
    assert state["run_id"] == "run-1"
    assert state["status"] == "stopped"
    assert state["current_iteration"] == 1
    assert state["iterations"][0]["recommended_action"] == "accept"


def test_accept_first_pass():
    tracer, _ = _run(["accept"])
    assert len(tracer.records) == 1


def test_review_stops():
    tracer, _ = _run(["review"])
    assert len(tracer.records) == 1  # only "iterate" continues


def test_default_max_iterations(monkeypatch):
    monkeypatch.delenv("RAE_MAX_ITERATIONS", raising=False)
    seq = iter(["iterate"] * 10)
    tracer = _Tracer()
    run_iteration_loop(
        _payload(),  # no execution_objectives -> default cap of 3
        lambda pl: {},
        lambda pl: {"recommended_action": next(seq), "evaluation": {}},
        tracer=tracer,
    )
    assert len(tracer.records) == 3


def test_with_feedback_no_mutation():
    p = _payload()
    original = p["args"]["strategy"]
    updated = _with_feedback(p, {"summary": "sharpe too low"})
    assert p["args"]["strategy"] == original          # caller not mutated
    assert "sharpe too low" in updated["args"]["strategy"]
    assert updated is not p
