from __future__ import annotations

import shutil
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel, Field

from jazz_guru.actions.context import current
from jazz_guru.actions.registry import registry
from jazz_guru.actions.sandbox import resolve_in_workspace
from jazz_guru.config import get_settings


class SpeechSynthesizer(ABC):
    @abstractmethod
    def synth(self, text: str, out_path: Path) -> dict[str, object]: ...


class StubSynth(SpeechSynthesizer):
    def synth(self, text: str, out_path: Path) -> dict[str, object]:
        out_path.with_suffix(".txt").write_text(text, encoding="utf-8")
        return {"path": str(out_path.with_suffix(".txt")), "engine": "stub"}


class PiperSynth(SpeechSynthesizer):
    def __init__(self, model_path: str | None = None) -> None:
        self.model_path = model_path

    def synth(self, text: str, out_path: Path) -> dict[str, object]:
        piper = shutil.which("piper")
        if not piper:
            return {"error": "piper binary not on PATH"}
        cmd = [piper, "--output_file", str(out_path)]
        if self.model_path:
            cmd.extend(["--model", self.model_path])
        proc = subprocess.run(cmd, input=text.encode("utf-8"), capture_output=True)
        if proc.returncode != 0:
            return {"error": "piper failed", "stderr": proc.stderr.decode("utf-8", errors="replace")}
        return {"path": str(out_path), "engine": "piper"}


def get_synth() -> SpeechSynthesizer:
    s = get_settings()
    if not s.feature_tts:
        return StubSynth()
    return PiperSynth()


class TtsInput(BaseModel):
    text: str
    out_path: str = Field(..., description="Output .wav path in workspace (or .txt if stub).")


@registry.register(
    "tts",
    description="Synthesize speech to a .wav file. Stub writes .txt unless FEATURE_TTS=1 + piper installed.",
    input_model=TtsInput,
    tags=("audio", "tts"),
)
async def tts(text: str, out_path: str) -> dict[str, object]:
    p = resolve_in_workspace(out_path, current().session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    return get_synth().synth(text, p)
