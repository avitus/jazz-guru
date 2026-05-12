from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from jazz_guru.actions import (
    ActionController,
    ToolContext,
    reset_tool_context,
    set_tool_context,
)
from jazz_guru.actions import store as tool_store
from jazz_guru.actions.dynamic import DynamicRegistry
from jazz_guru.actions.jobs import BackgroundJobRegistry
from jazz_guru.actions.jobs import attach_jobs as _attach_jobs
from jazz_guru.actions.jobs import detach_jobs as _detach_jobs
from jazz_guru.actions.registry import registry as static_registry
from jazz_guru.actions.tools.tool_meta import (
    reset_event_sink as _reset_meta_event_sink,
)
from jazz_guru.actions.tools.tool_meta import (
    set_event_sink as _set_meta_event_sink,
)
from jazz_guru.context import BuildInputs, ContextBuilder
from jazz_guru.db import session_scope
from jazz_guru.harness.session import SessionHandle
from jazz_guru.llm import LLMUsage
from jazz_guru.logging import TraceWriter, get_logger
from jazz_guru.memory import MemoryStore, get_memory
from jazz_guru.state import (
    EventType,
    PlaybookEntry,
    StateDoc,
    Turn,
    list_session_artifacts,
    load_latest,
    log_event,
    state_from_snapshot,
    write_snapshot,
)

log = get_logger(__name__)


@dataclass
class TurnResult:
    text: str
    tool_calls: int = 0
    rounds: int = 0
    usage: LLMUsage = field(default_factory=LLMUsage)
    errors: list[str] = field(default_factory=list)


class AgentLoop:
    """Main perceive -> plan -> act -> observe loop."""

    def __init__(
        self,
        session: SessionHandle,
        *,
        controller: ActionController | None = None,
        builder: ContextBuilder | None = None,
        memory: MemoryStore | None = None,
        retrieve_k: int = 5,
    ) -> None:
        self.session = session
        self.trace = TraceWriter(session.id)
        self.dynamic = DynamicRegistry()
        # Background jobs persist for the lifetime of the AgentLoop (i.e. the
        # session): a render started in turn 1 should be inspectable in turn 5.
        self.jobs = BackgroundJobRegistry()
        self.controller = controller or ActionController(on_event=self._on_event)
        self.builder = builder or ContextBuilder()
        self.memory = memory or get_memory()
        self.retrieve_k = retrieve_k

    def _on_event(self, name: str, payload: dict[str, Any]) -> None:
        self.trace.write(name, payload)

    async def _hydrate_dynamic_registry(self) -> None:
        """Load globally-published tools into this session's dynamic registry."""
        try:
            specs = await tool_store.load_all_specs()
        except Exception as e:
            log.warning("dynamic_tools.load_failed", err=str(e))
            return
        for s in specs:
            self.dynamic.add(s)

    async def _retrieve_memory(self, query: str) -> list[str]:
        try:
            recs = await self.memory.search(query, k=self.retrieve_k, session_id=self.session.id)
        except Exception as e:
            log.warning("memory.search_failed", err=str(e))
            return []
        return [f"[{r.kind}] {r.text}" for r in recs]

    async def _load_playbook(self, k: int = 8) -> list[str]:
        try:
            async with session_scope() as s:
                rows = (
                    await s.execute(
                        select(PlaybookEntry).order_by(PlaybookEntry.score.desc()).limit(k)
                    )
                ).scalars().all()
            return [r.text for r in rows]
        except Exception as e:
            log.warning("playbook.load_failed", err=str(e))
            return []

    def _state_doc(self) -> StateDoc:
        snap = load_latest(self.session.id)
        doc = state_from_snapshot(snap)
        artifacts = list_session_artifacts(self.session.id)
        if artifacts:
            doc.artifacts = artifacts
        return doc

    async def _record_turn(
        self,
        *,
        idx: int,
        role: str,
        content: dict[str, Any],
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
        ended: bool = False,
    ) -> uuid.UUID:
        async with session_scope() as s:
            t = Turn(
                session_id=self.session.id,
                idx=idx,
                role=role,
                content=content,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost_usd,
                ended_at=datetime.now(UTC) if ended else None,
            )
            s.add(t)
            await s.flush()
            return t.id

    async def step(self, user_input: str) -> TurnResult:
        idx = self.session.next_turn_idx
        log.info("agent_loop.step", session_id=str(self.session.id), idx=idx)
        self.trace.write("turn_start", {"idx": idx, "input": user_input})
        await log_event(session_id=self.session.id, event_type=EventType.TURN_START.value,
                        payload={"idx": idx, "input": user_input})

        user_turn_id = await self._record_turn(idx=idx, role="user", content={"text": user_input})

        retrieved = await self._retrieve_memory(user_input)
        playbook = await self._load_playbook()
        state_doc = self._state_doc()

        prompt = self.builder.build(BuildInputs(
            user_message=user_input,
            history=self.session.history,
            state_doc=state_doc.render_markdown(),
            retrieved_memory=retrieved,
            playbook_excerpts=playbook,
        ))

        await self._hydrate_dynamic_registry()
        # Per-async-task scoped (ContextVar tokens), so concurrent turns in
        # the same process don't clobber each other. The controller now
        # recomputes its allowlist inside run(), so dynamic tools attached
        # here automatically appear there — no explicit push needed.
        dyn_token = static_registry.attach_dynamic(self.dynamic)
        meta_token = _set_meta_event_sink(self._on_event)
        # Background job registry is per-session but bound via ContextVar for
        # the turn so concurrent sessions don't see each other's jobs.
        jobs_token = _attach_jobs(self.jobs)

        tok = set_tool_context(ToolContext(session_id=str(self.session.id), turn_idx=idx))
        try:
            run = await self.controller.run(system=prompt.system, messages=prompt.messages)
        finally:
            reset_tool_context(tok)
            static_registry.detach_dynamic(dyn_token)
            _reset_meta_event_sink(meta_token)
            _detach_jobs(jobs_token)

        await self._record_turn(
            idx=idx + 1,
            role="assistant",
            content={"text": run.final_text, "messages_added": len(run.messages) - len(prompt.messages)},
            tokens_in=run.usage.input_tokens,
            tokens_out=run.usage.output_tokens,
            cost_usd=run.usage.cost_usd,
            ended=True,
        )

        try:
            await self.memory.write(
                text=f"USER: {user_input}\nASSISTANT: {run.final_text[:1000]}",
                kind="turn",
                session_id=self.session.id,
                meta={"idx": idx, "tool_calls": run.tool_calls},
            )
        except Exception as e:
            log.warning("memory.write_failed", err=str(e))

        snap_payload = {
            "session_id": str(self.session.id),
            "turn_idx": idx,
            "summary": (run.final_text or "")[:1000],
            "open_threads": [],
            "artifacts": list_session_artifacts(self.session.id),
            "last_critique": state_doc.last_critique,
            "ts": datetime.now(UTC).isoformat(),
        }
        try:
            await write_snapshot(
                self.session.id, snap_payload, turn_id=user_turn_id, turn_idx=idx
            )
        except Exception as e:
            log.warning("snapshot.write_failed", err=str(e))

        await log_event(session_id=self.session.id, event_type=EventType.TURN_END.value,
                        payload={"idx": idx, "tool_calls": run.tool_calls, "rounds": run.rounds})
        self.trace.write("turn_end", {"idx": idx, "text": run.final_text, "tool_calls": run.tool_calls})

        self.session.next_turn_idx = idx + 2
        self.session.history.extend(run.messages[len(prompt.messages) - 1:])

        return TurnResult(
            text=run.final_text,
            tool_calls=run.tool_calls,
            rounds=run.rounds,
            usage=run.usage,
            errors=run.errors,
        )
