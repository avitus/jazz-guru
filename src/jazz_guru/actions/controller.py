from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jazz_guru.actions.registry import ToolRegistry, register_all
from jazz_guru.config import Policy, get_policy, get_settings
from jazz_guru.llm import LLMResponse, LLMUsage, complete
from jazz_guru.logging import get_logger

log = get_logger(__name__)


@dataclass
class StepRecord:
    role: str
    content: list[dict[str, Any]]


@dataclass
class RunResult:
    final_text: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: int = 0
    rounds: int = 0
    stop_reason: str | None = None
    usage: LLMUsage = field(default_factory=LLMUsage)
    errors: list[str] = field(default_factory=list)


class ActionController:
    """Drive the Anthropic tool_use loop until the model emits an end_turn."""

    def __init__(
        self,
        registry: ToolRegistry | None = None,
        policy: Policy | None = None,
        max_rounds: int | None = None,
        on_event: Any = None,
    ) -> None:
        self.registry = registry or register_all()
        self.policy = policy or get_policy()
        # Decouple tool-call budget from LLM round limit: with a single
        # combined cap of N, the controller uses one round to call a tool
        # and then has nothing left for the model to read the result and
        # produce a final answer.
        self.max_tool_calls = self.policy.budgets.per_turn.tool_calls
        self.max_rounds = max_rounds or (self.max_tool_calls + 1)
        self.on_event = on_event  # optional callable(name, payload)
        # Don't cache the allowlist as an attribute. Dynamic tools attach
        # via ContextVar inside AgentLoop.step(); a frozen set here would
        # exclude them from to_anthropic() AND fail the policy check on
        # tool_use. Compute fresh each run() (cheap — just a registry walk).

    def _allowed_set(self) -> set[str]:
        allowed: set[str] = set()
        s = get_settings()
        for name in self.registry.names():
            tp = self.policy.for_tool(name)
            if tp.mode != "allow":
                continue
            if tp.feature_flag and not getattr(s, tp.feature_flag.lower(), 0):
                continue
            allowed.add(name)
        return allowed

    def _emit(self, name: str, payload: dict[str, Any]) -> None:
        if self.on_event:
            try:
                self.on_event(name, payload)
            except Exception as e:  # don't let logging break the loop
                log.warning("on_event_failed", err=str(e))

    async def run(
        self,
        *,
        system: str | list[dict[str, Any]],
        messages: list[dict[str, Any]],
        max_tokens: int | None = None,
        temperature: float = 0.7,
    ) -> RunResult:
        result = RunResult(final_text="", messages=list(messages))
        for round_idx in range(self.max_rounds):
            # Recompute on every round: if a previous round called
            # tool_create, the new tool is now in the dynamic overlay and
            # we want the next LLM request to see it. Otherwise the model
            # would be told to use a tool whose name we'd then reject.
            allowed = self._allowed_set()
            tools = self.registry.to_anthropic(allowed=allowed)
            result.rounds = round_idx + 1
            self._emit("llm_request", {"round": round_idx, "messages_len": len(result.messages)})
            try:
                resp: LLMResponse = await complete(
                    result.messages,
                    system=system,
                    tools=tools,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
            except Exception as e:
                err = f"llm error: {e}"
                result.errors.append(err)
                self._emit("error", {"phase": "llm", "error": err})
                break
            result.usage.add(resp.usage)
            result.stop_reason = resp.stop_reason
            self._emit("llm_response", {
                "round": round_idx,
                "stop_reason": resp.stop_reason,
                "tool_uses": [{"name": t["name"], "id": t["id"]} for t in resp.tool_uses],
                "usage": {"in": resp.usage.input_tokens, "out": resp.usage.output_tokens, "usd": resp.usage.cost_usd},
            })

            assistant_blocks: list[dict[str, Any]] = []
            if resp.text:
                assistant_blocks.append({"type": "text", "text": resp.text})
            for tu in resp.tool_uses:
                assistant_blocks.append({"type": "tool_use", "id": tu["id"], "name": tu["name"], "input": tu["input"]})
            result.messages.append({"role": "assistant", "content": assistant_blocks})

            if resp.stop_reason != "tool_use" or not resp.tool_uses:
                result.final_text = resp.text
                break

            tool_results: list[dict[str, Any]] = []
            for tu in resp.tool_uses:
                tool_calls_so_far = result.tool_calls
                if tool_calls_so_far >= self.max_tool_calls:
                    err = f"tool-call budget exceeded ({self.max_tool_calls})"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu["id"],
                        "is_error": True,
                        "content": err,
                    })
                    result.errors.append(err)
                    continue
                result.tool_calls += 1
                if tu["name"] not in allowed:
                    msg = f"tool '{tu['name']}' not allowed by policy"
                    self._emit("tool_result", {"id": tu["id"], "name": tu["name"], "error": msg})
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu["id"],
                        "is_error": True,
                        "content": msg,
                    })
                    # Match the other error paths (budget exceeded, tool
                    # exception): policy violations are surfaced in the
                    # aggregated `result.errors` so they show up in eval/
                    # logging downstream, not just in the model-facing
                    # tool_result.
                    result.errors.append(msg)
                    continue
                self._emit("tool_use", {"id": tu["id"], "name": tu["name"], "input": tu["input"]})
                try:
                    out = await self.registry.invoke(tu["name"], tu["input"] or {})
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu["id"],
                        "content": _to_tool_result_content(out),
                    })
                    self._emit("tool_result", {"id": tu["id"], "name": tu["name"], "ok": True})
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu["id"],
                        "is_error": True,
                        "content": err,
                    })
                    result.errors.append(err)
                    self._emit("tool_result", {"id": tu["id"], "name": tu["name"], "ok": False, "error": err})

            result.messages.append({"role": "user", "content": tool_results})
        else:
            result.errors.append(f"max_rounds {self.max_rounds} reached without end_turn")
        return result


def _to_tool_result_content(value: Any) -> list[dict[str, Any]]:
    import json

    if isinstance(value, str):
        return [{"type": "text", "text": value}]
    try:
        return [{"type": "text", "text": json.dumps(value, ensure_ascii=False, default=str)}]
    except Exception:
        return [{"type": "text", "text": repr(value)}]
