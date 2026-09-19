import warnings

import pytest

from artifact_validation import (
    content_quality_issues,
    inspect_committed_artifacts,
    proposed_artifact_issues,
)


@pytest.mark.parametrize(
    "content",
    [
        "",
        "   ",
        "The above draft content",
        "placeholder",
        "TBD",
        "TODO",
        "Detailed content for the comprehensive README will be inserted here.",
        # Back-references to content stated elsewhere, seen on real runs.
        "Full implementation as described above",
        "python code from above",
        "code from above",
        "See above.",
        "As shown previously",
        # Unsubstituted template tokens.
        "<<file_content>>",
        "{{content}}",
    ],
)
def test_empty_and_placeholder_only_artifacts_are_rejected(content):
    assert content_quality_issues("README.md", content)


@pytest.mark.parametrize(
    "content",
    [
        # "see above" inside a real file must not trigger the whole-file rule.
        "# Helper\n\n\ndef parse(value):\n    \"\"\"See above for the grammar.\"\"\"\n    return value\n",
        "from above import helper\n\n\ndef run():\n    return helper()\n",
    ],
)
def test_placeholder_wording_inside_a_real_file_is_accepted(content):
    assert content_quality_issues("pkg/helper.py", content) == []


def test_trivial_readme_is_rejected():
    issues = content_quality_issues("README.md", "# Project\n\nSmall description.")

    assert any("too short" in issue for issue in issues)


def test_substantive_structured_readme_is_accepted():
    content = "# Project\n\n" + ("Verified repository documentation. " * 12)

    assert content_quality_issues("README.md", content) == []


# Contents observed on real runs that reached a committed .py file. These read
# as plausible source, so only parsing rejects them -- no phrase list would.
@pytest.mark.parametrize(
    "content",
    [
        (
            "import pandas as pd\n"
            "\n"
            "def test_cs_rank():\n"
            "    data = pd.DataFrame({'sector: ['A', 'A', 'date: ['2026-01-01',\n"
        ),
        "def cs_rank(frame):\n    return frame[\n",
    ],
)
def test_unparseable_python_artifacts_are_rejected(content):
    issues = content_quality_issues("pkg/operators.py", content)

    assert any("is not valid Python" in issue for issue in issues)


def test_leaked_tool_call_fragment_is_rejected():
    # Observed on SCRUM-108: the model passed part of its own call as content.
    content = (
        "def select_individuals(population):\n"
        "    pass\n"
        "```], commit_message=\n"
    )

    issues = content_quality_issues("pkg/multi_horizon_nsga2.py", content)

    assert any("fragment of a tool call" in issue for issue in issues)


def test_markdown_fenced_source_file_is_rejected():
    content = "```python\ndef cs_rank(frame):\n    return frame\n```\n"

    issues = content_quality_issues("pkg/operators.py", content)

    assert any("markdown code fence" in issue for issue in issues)


def test_markdown_file_may_legitimately_start_with_a_fence():
    content = "```python\nprint('example')\n```\n\n" + ("Documented usage. " * 12)

    assert content_quality_issues("docs/usage.md", content) == []


@pytest.mark.parametrize(
    "content",
    [
        # Keyword arguments at the end of a real file must not look like a leak.
        "def run():\n    return helper(path='x', branch='main')\n",
        "CONFIG = dict(\n    values=[1, 2],\n    name='x',\n)\n",
    ],
)
def test_legitimate_keyword_arguments_are_not_treated_as_leaked_calls(content):
    assert content_quality_issues("pkg/helper.py", content) == []


def test_syntax_check_ignores_warnings_so_the_verdict_is_deterministic():
    # A non-raw "\d" emits SyntaxWarning; under -W error that would otherwise
    # reject a file that imports perfectly well.
    content = "import re\n\n\nPATTERN = re.compile('\\d+')\n"

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert content_quality_issues("pkg/patterns.py", content) == []


def test_pathological_nesting_is_reported_not_raised():
    issues = content_quality_issues("pkg/deep.py", "x = " + "(" * 5000 + ")" * 5000)

    assert any("is not valid Python" in issue for issue in issues)


def test_valid_python_artifact_is_accepted():
    content = "import statistics\n\n\ndef mean(values):\n    return statistics.mean(values)\n"

    assert content_quality_issues("pkg/operators.py", content) == []


def test_unparseable_json_artifact_is_rejected_with_location():
    issues = content_quality_issues("submissions/result.json", '{"rows": [1, 2}')

    assert any("is not valid JSON" in issue for issue in issues)
    assert any("line 1, column" in issue for issue in issues)


def test_complete_json_artifact_is_accepted():
    assert content_quality_issues("submissions/result.json", '{"rows":[1,2]}') == []


def test_python_syntax_check_does_not_apply_to_other_extensions():
    # Prose is a legitimate .md artifact; only .py is parsed.
    assert content_quality_issues("docs/design.md", "Full implementation below.") == []


def test_unparseable_python_is_rejected_before_the_commit_is_attempted():
    issues = proposed_artifact_issues(
        path="pkg/operators.py",
        content="def cs_rank(frame):\n    return frame[\n",
        source_branch="main",
        target_branch="quant/SCRUM-1",
        repo_name="bankingscience/Repo",
        reader=lambda **_: "",
    )

    assert any("is not valid Python" in issue for issue in issues)


def test_proposed_readme_is_checked_before_commit_against_repository_evidence():
    readme = """# ATP Data Handlers

Run `mvn clean install` from the repository root. See `LICENSE`.

""" + ("Verified project documentation. " * 10)

    def reader(*, branch, path, repo_name):
        raise RuntimeError("404 not found")

    issues = proposed_artifact_issues(
        path="README.md",
        content=readme,
        source_branch="develop",
        target_branch="quant/SCRUM-115",
        repo_name="bankingscience/ATPDataHandlersRepo",
        reader=reader,
    )

    assert any("LICENSE" in issue for issue in issues)
    assert any("mvn clean install" in issue for issue in issues)


def test_committed_artifacts_are_read_from_target_repo_and_classified_from_source():
    reads = []

    def reader(*, branch, path, repo_name):
        reads.append((repo_name, branch, path))
        if branch == "develop" and path == "README.md":
            raise RuntimeError("404 not found")
        return "# Project\n\n" + ("Verified repository documentation. " * 12)

    modified, new, issues = inspect_committed_artifacts(
        committed_paths=["README.md"],
        source_branch="develop",
        target_branch="quant/SCRUM-115",
        repo_name="bankingscience/ATPDataHandlersRepo",
        reader=reader,
        required_target_path="README.md",
    )

    assert modified == []
    assert new == ["README.md"]
    assert issues == []
    assert reads == [
        ("bankingscience/ATPDataHandlersRepo", "quant/SCRUM-115", "README.md"),
        ("bankingscience/ATPDataHandlersRepo", "develop", "README.md"),
    ]


def test_missing_required_target_is_rejected():
    modified, new, issues = inspect_committed_artifacts(
        committed_paths=["docs/notes.md"],
        source_branch="main",
        target_branch="quant/SCRUM-1",
        repo_name="bankingscience/BSLAgenticQuantDevLoop",
        reader=lambda **kwargs: "Useful notes",
        required_target_path="README.md",
    )

    assert modified == ["docs/notes.md"]
    assert new == []
    assert any("README.md" in issue for issue in issues)


def test_unreadable_committed_artifact_is_rejected():
    def reader(**kwargs):
        raise RuntimeError("404 not found")

    _, _, issues = inspect_committed_artifacts(
        committed_paths=["README.md"],
        source_branch="develop",
        target_branch="quant/SCRUM-115",
        repo_name="bankingscience/ATPDataHandlersRepo",
        reader=reader,
        required_target_path="README.md",
    )

    assert any("could not be read" in issue for issue in issues)


def test_readme_rejects_nonexistent_paths_and_wrong_maven_working_directory():
    readme = """# ATP Data Handlers

Run `mvn clean install` from the repository root. See the `LICENSE` file.

""" + ("Verified project documentation. " * 10)

    def reader(*, branch, path, repo_name):
        if branch == "quant/SCRUM-115" and path == "README.md":
            return readme
        if path == "atp-handlers/pom.xml":
            return "<project />"
        raise RuntimeError("404 not found")

    _, new, issues = inspect_committed_artifacts(
        committed_paths=["README.md"],
        source_branch="develop",
        target_branch="quant/SCRUM-115",
        repo_name="bankingscience/ATPDataHandlersRepo",
        reader=reader,
        required_target_path="README.md",
    )

    assert new == ["README.md"]
    assert any("LICENSE" in issue and "does not exist" in issue for issue in issues)
    assert any("mvn clean install" in issue and "pom.xml" in issue for issue in issues)


def test_readme_accepts_verified_module_qualified_maven_command():
    readme = """# ATP Data Handlers

Build with `mvn -f atp-handlers/pom.xml clean install` from the repository root.

""" + ("Verified project documentation. " * 10)

    def reader(*, branch, path, repo_name):
        if path == "README.md" and branch == "quant/SCRUM-115":
            return readme
        if path == "atp-handlers/pom.xml":
            return "<project />"
        raise RuntimeError("404 not found")

    _, new, issues = inspect_committed_artifacts(
        committed_paths=["README.md"],
        source_branch="develop",
        target_branch="quant/SCRUM-115",
        repo_name="bankingscience/ATPDataHandlersRepo",
        reader=reader,
        required_target_path="README.md",
    )

    assert new == ["README.md"]
    assert issues == []
