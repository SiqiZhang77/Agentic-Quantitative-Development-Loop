from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "tools" / "build_run_package.py"
SPEC = importlib.util.spec_from_file_location("build_run_package", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


class BuildRunPackageTests(unittest.TestCase):
    def test_builds_18_runs_and_only_rag_differs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "package"
            manifest = MODULE.build_package(
                source_branch="exp2/si-formal-v2-source",
                source_commit="a" * 40,
                salt=b"a-private-blinding-salt-with-at-least-32-bytes",
                output=output,
            )
            self.assertEqual(manifest["run_count"], 18)
            self.assertEqual(
                [run["run_key"] for run in manifest["runs"]],
                MODULE.execution_schedule(),
            )
            self.assertEqual(
                manifest["task_records"]["T3"]["input_sha256"],
                {
                    "task_inputs/financial_returns_v1.csv": (
                        "1b0830f2077cb2a70cd094c6e879702d8dcea0fa4e07152b533e8c9cb005df61"
                    )
                },
            )
            run_manifests = list(output.glob("runs/*/R*/*/run_manifest.json"))
            self.assertEqual(len(run_manifests), 18)
            for task_id in ("T1", "T2", "T3"):
                for replicate in (1, 2, 3):
                    base = output / "runs" / task_id / f"R{replicate}"
                    c0 = (base / "C0" / "command.txt").read_text(encoding="utf-8")
                    c1 = (base / "C1" / "command.txt").read_text(encoding="utf-8")
                    c0_lines = c0.splitlines()
                    c1_lines = c1.splitlines()
                    self.assertEqual(len(c0_lines), len(c1_lines))
                    self.assertEqual(
                        [
                            (left, right)
                            for left, right in zip(c0_lines, c1_lines)
                            if left != right
                        ],
                        [("rag_enabled: false", "rag_enabled: true")],
                    )
                    self.assertEqual(c0_lines.count("rag_enabled: false"), 1)
                    self.assertEqual(c1_lines.count("rag_enabled: true"), 1)

            private_map = json.loads(
                (output / "blinding" / "private_condition_map.json").read_text()
            )
            self.assertEqual(len(private_map["submissions"]), 18)
            self.assertEqual(len(set(private_map["submissions"])), 18)
            self.assertTrue((output / "checksums.sha256").is_file())

    def test_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "package"
            output.mkdir()
            with self.assertRaises(FileExistsError):
                MODULE.build_package(
                    source_branch="exp2/si-formal-v2-source",
                    source_commit="b" * 40,
                    salt=b"another-private-blinding-salt-at-least-32-bytes",
                    output=output,
                )

    def test_rejects_short_salt_and_non_exp2_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                MODULE.build_package(
                    source_branch="feature/not-exp2",
                    source_commit="c" * 40,
                    salt=b"x" * 64,
                    output=Path(tmp) / "one",
                )
            with self.assertRaises(ValueError):
                MODULE.build_package(
                    source_branch="exp2/si-formal-v2-source",
                    source_commit="c" * 40,
                    salt=b"too short",
                    output=Path(tmp) / "two",
                )

    def test_schedule_is_hash_derived_and_pair_members_stay_adjacent(self) -> None:
        schedule = MODULE.execution_schedule()
        self.assertEqual(len(schedule), 18)
        for first, second in zip(schedule[::2], schedule[1::2]):
            first_prefix, first_condition = first.rsplit("-", 1)
            second_prefix, second_condition = second.rsplit("-", 1)
            self.assertEqual(first_prefix, second_prefix)
            self.assertEqual({first_condition, second_condition}, {"C0", "C1"})


if __name__ == "__main__":
    unittest.main()
