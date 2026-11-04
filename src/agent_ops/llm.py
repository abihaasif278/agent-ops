"""LLM provider with token accounting.

Every call is metered: the provider returns the parsed object and adds the call's usage
to a ``UsageMeter``. That's what lets each pipeline run report tokens and dollar cost.

* ``AnthropicProvider`` reads real ``usage`` from the API response.
* ``MockProvider`` runs deterministic handlers and *estimates* tokens (~4 chars/token), so
  cost tracking can be exercised and tested without spending anything.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

DEFAULT_MODEL = "claude-opus-5-5"
# USD per million tokens (input, output). Update when pricing changes.
PRICING = {"claude-opus-5-5": (4.00, 20.00), "claude-sonnet-5-5": (2.00, 10.00), "claude-haiku-5-5": (0.10, 0.50)}


class LLMError(RuntimeError):
    pass


@dataclass
class UsageMeter:
    model: str = DEFAULT_MODEL
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    by_task: dict[str, dict[str, int]] = field(default_factory=dict)

    def add(self, task: str, input_tokens: int, output_tokens: int) -> None:
        self.calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        t = self.by_task.setdefault(task, {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        t["calls"] += 1
        t["input_tokens"] += input_tokens
        t["output_tokens"] += output_tokens

    @property
    def cost_usd(self) -> float:
        inp, out = PRICING.get(self.model, PRICING[DEFAULT_MODEL])
        return round(self.input_tokens / 1e6 * inp + self.output_tokens / 1e6 * out, 6)

    def snapshot(self) -> dict:
        return {"model": self.model, "calls": self.calls, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "cost_usd": self.cost_usd, "by_task": self.by_task}


class LLMProvider(Protocol):
    name: str
    meter: UsageMeter

    def structured(self, *, task: str, system: str, prompt: str, schema: type[T],
                   context: dict[str, Any] | None = None) -> T: ...


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class MockProvider:
    name = "mock"

    def __init__(self, handlers: dict[str, Callable[[dict[str, Any]], BaseModel]]):
        self._handlers = handlers
        self.meter = UsageMeter()

    def structured(self, *, task, system, prompt, schema, context=None):
        if task not in self._handlers:
            raise LLMError(f"No mock handler for task '{task}'")
        result = schema.model_validate(self._handlers[task](context or {}).model_dump())
        self.meter.add(task, estimate_tokens(system + prompt), estimate_tokens(result.model_dump_json()))
        return result


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str | None = None, max_tokens: int = 4096):
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise LLMError("pip install 'agent-ops[anthropic]'") from exc
        self._client = anthropic.Anthropic()
        self._model = model or os.getenv("ANTHROPIC_MODEL", DEFAULT_MODEL)
        self._max_tokens = max_tokens
        self.meter = UsageMeter(model=self._model)

    def structured(self, *, task, system, prompt, schema, context=None):
        response = self._client.messages.parse(
            model=self._model, max_tokens=self._max_tokens, system=system,
            messages=[{"role": "user", "content": prompt}], output_format=schema,
        )
        self.meter.add(task, response.usage.input_tokens, response.usage.output_tokens)
        if response.stop_reason == "refusal" or response.parsed_output is None:
            raise LLMError(f"No usable output for task '{task}' (stop_reason={response.stop_reason})")
        return response.parsed_output


def get_provider(handlers) -> LLMProvider:
    choice = os.getenv("LLM_PROVIDER", "mock").lower()
    if choice == "mock":
        return MockProvider(handlers)
    if choice == "anthropic":
        return AnthropicProvider()
    raise LLMError(f"Unknown LLM_PROVIDER '{choice}'")
