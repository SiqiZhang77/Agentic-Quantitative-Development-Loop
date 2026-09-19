from pathlib import Path


def test_t3_task_requires_item_first_and_reference_based_artifact_workflows() -> None:
    repository_root = Path(__file__).resolve().parents[3]
    task = (
        repository_root
        / "experiments/shared/t3-quant-suite-v1/TASK.md"
    ).read_text(encoding="utf-8")

    assert "Do not retype long arrays" in task
    assert "Do not use an ordinary `ref` for a prior call" in task
    assert '"stored_ref"' in task
    assert "call `submit_t3_items`" in task
    assert "item store is the primary mathematical submission" in task
    assert "checks every item independently" in task
    assert "does not say whether the mathematics is correct" in task
    assert "use `submit_calculation_checkpoint` and `commit_calculation_artifact`" in task
    assert "secondary record" in task
    assert "`experiments/shared/t3-quant-suite-v1/output_schema_v1.json` as `schema_path`" in task
    assert "Use `$calc` references" in task
    assert "`$zip_rows`" in task
    assert "The model must choose every calculation" in task
    assert "perform no new arithmetic" in task
    assert "Failure to complete that secondary record does not erase item candidates" in task
