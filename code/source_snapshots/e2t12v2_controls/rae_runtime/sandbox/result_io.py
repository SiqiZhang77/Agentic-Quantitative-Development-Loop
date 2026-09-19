"""RAE-only. Single emit path: validates against the contract, writes atomically to
the mounted FS (tmp->rename), and emits the same bytes as the last line of stdout."""

import json
import os
import sys
from pathlib import Path
from contract import response_errors
from result_builder import minimal_failed_response, FALLBACK_TICKET

XCOM_BUDGET_BYTES = 4096


def emit_result(resp: dict, result_path: str) -> None:
    errs = response_errors(resp)
    if errs:  # RAE built something its own gate rejects -> emit a minimal valid FAILED
        print(f"warn: response failed contract validation: {errs[:3]}", file=sys.stderr)
        es = resp.get("execution_summary", {}) or {}
        resp = minimal_failed_response(
            resp.get("run_id", "unknown"),
            es.get("ticket_id", FALLBACK_TICKET),
            f"internal: response failed schema validation: {errs[:3]}",
        )

    line = json.dumps(resp, separators=(",", ":"))
    if len(line) > XCOM_BUDGET_BYTES:  # error_message is the only soft field; trim it
        msg = (resp.get("diagnostics") or {}).get("error_message") or ""
        over = len(line) - XCOM_BUDGET_BYTES
        resp["diagnostics"]["error_message"] = (
            (msg[: max(0, len(msg) - over - 1)] + "…") if msg else None
        )
        line = json.dumps(resp, separators=(",", ":"))

    target = Path(result_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp.json")  # same dir -> os.replace is atomic
    tmp.write_text(line)
    os.replace(tmp, target)
    sys.stdout.write(line + "\n")  # last line = XCom payload
    sys.stdout.flush()
