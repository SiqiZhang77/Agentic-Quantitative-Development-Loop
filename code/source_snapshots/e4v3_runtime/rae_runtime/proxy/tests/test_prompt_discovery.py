from prompt_context import build_mcp_code_prompt


def _payload(**strategy):
    return {
        "issue_key": "SCRUM-90",
        "command": "other",
        "execution_objectives": {"strategy_type": "other"},
        "strategy": {"repo_full_name": "org/repo", **strategy},
        "jira_context": {"ticket_id": "SCRUM-90", "summary": "do the thing"},
    }


def test_mcp_prompt_discovery_mode_injects_file_map():
    prompt = build_mcp_code_prompt(
        _payload(source_path="", target_path=""),
        read_ref="develop",
        branch_name="quant/SCRUM-90",
        file_map=(
            "Repository org/a (branch develop):\n"
            "- org/a:src/ga.py (1.2 KB)\n\n"
            "Repository org/b (branch main):\n"
            "- org/b:handlers/loader.py (800 B)"
        ),
    )
    assert "No file was named" in prompt
    assert "REPOSITORY MAP" in prompt
    assert "org/a:src/ga.py (1.2 KB)" in prompt
    assert "org/b:handlers/loader.py" in prompt
    # the agent is told entries are repository-qualified, so it routes tool calls
    assert "repository:path" in prompt
    # discovery mode does not fabricate a required target path
    assert "commit every file you changed" in prompt
    assert 'Read "' not in prompt


def test_mcp_prompt_anchored_mode_reads_named_file():
    prompt = build_mcp_code_prompt(
        _payload(source_path="src/x.py", target_path="src/x.py"),
        read_ref="develop",
        branch_name="quant/SCRUM-90",
    )
    assert 'Read\n   "src/x.py"' in prompt or 'Read "src/x.py"' in prompt
    assert "No file was named" not in prompt
    assert "REPOSITORY MAP" not in prompt
