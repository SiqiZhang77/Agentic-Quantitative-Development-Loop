import pipeline_mcp


def _payload(source_path="src/app.py", target_path="src/app.py", override=None):
    controls = {}
    if override is not None:
        controls["max_agent_turns"] = override
    return {
        "strategy": {"source_path": source_path, "target_path": target_path},
        "iteration_controls": controls,
    }


def test_simple_edit_uses_small_default_turn_budget():
    assert pipeline_mcp._resolve_max_agent_turns(_payload(), 1) == 12


def test_repository_grounded_readme_uses_wide_turn_budget():
    assert pipeline_mcp._resolve_max_agent_turns(
        _payload("notes/readme", "README.md"), 1
    ) == 30


def test_multi_repository_budget_is_bounded():
    assert pipeline_mcp._resolve_max_agent_turns(_payload(None, None), 8) == 60


def test_requested_turn_budget_overrides_dynamic_default_with_safe_clamp():
    assert pipeline_mcp._resolve_max_agent_turns(_payload(override=45), 1) == 45
    assert pipeline_mcp._resolve_max_agent_turns(_payload(override=100), 1) == 60


def test_explicit_commit_cap_overrides_mcp_environment_default():
    server_env = {"MAX_COMMITS_PER_RUN": "5"}

    pipeline_mcp._apply_github_mcp_request_limits(
        server_env,
        {"iteration_controls": {"max_commits_per_run": 20}},
    )

    assert server_env["MAX_COMMITS_PER_RUN"] == "20"


def test_omitted_commit_cap_preserves_mcp_environment_default():
    inherited = {"MAX_COMMITS_PER_RUN": "9"}
    absent = {}

    pipeline_mcp._apply_github_mcp_request_limits(
        inherited,
        {"iteration_controls": {}},
    )
    pipeline_mcp._apply_github_mcp_request_limits(absent, {})

    assert inherited["MAX_COMMITS_PER_RUN"] == "9"
    assert "MAX_COMMITS_PER_RUN" not in absent


def test_reused_target_noop_requires_a_real_repair_pass():
    assert not pipeline_mcp._reused_target_noop_can_repair(
        {"command": "other", "iteration_controls": {"allow_iteration": False, "max_iterations": 2}}
    )
    assert not pipeline_mcp._reused_target_noop_can_repair(
        {"command": "other", "iteration_controls": {"allow_iteration": True, "max_iterations": 1}}
    )
    assert pipeline_mcp._reused_target_noop_can_repair(
        {"command": "other", "iteration_controls": {"allow_iteration": True, "max_iterations": 2}}
    )


def test_reused_target_noop_honours_the_operator_iteration_kill_switch(monkeypatch):
    # RAE_ALLOW_ITERATION=false pins the sandbox loop to a single pass, so a
    # backtest ticket asking for three iterations still gets no repair pass.
    monkeypatch.setenv("RAE_ALLOW_ITERATION", "false")
    assert not pipeline_mcp._reused_target_noop_can_repair(
        {"command": "backtest", "iteration_controls": {"max_iterations": 3}}
    )


def test_reused_target_noop_uses_the_same_max_iteration_fallbacks(monkeypatch):
    monkeypatch.delenv("RAE_MAX_ITERATIONS", raising=False)
    # No max_iterations anywhere falls back to the shared default of 3, not to 1.
    assert pipeline_mcp._reused_target_noop_can_repair(
        {"command": "other", "iteration_controls": {"allow_iteration": True}}
    )
    # execution_objectives is consulted before the env var, as in the sandbox.
    assert not pipeline_mcp._reused_target_noop_can_repair(
        {
            "command": "other",
            "iteration_controls": {"allow_iteration": True},
            "execution_objectives": {"max_iterations": 1},
        }
    )
    monkeypatch.setenv("RAE_MAX_ITERATIONS", "1")
    assert not pipeline_mcp._reused_target_noop_can_repair(
        {"command": "other", "iteration_controls": {"allow_iteration": True}}
    )
