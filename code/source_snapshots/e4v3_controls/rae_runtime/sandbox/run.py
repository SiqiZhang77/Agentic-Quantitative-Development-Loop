import json
import hashlib
import math
import os
import sys
import copy
from collections import Counter
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
import time

import jsonschema

import logging

logging.basicConfig(
    stream=sys.stderr, level=logging.INFO, force=True
)  # stdout stays the data bus

# sandbox/ and proxy/ on the path (run.py is /app/run.py in the image)
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "proxy"))

from mcp_client import run_backtest_via_mcp
from pipeline import run_pipeline, run_pipeline_auto
from result_builder import build_response, minimal_failed_response, FALLBACK_TICKET
from result_io import emit_result
from trace import TraceCollector, now_iso
from budget_guard import RunTimeout, BudgetExceeded
from progress_emitter import emit_progress_event, get_progress_events_path


DEFAULT_OUTPUT = "/workspace/output/result.json"
DEFAULT_STRATEGY_PATH = "rae_runtime/proxy/strategy.py"
# Backtest tickets tune the engine .request (the atrade engine can't run Python);
# general tickets keep editing arbitrary code. STRATEGY_PATH overrides both.
DEFAULT_BACKTEST_STRATEGY_PATH = "rae_runtime/proxy/strategy.request"
DEFAULT_REPO_URL = "https://github.com/bankingscience/BSLAgenticQuantDevLoop"
DEFAULT_REPO_FULL_NAME = "bankingscience/BSLAgenticQuantDevLoop"
DEFAULT_SOURCE_REF = "main"
LOG_REF = "/workspace/output/artifacts/run.log"
INPUT_DATASET_ROOT = PurePosixPath("/workspace/input/datasets")

# RAE's interim flat request schema, consolidated next to the vendored IW contracts.
# NOTE: this is the PoC shape; IW's orchestrator emits the richer
# runtime_request.schema.json shape — see the follow-up ticket before prod e2e.
_REQUEST_SCHEMA_PATH = Path(__file__).parent / "schemas" / "runtime_request.schema.json"

os.environ.setdefault("MOCK_RUNTIME_DIR", "/tmp/mock_runtime")
os.environ.setdefault("BACKTEST_REQUESTS_DIR", "/tmp/mock_runtime/backtest-requests")


# ---------- input: stdin JSON request (RAE-02) ----------


def _validate_payload(payload: dict) -> None:
    """Validate against the request schema. Raises jsonschema.ValidationError on failure."""
    jsonschema.validate(payload, json.loads(_REQUEST_SCHEMA_PATH.read_text()))


def _default_strategy_path(command: str, resource_path: str | None = None) -> str:
    """The repo file this ticket edits.

    STRATEGY_PATH (operator pin) still wins over everything. Otherwise the request's
    own ``execution_objectives.resource_path`` names the file, which is the only way a
    general ticket can target real code — without it every non-backtest run falls back
    to the demo strategy. The type-based defaults remain for requests that name no
    file: a backtest ticket tunes the engine .request, anything else edits code.
    The schema constrains resource_path to a repo-relative path (no leading '/', no '..').
    """
    override = os.getenv("STRATEGY_PATH")
    if override:
        return override
    if resource_path and str(resource_path).strip():
        return str(resource_path).strip()
    if str(command or "").strip().lower() == "backtest":
        return DEFAULT_BACKTEST_STRATEGY_PATH
    # No file named and not a backtest: the run discovers its own target from the
    # repository (see resource_discovery), so leave the source path empty rather
    # than assuming the demo strategy — that default only ever existed in the
    # default repo and silently misfired everywhere else.
    return ""


def _normalise_request(p: dict) -> dict:
    """Map IW's validated runtime_request shape -> the flat internal dict the
    pipeline/mcp_client consume. Pure: returns a new dict, never mutates input."""
    jm = p["jira_metadata"]
    ticket_id = jm["ticket_id"]
    objectives = p["execution_objectives"]
    params = objectives.get("parsed_task_parameters") or {}
    retrieval_context = p.get("retrieval_context")
    rag_enabled = params.get("rag_enabled") is True
    if rag_enabled and not isinstance(retrieval_context, dict):
        raise ValueError(
            "rag_enabled=true requires a verified retrieval_context from the gateway"
        )
    if not rag_enabled and retrieval_context is not None:
        raise ValueError("retrieval_context is forbidden unless rag_enabled=true")
    if rag_enabled:
        _validate_final_retrieval_context(
            retrieval_context,
            requested_top_k=params.get("rag_top_k"),
        )
    dr = objectives.get("target_date_range") or {}
    repos = p.get("repository_details") or []
    if not repos:
        repos = [
            {
                "alias": "BSLAgenticQuantDevLoop",
                "repo_full_name": DEFAULT_REPO_FULL_NAME,
                "clone_url": DEFAULT_REPO_URL,
                "source_branch": DEFAULT_SOURCE_REF,
                "target_branch": f"quant/{ticket_id}",
                "runtime_role": "default",
                "allowed_directories": ["."],
            }
        ]
    repo = repos[0]
    source_path = _default_strategy_path(
        objectives["strategy_type"], objectives.get("resource_path")
    )
    requested_target_path = objectives.get("target_path")
    explicit_target_path = (
        requested_target_path
        if requested_target_path != objectives.get("resource_path")
        else None
    )
    target_path = requested_target_path or source_path
    # The backtest window comes solely from target_date_range: drop any window keys
    # smuggled through parsed_task_parameters so they can't re-introduce a default.
    # When the user omits dates, leave start/end unset so the strategy .request's own
    # (long) window stands; a short user window still fails submit_backtest's guard.
    _WINDOW_KEYS = {"start", "end", "start_date", "end_date"}
    args = {
        "strategy": params.get("strategy") or jm["summary"],
        **{k: v for k, v in params.items() if k not in _WINDOW_KEYS},
    }
    if dr.get("start_date"):
        args["start"] = dr["start_date"]
    if dr.get("end_date"):
        args["end"] = dr["end_date"]

    input_datasets = _verify_input_datasets(p.get("input_datasets") or [])

    return {
        "run_id": p["run_id"],
        # Omission is the byte-compatible M0 path.  Preserve explicit experiment
        # metadata only when the caller supplied it; do not inject a default key
        # into legacy requests or the payload consumed by existing prompt code.
        **(
            {"architecture_mode": p["architecture_mode"]}
            if "architecture_mode" in p
            else {}
        ),
        "issue_key": ticket_id,
        "repositories": repos,
        "command": objectives["strategy_type"],
        "execution_objectives": objectives,
        "input_paths": p.get("input_paths") or {},
        "jira_context": jm,
        "retrieval_context": (
            json.loads(json.dumps(retrieval_context))
            if retrieval_context is not None
            else None
        ),
        "code_free": False,
        "args": args,
        "strategy": {
            "repo_url": repo.get("clone_url", DEFAULT_REPO_URL),
            "repo_full_name": repo.get("repo_full_name", DEFAULT_REPO_FULL_NAME),
            "ref": repo.get("source_branch") or DEFAULT_SOURCE_REF,
            "target_branch": repo.get("target_branch") or f"quant/{ticket_id}",
            # path remains the compatibility source path for old pipeline callers.
            "path": source_path,
            "source_path": source_path,
            "target_path": target_path,
            "target_path_explicit": explicit_target_path is not None,
            # Empty means unrestricted; the MCP server enforces this list.
            "allowed_directories": repo.get("allowed_directories") or [],
        },
        "output_paths": p["output_paths"],  # keep, so _result_path & progress read it
        "iteration_controls": p["iteration_controls"],  # for budget_guard
        "input_datasets": input_datasets,
        "result_path": p["output_paths"]["result_path"],
    }


def _validate_final_retrieval_context(
    context: dict,
    *,
    requested_top_k: object,
) -> None:
    """Enforce final C1 invariants that the shared pre-retrieval schema cannot.

    The gateway uses the same request schema before and after retrieval, so
    treatment/context relationships and equality between two fields are checked
    at the sandbox boundary immediately before any agent work starts.
    """

    if context.get("enabled") is not True:
        raise ValueError("retrieval_context.enabled must be true for rag_enabled=true")
    context_top_k = context.get("requested_top_k")
    if (
        isinstance(requested_top_k, bool)
        or not isinstance(requested_top_k, int)
        or not 1 <= requested_top_k <= 10
    ):
        raise ValueError("rag_top_k must be an integer between 1 and 10")
    if context_top_k != requested_top_k:
        raise ValueError("retrieval_context.requested_top_k must match rag_top_k")
    source_ticket_cap = context.get("max_memories_per_source_ticket")
    if source_ticket_cap != 2:
        raise ValueError(
            "retrieval_context.max_memories_per_source_ticket must be 2"
        )

    memories = context.get("memories")
    if not isinstance(memories, list):
        raise ValueError("retrieval_context.memories must be an array")
    if len(memories) > requested_top_k:
        raise ValueError("retrieval_context.memories exceeds requested_top_k")
    status = context.get("status")
    if status == "ok" and not memories:
        raise ValueError("retrieval_context.status=ok requires at least one memory")
    if status == "empty" and memories:
        raise ValueError("retrieval_context.status=empty requires no memories")
    if status not in {"ok", "empty"}:
        raise ValueError("retrieval_context.status must be ok or empty")

    expected_ranks = list(range(1, len(memories) + 1))
    actual_ranks = [item.get("rank") if isinstance(item, dict) else None for item in memories]
    if actual_ranks != expected_ranks:
        raise ValueError("retrieval_context memory ranks must be consecutive from 1")
    memory_ids = [
        item.get("memory_id") if isinstance(item, dict) else None for item in memories
    ]
    if any(not isinstance(item, str) or not item for item in memory_ids):
        raise ValueError("retrieval_context memory IDs must be non-empty strings")
    if len(memory_ids) != len(set(memory_ids)):
        raise ValueError("retrieval_context memory IDs must be unique")
    source_ticket_ids = [
        item.get("source_ticket_id") if isinstance(item, dict) else None
        for item in memories
    ]
    if any(not isinstance(item, str) or not item for item in source_ticket_ids):
        raise ValueError(
            "retrieval_context source ticket IDs must be non-empty strings"
        )
    source_ticket_counts = Counter(source_ticket_ids)
    if any(count > source_ticket_cap for count in source_ticket_counts.values()):
        raise ValueError(
            "retrieval_context exceeds max_memories_per_source_ticket"
        )
    for item in memories:
        score = item.get("score")
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
            or score < 0
        ):
            raise ValueError("retrieval_context memory scores must be finite and non-negative")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_input_datasets(items: list[dict]) -> list[dict]:
    """Verify the files declared by IW before any agent or analysis runs."""

    verified = []
    for raw in items:
        item = dict(raw)
        if item.get("source_kind") not in {"hdfs", "jira_attachment"}:
            raise ValueError("unsupported input dataset source_kind")
        container_path = str(item.get("container_path") or "")
        posix_path = PurePosixPath(container_path)
        try:
            relative = posix_path.relative_to(INPUT_DATASET_ROOT)
        except ValueError as exc:
            raise ValueError(
                "input dataset path must be below /workspace/input/datasets"
            ) from exc
        if not relative.parts or ".." in posix_path.parts:
            raise ValueError(f"unsafe input dataset path: {container_path}")

        file_path = Path(container_path)
        if not file_path.is_file():
            raise FileNotFoundError(
                f"declared input dataset is missing: {container_path}"
            )
        actual_size = file_path.stat().st_size
        expected_size = item.get("size_bytes")
        if expected_size is not None and actual_size != int(expected_size):
            raise ValueError(
                f"input dataset size mismatch for {container_path}: "
                f"expected {expected_size}, got {actual_size}"
            )
        actual_sha = _sha256_file(file_path)
        expected_sha = item.get("sha256")
        if expected_sha and actual_sha != expected_sha:
            raise ValueError(
                f"input dataset checksum mismatch for {container_path}"
            )
        item["size_bytes"] = actual_size
        item["sha256"] = actual_sha
        verified.append(item)
    return verified


def _load_payload_from_stdin() -> dict:
    raw = sys.stdin.read()
    if not raw.strip():
        raise ValueError("stdin was empty — expected a JSON request payload")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"stdin payload is not valid JSON: {exc}") from exc
    # Capture correlation identifiers before full contract validation. A schema
    # mismatch elsewhere in an otherwise parseable gateway request must not erase
    # the real ticket/run identity from the failure response.
    run_id = payload.get("run_id") if isinstance(payload, dict) else None
    metadata = payload.get("jira_metadata") if isinstance(payload, dict) else None
    ticket_id = metadata.get("ticket_id") if isinstance(metadata, dict) else None
    try:
        _validate_payload(payload)  # validates the IW shape
        return _normalise_request(payload)  # returns the flat internal shape
    except Exception as exc:
        exc.runtime_run_id = run_id if isinstance(run_id, str) and run_id else "unknown"
        exc.runtime_ticket_id = ticket_id
        raise


def _load_payload_legacy() -> dict:
    """Env-var input path. Enabled by RAE_LEGACY_INPUT=1 so we can roll back without redeploy."""
    default_input = os.getenv("RAE_REQUEST_FILE", "/workspace/input/request.json")
    ticket = os.getenv("TICKET")
    request_text = os.getenv("REQUEST")
    if ticket or request_text:
        issue_key = ticket or os.getenv("ISSUE_KEY", "POC")
        return {
            "run_id": os.getenv("RUN_ID", issue_key),
            "issue_key": issue_key,
            "command": "backtest",
            "args": {"strategy": request_text or "improve the strategy"},
            "strategy": {
                "repo_url": DEFAULT_REPO_URL,
                "ref": DEFAULT_SOURCE_REF,
                "path": _default_strategy_path("backtest"),
            },
            "result_path": os.getenv("RESULT_PATH", DEFAULT_OUTPUT),
        }
    raw = os.getenv("REQUEST_JSON") or os.getenv("RAE_REQUEST")
    if raw:
        return json.loads(raw)
    if default_input and Path(default_input).is_file():
        return json.loads(Path(default_input).read_text())
    raise FileNotFoundError(
        f"no payload: set TICKET/REQUEST, RAE_REQUEST (JSON), or a file at {default_input}"
    )


def load_payload() -> dict:
    if os.getenv("RAE_LEGACY_INPUT") == "1":
        return _load_payload_legacy()
    return _load_payload_from_stdin()


# ---------- output: IW response contract ----------


def _ticket_id(payload):
    return (
        (payload.get("jira_metadata") or {}).get("ticket_id")
        or payload.get("issue_key")
        or FALLBACK_TICKET
    )


def _result_path(payload):
    return (
        (payload.get("output_paths") or {}).get("result_path")
        or payload.get("result_path")
        or DEFAULT_OUTPUT
    )


def _classify(e):
    if isinstance(e, RunTimeout):
        return "timeout"
    if isinstance(e, BudgetExceeded):
        return "rate_limit"
    if type(e).__name__ == "AgentTurnLimitExceeded":
        return "agent_turn_limit"
    n = type(e).__name__.lower()
    return "compile" if ("syntax" in n or "compile" in n) else "runtime"


def _needs_backtest(payload) -> bool:
    """Whether this request runs the backtester.

    The request's ``strategy_type`` (mapped to ``command`` in _normalise_request)
    classifies the task: only ``backtest`` runs the engine. Every other type
    (``ingestion`` / ``refactor`` / ``analysis`` / ``other``) is a general code
    task — the agent still branches and commits, but no backtest is run. The env
    override ``RAE_FORCE_BACKTEST`` (``true``/``false``) wins when set, for
    operators who need to pin the behaviour regardless of the request.
    """
    forced = os.getenv("RAE_FORCE_BACKTEST")
    if forced is not None:
        return forced.strip().lower() == "true"
    return str(payload.get("command", "")).strip().lower() == "backtest"


def _select_evaluate_fn(payload):
    """The iteration loop's evaluator: whoever scores this request type.

    Backtest requests are scored by the engine. General requests have no metrics,
    so they are scored by the review agent, which returns the same
    evaluation/recommended_action shape and lets them iterate on the same loop.
    """
    if _needs_backtest(payload):
        return _select_backtest_fn()
    if (
        payload.get("architecture_mode") == "single_agent"
        and _uses_two_attempt_contract(payload)
    ):
        from exp3.attempt_control import evaluate_single_agent_attempt

        return evaluate_single_agent_attempt
    from review_agent import review_general_task  # proxy/ is on sys.path

    return review_general_task


def _select_backtest_fn():
    """Pick the backtest driver for the iteration loop.

    Default: the scripted MCP client (``run_backtest_via_mcp``), which drives
    submit -> status -> postprocessing deterministically and returns the
    structured metrics/evaluation the loop scores against.

    ``USE_AGENT_BACKTEST=true`` instead hands the backtest tools to the LLM and
    lets it drive them (``run_backtest_mcp``). This is orthogonal to
    ``USE_REAL_BACKTESTER`` (mock vs. real engine, default mock): the flag here
    only chooses *who* drives the tools, not *which* engine they hit.
    """
    if os.getenv("USE_AGENT_BACKTEST", "false").strip().lower() == "true":
        from backtest_mcp import run_backtest_mcp  # proxy/ is on sys.path
        return run_backtest_mcp
    return run_backtest_via_mcp




def _zero_code_modifications(payload: dict) -> bool:
    """Whether this request forbids editing code, so no branch or commit is made.

    Declared by ``execution_objectives.zero_code_modifications``. Request
    normalization supplies the current repository when repository_details is
    omitted, so omission alone does not make a run read-only.
    """
    objectives = payload.get("execution_objectives") or {}
    if objectives.get("zero_code_modifications") is True:
        return True
    return payload.get("code_free") is True


def _should_run_direct_backtest(payload: dict) -> bool:
    """Return True for requests that submit the existing strategy.request baseline
    directly, with no strategy rewrite first.

    Normal strategy-improvement tickets still use the iteration loop.
    """
    flag = os.getenv("RAE_DIRECT_BACKTEST", "").strip().lower()
    if flag in {"1", "true", "yes", "y", "on"}:
        return True

    command = str(payload.get("command") or "").strip().lower()
    if command != "backtest":
        return False

    # The declared form of "backtest the baseline without editing it". The marker
    # sniffing below predates it and is kept only so the existing SCRUM-83 canary
    # tickets keep working; prefer zero_code_modifications for new requests.
    if _zero_code_modifications(payload):
        return True

    args = payload.get("args") or {}
    objective = " ".join(
        str(x or "")
        for x in [
            args.get("strategy"),
            args.get("objective"),
            args.get("task"),
            args.get("request"),
            payload.get("summary"),
            payload.get("description"),
        ]
    ).lower()

    smoke_markers = [
        "direct-backtest",
        "direct backtest",
        "hdfs bridge canary",
        "existing minimal real-backtester smoke test",
        "hardened hdfs bridge",
        "submit the backtest through the mcp submit_backtest tool",
        "do not add non-engine keys",
    ]
    return any(marker in objective for marker in smoke_markers)


def _uses_manager_star(payload: dict) -> bool:
    """Resolve an explicit E3 architecture selector without changing M0.

    The overwhelmingly common compatibility path omits ``architecture_mode``;
    keep that path free of Experiment 3 imports and return immediately.  An
    explicit value is resolved by the frozen contract so legacy-file input also
    fails closed for unknown modes instead of silently falling back to M0.
    """

    if "architecture_mode" not in payload:
        return False
    from exp3.architecture import MANAGER_STAR, validate_explicit_e3_request

    return validate_explicit_e3_request(payload) == MANAGER_STAR


def _uses_two_attempt_contract(payload: dict) -> bool:
    controls = payload.get("iteration_controls") or {}
    return (
        controls.get("allow_iteration") is True
        and controls.get("max_iterations") == 2
        and controls.get("max_failed_iterations") == 2
    )


class RetrievalDeliveryEvidenceError(RuntimeError):
    """A RAG-enabled attempt finished without consistent prompt-delivery proof."""


def _attempt_retrieval_delivery(
    payload: dict,
    deliveries: list[object],
) -> dict | None:
    """Return one verified, text-free receipt shared by every completed attempt.

    Two-attempt runs use deep-copied request objects.  The prompt builder writes its
    delivery receipt to those copies, so the outer observation must carry the receipt
    back explicitly instead of looking for it on the untouched original request.
    """

    context = payload.get("retrieval_context")
    if not isinstance(context, Mapping) or context.get("enabled") is not True:
        return payload.get("_retrieval_delivery")
    if not _uses_two_attempt_contract(payload):
        delivery = payload.get("_retrieval_delivery")
        return copy.deepcopy(delivery) if isinstance(delivery, Mapping) else None
    if not deliveries or any(not isinstance(item, Mapping) for item in deliveries):
        raise RetrievalDeliveryEvidenceError(
            "RAG-enabled attempt did not preserve generator prompt-delivery evidence"
        )
    canonical = [
        json.dumps(item, sort_keys=True, separators=(",", ":")) for item in deliveries
    ]
    if len(set(canonical)) != 1:
        raise RetrievalDeliveryEvidenceError(
            "RAG prompt-delivery evidence changed between observation attempts"
        )
    return copy.deepcopy(dict(deliveries[0]))


def main(
    *,
    exp3_boundary_factory=None,
    exp3_production_role_runner=None,
    exp3_audit_directory=None,
    exp3_clock=None,
) -> int:
    start = now_iso()
    tracer = TraceCollector()
    try:
        payload = load_payload()
    except Exception as e:
        run_id = getattr(e, "runtime_run_id", "unknown")
        ticket_id = getattr(e, "runtime_ticket_id", FALLBACK_TICKET)
        emit_result(
            minimal_failed_response(
                run_id,
                ticket_id,
                f"{type(e).__name__}: {e}",
                outcome="bad_payload",
                log_ref=LOG_REF,
            ),
            DEFAULT_OUTPUT,
        )
        return 1

    run_id, ticket_id, result_path = (
        payload.get("run_id", "unknown"),
        _ticket_id(payload),
        _result_path(payload),
    )

    channel = get_progress_events_path(payload)
    _active_stage = None

    def _progress(stage, status, message=None):
        nonlocal _active_stage
        if status == "RUNNING":
            _active_stage = stage
        elif status in ("SUCCESS", "FAILED"):
            _active_stage = None
        emit_progress_event(
            run_id=run_id,
            ticket_id=ticket_id,
            stage=stage,
            status=status,
            channel_path=channel,
            message=message,
        )

    _stage_timings = []
    _t0 = time.monotonic()

    def _stage_time(stage):
        nonlocal _t0
        elapsed = time.monotonic() - _t0
        _stage_timings.append({"stage": stage, "duration_seconds": round(elapsed, 3)})
        _t0 = time.monotonic()

    exp3_provider_boundary = None
    exp3_boundary_factory_resolved = None
    exp3_attempt_accountings = []
    exp3_attempt_records = []
    exp3_retrieval_deliveries = []
    retrieval_delivery = payload.get("_retrieval_delivery")
    try:
        _progress("fetch", "RUNNING", "starting pipeline")

        manager_star_mode = _uses_manager_star(payload)
        if not manager_star_mode and (
            exp3_production_role_runner is not None
            or exp3_audit_directory is not None
        ):
            raise TypeError("M1 runtime injections require manager_star mode")
        if manager_star_mode:
            from exp3.architecture import require_manager_star_enabled

            # Refuse M1 before loading any formal manifest when its explicit
            # kill switch is off. The router repeats this check defensively.
            require_manager_star_enabled()
        if "architecture_mode" in payload:
            from exp3.production_provider import boundary_from_environment

            exp3_boundary_factory_resolved = exp3_boundary_factory or boundary_from_environment
            if not callable(exp3_boundary_factory_resolved):
                raise TypeError("exp3_boundary_factory must be callable")
            if not _uses_two_attempt_contract(payload):
                exp3_provider_boundary = exp3_boundary_factory_resolved(payload)
                if not manager_star_mode:
                    exp3_provider_boundary.start(payload)

        if manager_star_mode:
            # One observation may contain two complete fixed-topology attempts.
            # Each attempt owns a fresh 20-call/350k-token provider boundary. An
            # answer-blind completion check decides continuation: T3 uses the 25
            # validated item-store entries, while non-T3 tasks use the audited
            # target commit. Mathematical correctness is never inspected here.
            from exp3.attempt_control import (
                ExperimentTimeoutController,
                MAX_ATTEMPTS,
                attempt_record,
                deterministic_evaluation,
                freeze_attempt_snapshot,
                manager_star_commit_evidence,
                validate_two_attempt_controls,
                with_attempt_context,
            )
            from exp3.router import run_manager_star_runtime
            from exp3.telemetry import (
                aggregate_attempt_accountings,
                build_manager_star_telemetry,
            )
            from iteration_loop import record_attempt_snapshot_best_effort

            two_attempt_mode = _uses_two_attempt_contract(payload)
            if two_attempt_mode:
                validate_two_attempt_controls(payload)
            attempt_limit = MAX_ATTEMPTS if two_attempt_mode else 1
            attempt_timeout_controller = ExperimentTimeoutController(
                payload,
                maximum_attempts=attempt_limit,
                clock=exp3_clock or time.monotonic,
                timeout_error_cls=RunTimeout,
            )
            attempt_results = []
            attempt_audits = []
            all_committed_paths = set()
            last_error = None
            prior_status = None
            completion = None

            for attempt_number_value in range(1, attempt_limit + 1):
                try:
                    attempt_timeout_controller.start_attempt(attempt_number_value)
                except RunTimeout as exc:
                    exc.experiment_attempts = list(exp3_attempt_records)
                    raise
                attempt_payload = (
                    with_attempt_context(
                        payload,
                        attempt_number_value,
                        prior_status=prior_status,
                    )
                    if two_attempt_mode
                    else payload
                )
                boundary = (
                    exp3_boundary_factory_resolved(attempt_payload)
                    if two_attempt_mode
                    else exp3_provider_boundary
                )
                exp3_provider_boundary = boundary
                try:
                    attempt_result = run_manager_star_runtime(
                        attempt_payload,
                        tracer=tracer,
                        production_boundary=boundary,
                        production_role_runner=exp3_production_role_runner,
                        production_audit_directory=exp3_audit_directory,
                    )
                    try:
                        attempt_timeout_controller.check_attempt(
                            attempt_number_value
                        )
                    except RunTimeout as timeout_exc:
                        for attribute in (
                            "audit",
                            "calculation_tool_calls",
                            "commit_count",
                            "committed_paths",
                            "manager_acceptance_map",
                            "topology",
                        ):
                            if attribute in attempt_result:
                                setattr(
                                    timeout_exc,
                                    attribute,
                                    copy.deepcopy(attempt_result[attribute]),
                                )
                        raise
                except Exception as exc:
                    try:
                        attempt_timeout_controller.check_attempt(
                            attempt_number_value
                        )
                    except RunTimeout as timeout_exc:
                        for attribute in (
                            "audit",
                            "calculation_tool_calls",
                            "commit_count",
                            "committed_paths",
                            "manager_acceptance_map",
                            "topology",
                        ):
                            if hasattr(exc, attribute):
                                setattr(
                                    timeout_exc,
                                    attribute,
                                    copy.deepcopy(getattr(exc, attribute)),
                                )
                        exc = timeout_exc
                    exp3_retrieval_deliveries.append(
                        copy.deepcopy(attempt_payload.get("_retrieval_delivery"))
                    )
                    if not two_attempt_mode:
                        raise exc
                    last_error = exc
                    accounting = boundary.snapshot()
                    accounting["calculation_tool_calls"] = list(
                        getattr(exc, "calculation_tool_calls", []) or []
                    )
                    accounting["commit_count"] = int(
                        getattr(exc, "commit_count", 0) or 0
                    )
                    exp3_attempt_accountings.append(accounting)
                    calls = int(
                        ((accounting.get("budget") or {}).get("shared") or {}).get(
                            "calls", 0
                        )
                        or 0
                    )
                    paths = sorted(
                        set(getattr(exc, "committed_paths", []) or [])
                    )
                    all_committed_paths.update(paths)
                    failure_completion = manager_star_commit_evidence(
                        attempt_payload,
                        {
                            "commit_count": accounting["commit_count"],
                            "committed_paths": paths,
                        },
                    )
                    completion = failure_completion
                    record = attempt_record(
                        number=attempt_number_value,
                        runtime_status="failed",
                        provider_call_count=calls,
                        evidence=failure_completion,
                        failure_type=type(exc).__name__,
                    )
                    exp3_attempt_records.append(record)
                    record_attempt_snapshot_best_effort(
                        record,
                        attempt_payload,
                        attempt_number_value,
                        freeze_attempt_snapshot,
                    )
                    failed_attempt_result = {
                        "source_attempt": attempt_number_value,
                        "status": "failed",
                        "failure_phase": getattr(exc, "failure_phase", None),
                        "manager_final": {},
                        "developer_result": {},
                        "manager_acceptance_map": None,
                        "topology": None,
                    }
                    for output_name in ("manager_final", "developer_result"):
                        output_value = getattr(exc, output_name, None)
                        if isinstance(output_value, Mapping):
                            failed_attempt_result[output_name] = {
                                **copy.deepcopy(dict(output_value)),
                                "source_attempt": attempt_number_value,
                            }
                    for metadata_name in ("manager_acceptance_map", "topology"):
                        metadata_value = getattr(exc, metadata_name, None)
                        if isinstance(metadata_value, Mapping):
                            failed_attempt_result[metadata_name] = copy.deepcopy(
                                dict(metadata_value)
                            )
                    attempt_results.append(failed_attempt_result)
                    attempt_audits.extend(list(getattr(exc, "audit", []) or []))
                    if failure_completion["complete"] is True:
                        break
                    observation_timed_out = (
                        isinstance(exc, RunTimeout)
                        and getattr(exc, "timeout_scope", None) == "observation"
                    )
                    if (
                        attempt_number_value >= attempt_limit
                        or calls == 0
                        or observation_timed_out
                    ):
                        if calls == 0:
                            exc.experiment_attempts = list(exp3_attempt_records)
                            raise exc
                        break
                    prior_status = "retryable_workflow_failure"
                    continue

                exp3_retrieval_deliveries.append(
                    copy.deepcopy(attempt_payload.get("_retrieval_delivery"))
                )
                scoped_attempt_result = copy.deepcopy(attempt_result)
                scoped_attempt_result["source_attempt"] = attempt_number_value
                for output_name in ("manager_final", "developer_result"):
                    output_value = scoped_attempt_result.get(output_name)
                    if isinstance(output_value, Mapping):
                        scoped_attempt_result[output_name] = {
                            **copy.deepcopy(dict(output_value)),
                            "source_attempt": attempt_number_value,
                        }
                attempt_results.append(scoped_attempt_result)
                attempt_audits.extend(attempt_result.get("audit") or [])
                all_committed_paths.update(attempt_result.get("committed_paths") or [])
                accounting = {
                    "architecture_mode": "manager_star",
                    "rag_enabled": boundary.rag_enabled,
                    "provider_identity": boundary.provider_config.identity,
                    "identity_sha256": boundary.identity_sha256,
                    "budget": attempt_result.get("budget"),
                    "provider_calls": list(attempt_result.get("provider_calls") or []),
                    "isolation_preflight": list(
                        attempt_result.get("isolation_preflight") or []
                    ),
                    "calculation_tool_calls": list(
                        attempt_result.get("calculation_tool_calls") or []
                    ),
                    "commit_count": int(attempt_result.get("commit_count", 0) or 0),
                }
                exp3_attempt_accountings.append(accounting)
                if two_attempt_mode:
                    completion = manager_star_commit_evidence(
                        attempt_payload, attempt_result
                    )
                else:
                    completion = {
                        "complete": attempt_result.get("status") == "succeeded",
                        "commit_count": accounting["commit_count"],
                        "committed_paths": sorted(
                            set(attempt_result.get("committed_paths") or [])
                        ),
                        "target_path": (payload.get("strategy") or {}).get(
                            "target_path"
                        ),
                    }
                complete = completion["complete"] is True
                record = attempt_record(
                    number=attempt_number_value,
                    runtime_status="succeeded",
                    provider_call_count=int(
                        ((accounting.get("budget") or {}).get("shared") or {}).get(
                            "calls", 0
                        )
                        or 0
                    ),
                    evidence=completion,
                )
                exp3_attempt_records.append(record)
                record_attempt_snapshot_best_effort(
                    record,
                    attempt_payload,
                    attempt_number_value,
                    freeze_attempt_snapshot,
                )
                if complete:
                    break
                prior_status = (
                    "required_items_missing"
                    if (completion or {}).get("completion_basis")
                    == "t3_items_missing"
                    else "required_commit_missing"
                )

            aggregate = aggregate_attempt_accountings(
                exp3_attempt_accountings,
                architecture_mode="manager_star",
                planned_attempts=attempt_limit,
            )
            aggregate["attempts"] = list(exp3_attempt_records)
            if attempt_results:
                manager_star_result = copy.deepcopy(attempt_results[-1])
            else:
                manager_star_result = {
                    "source_attempt": None,
                    "status": "failed",
                    "failure_phase": "manager_star_runtime",
                    "manager_final": {},
                    "developer_result": {},
                    "manager_acceptance_map": None,
                    "topology": None,
                }
            manager_star_result = dict(manager_star_result)
            retrieval_delivery = _attempt_retrieval_delivery(
                payload,
                exp3_retrieval_deliveries,
            )
            final_complete = (
                exp3_attempt_records[-1]["completion"] == "complete"
            )
            incomplete_completion = exp3_attempt_records[-1]["completion"]
            manager_star_result.update(
                {
                    "status": "succeeded" if final_complete else "failed",
                    "failure_phase": (
                        None
                        if final_complete
                        else (
                            getattr(last_error, "failure_phase", None)
                            or (
                                "required_items_missing"
                                if incomplete_completion
                                == "required_items_missing"
                                else "required_commit_missing"
                            )
                        )
                    ),
                    "budget": aggregate["budget"],
                    "provider_calls": aggregate["provider_calls"],
                    "calculation_tool_calls": aggregate["calculation_tool_calls"],
                    "commit_count": aggregate["commit_count"],
                    "committed_paths": sorted(all_committed_paths),
                    "isolation_preflight": aggregate["isolation_preflight"],
                    "audit": attempt_audits,
                    "attempts": list(exp3_attempt_records),
                    "provider_identity": aggregate["provider_identity"],
                    "rag_enabled": aggregate["rag_enabled"],
                }
            )
            experiment_telemetry = build_manager_star_telemetry(
                manager_star_result,
                model_alias=exp3_provider_boundary.provider_config.model_alias,
                provider_identity=exp3_provider_boundary.provider_config.identity,
                identity_sha256=exp3_provider_boundary.identity_sha256,
            )
            final = manager_star_result.get("manager_final") or {}
            committed_paths = manager_star_result.get("committed_paths") or []
            if (
                not isinstance(committed_paths, list)
                or any(type(path) is not str or not path for path in committed_paths)
                or committed_paths != sorted(set(committed_paths))
            ):
                raise ValueError(
                    "manager-star committed_paths must be a sorted unique string list"
                )
            target_branch = (payload.get("strategy") or {}).get("target_branch")
            accepted = manager_star_result.get("status") == "succeeded"
            pipeline_out = {
                "status": "succeeded" if accepted else "failed",
                "summary": str(
                    final.get("summary")
                    or (
                        "Manager-star run completed."
                        if accepted
                        else (
                            "The final manager-star attempt ended without a final "
                            "manager summary."
                        )
                    )
                ),
                "source_attempt": manager_star_result.get("source_attempt"),
                "artifacts": {
                    "feature_branch": target_branch,
                    "modified_files": committed_paths,
                    "new_files": [],
                },
            }
            if two_attempt_mode:
                manager_evaluation = deterministic_evaluation(
                    completion
                    or {
                        "complete": False,
                        "commit_count": aggregate["commit_count"],
                        "committed_paths": sorted(all_committed_paths),
                        "target_path": (payload.get("strategy") or {}).get("target_path"),
                    }
                )
            else:
                manager_evaluation = {
                    "evaluation": {
                        "met_criteria": None if accepted else False,
                        "confidence": 0.0,
                        "criteria_results": [],
                        "unrecognised_criteria": [],
                        "summary": (
                            "Manager-star execution completed; blinded quality evaluation is separate."
                            if accepted
                            else "Manager-star execution ended with a failed manager decision."
                        ),
                    },
                    "recommended_action": "review" if accepted else "escalate",
                }
            _stage_time("manager_star")
            _progress("fetch", "SUCCESS", "manager-star topology completed")
            _progress("report", "RUNNING", "building manager-star response")
            resp = build_response(
                run_id=run_id,
                ticket_id=ticket_id,
                start_time=start,
                end_time=now_iso(),
                outcome="success" if accepted else "quality",
                traces=tracer.as_list(),
                backtest=manager_evaluation,
                pipeline_out=pipeline_out,
                error_message=(
                    None
                    if accepted
                    else str(final.get("summary") or "manager-star decision failed")
                ),
                log_ref=LOG_REF,
                stage_timings=_stage_timings,
                command=payload.get("command"),
                input_datasets=payload.get("input_datasets"),
                retrieval_context=payload.get("retrieval_context"),
                retrieval_delivery=retrieval_delivery,
                experiment_telemetry=experiment_telemetry,
            )
            _progress("report", "SUCCESS", "manager-star result emitted")
            emit_result(resp, result_path)
            return 0 if accepted else 1

        elif _should_run_direct_backtest(payload):
            summary = (
                "Direct backtest mode: skipped the strategy rewrite and submitted "
                "the existing strategy.request baseline to the engine as-is."
            )
            tracer.record("rae", "direct_backtest", "succeeded", summary)
            pipeline_out = {
                "status": "no_changes",
                "issue_key": ticket_id,
                "summary": summary,
                "artifacts": {
                    "feature_branch": f"quant/{ticket_id}",
                    "modified_files": [],
                    "new_files": [],
                    "no_code_changes": True,
                    "direct_backtest": True,
                },
                "usage": {
                    "model": None,
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "cost_usd": None,
                },
            }
            _stage_time("pipeline")
            _progress("fetch", "SUCCESS", "direct backtest mode: skipped strategy rewrite")
            _progress("commit", "SUCCESS", "direct backtest mode: no commit")
            _progress("backtest", "RUNNING", "submitting existing baseline")
            ran_backtest = True
            backtest = _select_backtest_fn()(payload)
            _stage_time("backtest")

        elif _zero_code_modifications(payload):
            # The request explicitly forbids editing, so there is nothing to
            # branch, commit, or iterate on. The value of the run is
            # the findings, which the read-only agent reports for a human to read.
            summary = (
                "Read-only request: no branch was created and nothing was committed."
            )
            tracer.record("rae", "read_only", "succeeded", summary)
            pipeline_out = {
                "status": "no_changes",
                "issue_key": ticket_id,
                "summary": summary,
                "artifacts": {
                    "modified_files": [],
                    "new_files": [],
                    "no_code_changes": True,
                },
                "usage": {
                    "model": None,
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "cost_usd": None,
                },
            }
            _progress("fetch", "SUCCESS", "read-only request: skipped strategy edit")
            _progress("commit", "SUCCESS", "read-only request: no commit")

            from review_agent import analyse_read_only_task  # proxy/ is on sys.path

            ran_backtest = False
            backtest = analyse_read_only_task(payload)
            _stage_time("pipeline")

        else:
            # Both paths are edit -> evaluate -> iterate; they differ only in who
            # returns the verdict. A backtest ticket is scored by the engine; a
            # general ticket is scored by the review agent, which returns the same
            # evaluation/recommended_action shape carrying no metrics — so
            # build_response leaves performance_metrics null for it. iteration_controls
            # (allow_iteration, max_iterations, ...) bound both identically.
            from iteration_loop import run_exp3_observation_attempts, run_iteration_loop

            ran_backtest = _needs_backtest(payload)
            if (
                payload.get("architecture_mode") == "single_agent"
                and _uses_two_attempt_contract(payload)
            ):
                def run_exp3_single_attempt(attempt_payload):
                    nonlocal exp3_provider_boundary
                    # The boundary hashes the exact request supplied to start().
                    # Bind it to this attempt-scoped payload from creation through
                    # the pipeline's defensive second start, rather than mixing the
                    # original observation payload with its attempt-marked copy.
                    boundary = exp3_boundary_factory_resolved(attempt_payload)
                    exp3_provider_boundary = boundary
                    boundary.start(attempt_payload)
                    try:
                        result = run_pipeline_auto(
                            attempt_payload,
                            tracer=tracer,
                            exp3_provider_boundary=boundary,
                        )
                    except Exception as exc:
                        accounting = getattr(exc, "exp3_provider_accounting", None)
                        if not isinstance(accounting, dict):
                            accounting = boundary.snapshot()
                            exc.exp3_provider_accounting = accounting
                        exp3_attempt_accountings.append(accounting)
                        raise
                    else:
                        accounting = result.get("experiment3_provider_accounting")
                        if not isinstance(accounting, dict):
                            accounting = boundary.snapshot()
                        exp3_attempt_accountings.append(accounting)
                        return result
                    finally:
                        exp3_retrieval_deliveries.append(
                            copy.deepcopy(attempt_payload.get("_retrieval_delivery"))
                        )

                loop_out = run_exp3_observation_attempts(
                    payload,
                    run_exp3_single_attempt,
                    _select_evaluate_fn(payload),
                    tracer=tracer,
                    clock=exp3_clock,
                )
                exp3_attempt_records = loop_out.get("attempts") or []
                retrieval_delivery = _attempt_retrieval_delivery(
                    payload,
                    exp3_retrieval_deliveries,
                )
            else:
                loop_out = run_iteration_loop(
                    payload,
                    lambda p: run_pipeline_auto(
                        p,
                        tracer=tracer,
                        exp3_provider_boundary=exp3_provider_boundary,
                    ),
                    _select_evaluate_fn(payload),
                    tracer=tracer,
                )
            pipeline_out = loop_out["pipeline_out"]
            backtest = loop_out["backtest"]
            _stage_time("pipeline")
            _progress("fetch", "SUCCESS", "fetch/modify phase completed")
            _progress("commit", "SUCCESS", "pipeline completed")

        # Only claim a backtest stage when the engine actually ran: a general run's
        # evaluator fills the same slot with a review verdict, not a backtest.
        if ran_backtest and backtest is not None:
            _progress("backtest", "SUCCESS", "backtest completed")
        _progress("report", "RUNNING", "building response")
        
        outcome = "success"
        error_message = None
        if not ran_backtest and not _zero_code_modifications(payload):
            action = (backtest or {}).get("recommended_action")
            if action != "accept":
                outcome = "quality"
                evaluation = (backtest or {}).get("evaluation") or {}
                error_message = (
                    evaluation.get("summary")
                    or f"Repository edit requires revision; reviewer action was {action or 'missing'}."
                )

        experiment_telemetry = None
        if exp3_provider_boundary is not None:
            from exp3.telemetry import (
                aggregate_attempt_accountings,
                build_single_agent_telemetry,
            )

            if exp3_attempt_accountings:
                accounting = aggregate_attempt_accountings(
                    exp3_attempt_accountings,
                    architecture_mode="single_agent",
                )
                accounting["attempts"] = list(exp3_attempt_records)
            else:
                accounting = (
                    (pipeline_out or {}).get("experiment3_provider_accounting")
                    or exp3_provider_boundary.snapshot()
                )
            if outcome == "success":
                single_agent_failure_phase = None
            elif (
                exp3_attempt_records
                and exp3_attempt_records[-1].get("completion")
                == "required_items_missing"
            ):
                single_agent_failure_phase = "required_items_missing"
            else:
                single_agent_failure_phase = "quality_validation"
            experiment_telemetry = build_single_agent_telemetry(
                accounting,
                status="succeeded" if outcome == "success" else "failed",
                failure_phase=single_agent_failure_phase,
                model_alias=exp3_provider_boundary.provider_config.model_alias,
                provider_identity=exp3_provider_boundary.provider_config.identity,
                identity_sha256=exp3_provider_boundary.identity_sha256,
                commit_count=accounting.get("commit_count", 0),
            )

        resp = build_response(
            run_id=run_id,
            ticket_id=ticket_id,
            start_time=start,
            end_time=now_iso(),
            outcome=outcome,
            traces=tracer.as_list(),
            backtest=backtest,
            pipeline_out=pipeline_out,
            error_message=error_message,
            log_ref=LOG_REF,
            stage_timings=_stage_timings,
            command=payload.get("command"),
            input_datasets=payload.get("input_datasets"),
            retrieval_context=payload.get("retrieval_context"),
            retrieval_delivery=retrieval_delivery,
            experiment_telemetry=experiment_telemetry,
        )
        _progress("report", "SUCCESS", "result emitted")

    except Exception as e:
        _stage_time("failed")
        if _active_stage and _active_stage != "report":
            _progress(_active_stage, "FAILED", str(e))
        _progress("report", "FAILED", str(e))
        tracer.record("rae", "run", "failed", f"{type(e).__name__}: {e}")
        experiment_telemetry = None
        if payload.get("architecture_mode") == "manager_star":
            try:
                from exp3.telemetry import (
                    aggregate_attempt_accountings,
                    build_manager_star_telemetry,
                )

                manager_telemetry_source = e
                if exp3_attempt_accountings:
                    aggregate = aggregate_attempt_accountings(
                        exp3_attempt_accountings,
                        architecture_mode="manager_star",
                        planned_attempts=(
                            2 if _uses_two_attempt_contract(payload) else 1
                        ),
                    )
                    manager_telemetry_source = {
                        "status": "failed",
                        "failure_phase": getattr(e, "failure_phase", None),
                        "budget": aggregate["budget"],
                        "audit": list(attempt_audits),
                        "provider_calls": aggregate["provider_calls"],
                        "calculation_tool_calls": aggregate[
                            "calculation_tool_calls"
                        ],
                        "commit_count": aggregate["commit_count"],
                        "isolation_preflight": aggregate[
                            "isolation_preflight"
                        ],
                        "manager_acceptance_map": getattr(
                            e, "manager_acceptance_map", None
                        ),
                        "topology": getattr(e, "topology", None),
                        "provider_identity": aggregate["provider_identity"],
                        "rag_enabled": aggregate["rag_enabled"],
                        "attempts": list(
                            getattr(e, "experiment_attempts", None)
                            or exp3_attempt_records
                        ),
                    }
                experiment_telemetry = build_manager_star_telemetry(
                    manager_telemetry_source,
                    model_alias=exp3_provider_boundary.provider_config.model_alias,
                    provider_identity=exp3_provider_boundary.provider_config.identity,
                    identity_sha256=exp3_provider_boundary.identity_sha256,
                )
            except Exception:
                # Never replace the original sanitized terminal failure with a
                # secondary telemetry-shaping error.
                experiment_telemetry = None
        elif (
            payload.get("architecture_mode") == "single_agent"
            and exp3_provider_boundary is not None
        ):
            try:
                from exp3.telemetry import (
                    aggregate_attempt_accountings,
                    build_single_agent_telemetry,
                )

                if exp3_attempt_accountings:
                    accounting = aggregate_attempt_accountings(
                        exp3_attempt_accountings,
                        architecture_mode="single_agent",
                    )
                    accounting["attempts"] = list(
                        getattr(e, "experiment_attempts", exp3_attempt_records) or []
                    )
                else:
                    accounting = getattr(
                        e,
                        "exp3_provider_accounting",
                        exp3_provider_boundary.snapshot(),
                    )
                experiment_telemetry = build_single_agent_telemetry(
                    accounting,
                    status="failed",
                    failure_phase="single_agent_runtime",
                    model_alias=exp3_provider_boundary.provider_config.model_alias,
                    provider_identity=exp3_provider_boundary.provider_config.identity,
                    identity_sha256=exp3_provider_boundary.identity_sha256,
                    commit_count=accounting.get("commit_count", 0),
                )
            except Exception:
                experiment_telemetry = None
        if exp3_retrieval_deliveries:
            try:
                retrieval_delivery = _attempt_retrieval_delivery(
                    payload,
                    exp3_retrieval_deliveries,
                )
            except RetrievalDeliveryEvidenceError as delivery_error:
                tracer.record(
                    "rae",
                    "retrieval_delivery",
                    "failed",
                    str(delivery_error),
                )
                retrieval_delivery = None
        resp = build_response(
            run_id=run_id,
            ticket_id=ticket_id,
            start_time=start,
            end_time=now_iso(),
            outcome=_classify(e),
            traces=tracer.as_list(),
            pipeline_out=getattr(e, "pipeline_out", None),
            error_message=f"{type(e).__name__}: {e}",
            log_ref=LOG_REF,
            stage_timings=_stage_timings,
            command=payload.get("command"),
            input_datasets=payload.get("input_datasets"),
            retrieval_context=payload.get("retrieval_context"),
            retrieval_delivery=retrieval_delivery,
            experiment_telemetry=experiment_telemetry,
        )

    emit_result(resp, result_path)
    return 0 if resp["execution_summary"]["status"] == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
