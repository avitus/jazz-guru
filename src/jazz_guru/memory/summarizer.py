from __future__ import annotations

import uuid
from typing import Any

from jazz_guru.llm import complete
from jazz_guru.memory.store import MemoryStore, get_memory

SUMMARIZER_SYSTEM = (
    "You compress conversation histories into a compact rolling summary. "
    "Preserve concrete decisions, named artifacts, open questions, and the user's stated taste. "
    "Be terse. Reply with the summary only, no preamble."
)


def _format_history(history: list[dict[str, Any]], max_chars: int = 12000) -> str:
    parts: list[str] = []
    for m in history:
        role = m.get("role", "?")
        content = m.get("content", "")
        if isinstance(content, list):
            text_chunks: list[str] = []
            for blk in content:
                if isinstance(blk, dict):
                    if blk.get("type") == "text":
                        text_chunks.append(blk.get("text", ""))
                    elif blk.get("type") == "tool_use":
                        text_chunks.append(f"[tool_use {blk.get('name')}]")
                    elif blk.get("type") == "tool_result":
                        text_chunks.append("[tool_result]")
            content = "\n".join(text_chunks)
        parts.append(f"### {role}\n{content}")
    text = "\n\n".join(parts)
    if len(text) > max_chars:
        text = text[-max_chars:]
    return text


async def summarize_history(history: list[dict[str, Any]]) -> str:
    if not history:
        return ""
    formatted = _format_history(history)
    resp = await complete(
        [{"role": "user", "content": f"Summarize:\n\n{formatted}"}],
        system=SUMMARIZER_SYSTEM,
        max_tokens=600,
        temperature=0.2,
    )
    return resp.text.strip()


async def summarize_and_store(
    *,
    session_id: uuid.UUID,
    history: list[dict[str, Any]],
    store: MemoryStore | None = None,
) -> str:
    summary = await summarize_history(history)
    if summary:
        s = store or get_memory()
        await s.write(text=summary, kind="summary", session_id=session_id, score=0.5)
    return summary
