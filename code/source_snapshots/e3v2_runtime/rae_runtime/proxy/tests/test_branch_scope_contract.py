from __future__ import annotations

import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "github_mcp_server.py"
PIPELINE = ROOT / "pipeline_mcp.py"


def parsed(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def function_node(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found")


def called_names(node: ast.AST) -> set[str]:
    return {
        call.func.id
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    }


class BranchScopeContractTests(unittest.TestCase):
    def test_runtime_patch_sources_compile(self) -> None:
        for path in (SERVER, PIPELINE):
            compile(path.read_text(encoding="utf-8"), str(path), "exec")

    def test_pipeline_always_enables_fail_closed_for_model_server(self) -> None:
        tree = parsed(PIPELINE)
        matching_values = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Subscript):
                continue
            if not isinstance(target.value, ast.Name) or target.value.id != "server_env":
                continue
            key = target.slice
            if isinstance(key, ast.Constant) and key.value == "ENFORCE_READ_BRANCH_SCOPE":
                matching_values.append(node.value)
        self.assertEqual(len(matching_values), 1)
        self.assertIsInstance(matching_values[0], ast.Constant)
        self.assertEqual(matching_values[0].value, "true")

    def test_every_model_read_tool_has_read_ref_gate(self) -> None:
        tree = parsed(SERVER)
        for name in ("read_file", "find_in_file", "read_files", "list_files"):
            with self.subTest(tool=name):
                self.assertIn("_check_read_scope", called_names(function_node(tree, name)))
        self.assertIn(
            "_check_base_branch_scope",
            called_names(function_node(tree, "create_or_reuse_branch")),
        )

    def test_read_and_base_helpers_fail_closed(self) -> None:
        tree = parsed(SERVER)
        namespace = {
            "_SOURCE_BRANCH_MAP": {},
            "_TARGET_BRANCH_MAP": {},
            "_ENFORCE_READ_BRANCH_SCOPE": True,
        }
        selected = ast.Module(
            body=[
                function_node(tree, "_check_read_scope"),
                function_node(tree, "_check_base_branch_scope"),
            ],
            type_ignores=[],
        )
        exec(compile(ast.fix_missing_locations(selected), str(SERVER), "exec"), namespace)
        self.assertIn("no configured source/target refs", namespace["_check_read_scope"]("repo", "main"))
        self.assertIn("no configured source branch", namespace["_check_base_branch_scope"]("repo", "main"))
        namespace["_SOURCE_BRANCH_MAP"] = {"repo": "frozen-source"}
        namespace["_TARGET_BRANCH_MAP"] = {"repo": "fresh-target"}
        self.assertIsNone(namespace["_check_read_scope"]("repo", "frozen-source"))
        self.assertIsNone(namespace["_check_read_scope"]("repo", "fresh-target"))
        self.assertIsNotNone(namespace["_check_read_scope"]("repo", "prior-result"))
        self.assertIsNone(namespace["_check_base_branch_scope"]("repo", "frozen-source"))
        self.assertIsNotNone(namespace["_check_base_branch_scope"]("repo", "main"))


if __name__ == "__main__":
    unittest.main()
