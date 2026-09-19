"""Collects the agent loop's per-step trace for the response's iteration_traces.
Iteration auto-increments; timestamps are timezone-aware UTC (RFC3339)."""

from datetime import datetime, timezone


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TraceCollector:
    def __init__(self):
        self._t: list[dict] = []

    def record(self, agent: str, tool_call: str, status: str, message: str) -> None:
        self._t.append(
            {
                "iteration": len(self._t) + 1,
                "agent": agent,
                "tool_call": tool_call,
                "status": status,
                "message": message,
                "timestamp": now_iso(),
            }
        )

    def as_list(self) -> list[dict]:
        return list(self._t)
