"""Termination boundaries from the request's iteration_controls.
run.py maps these exceptions onto IW's status/error_code:
  RunTimeout    -> TIMEOUT / TIMEOUT_REACHED
  BudgetExceeded-> FAILED  / RATE_LIMIT_EXCEEDED
"""

import time


class RunTimeout(Exception):
    pass


class BudgetExceeded(Exception):
    pass


class BudgetGuard:
    def __init__(
        self, *, timeout_seconds=None, max_token_budget=None, max_iterations=None
    ):
        self.timeout_seconds = timeout_seconds
        self.max_token_budget = max_token_budget
        self.max_iterations = max_iterations
        self._start = time.monotonic()
        self._tokens = 0
        self._iter = 0
        # --- usage recording (RAE-16) ---
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._calls = 0
        self._cost_usd = 0.0
        self._model = None

    def tick_iteration(self):
        self._iter += 1
        if self.max_iterations and self._iter > self.max_iterations:
            raise RunTimeout(f"max_iterations ({self.max_iterations}) exceeded")
        self.check_time()

    def add_tokens(self, n):
        self._tokens += int(n or 0)
        if self.max_token_budget and self._tokens > self.max_token_budget:
            raise BudgetExceeded(f"token budget ({self.max_token_budget}) exceeded")

    def check_time(self):
        if (
            self.timeout_seconds
            and (time.monotonic() - self._start) > self.timeout_seconds
        ):
            raise RunTimeout(f"timeout_seconds ({self.timeout_seconds}) exceeded")

    def record_llm_response(self, response_json: dict, *, model: str) -> None:
        """Record usage from a LiteLLM response and enforce token budget."""
        usage = response_json.get("usage") or {}
        pt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        ct = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)

        self._model = model
        self._calls += 1
        self._prompt_tokens += pt
        self._completion_tokens += ct

        cost = response_json.get("cost") or response_json.get("response_cost")
        if cost is None:
            cost = (response_json.get("_hidden_params") or {}).get("response_cost")
        if cost is not None:
            self._cost_usd += float(cost)

        self.add_tokens(pt + ct)

    def usage_dict(self) -> dict:
        return {
            "model": self._model,
            "calls": self._calls,
            "prompt_tokens": self._prompt_tokens,
            "completion_tokens": self._completion_tokens,
            "total_tokens": self._tokens,
            "cost_usd": round(self._cost_usd, 6),
        }