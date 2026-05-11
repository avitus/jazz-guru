from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

InputKind = Literal["text", "audio"]


@dataclass
class AgentInput:
    kind: InputKind
    text: str | None = None
    audio_path: Path | None = None


def text(s: str) -> AgentInput:
    return AgentInput(kind="text", text=s)


def audio(path: str | Path) -> AgentInput:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(p)
    return AgentInput(kind="audio", audio_path=p)


def to_user_message(inp: AgentInput) -> str:
    """Render an AgentInput as a textual user message.

    Audio is described by metadata + a hint to call audio_analyze; the agent itself
    does the deeper analysis through tools. This keeps the LLM call cheap.
    """
    if inp.kind == "text":
        return inp.text or ""
    if inp.kind == "audio" and inp.audio_path is not None:
        return (
            f"[audio input: {inp.audio_path}]\n"
            f"Use the audio_analyze tool on the path above to extract features. "
            f"If you need symbolic notation, render or transcribe accordingly."
        )
    return ""
