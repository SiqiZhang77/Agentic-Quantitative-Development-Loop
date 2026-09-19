from __future__ import annotations

import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "build_terra_agreement.py"
SPEC = importlib.util.spec_from_file_location("build_terra_agreement", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_v2_target_namespace_is_separate_from_v1() -> None:
    assert MODULE.EXPERIMENT_ID == "E3-manager-star-v1"
    assert MODULE.EXECUTION_EPOCH == "E3V2"
    assert MODULE._target_ref("T2", 3, "M1") == "quant/E3V2-T2-R3-M1"
    assert MODULE._run_id("T2", 3, "M1") == "E3-T2-R3-M1"


def test_negative_ref_inventory_separates_prior_e3_targets_from_e2() -> None:
    remote_heads = {
        "main": "a" * 40,
        "V5-reference": "b" * 40,
        "experiment-2-data": "c" * 40,
        "quant/E2-T1-C0": "d" * 40,
        "quant/E3-T1-R2-M0": "e" * 40,
        "quant/E3V1-T1-R2-M1": "f" * 40,
    }

    v5_refs, e2_refs, prior_e3_targets = MODULE._known_negative_refs(remote_heads)

    assert v5_refs == ["V5-reference"]
    assert e2_refs == ["experiment-2-data", "quant/E2-T1-C0"]
    assert prior_e3_targets == [
        "quant/E3-T1-R2-M0",
        "quant/E3V1-T1-R2-M1",
    ]
