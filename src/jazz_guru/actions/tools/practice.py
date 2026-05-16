"""Agent tool: ``analyze_practice_take``.

Lets the LLM run the entire music-backend pipeline against an audio
file in the session workspace and get a structured
:class:`~jazz_guru.music.models.PracticeFeedback` back. Optional
backends are configured via the ``MUSIC_*_BACKEND`` env vars; absent
ones become non-fatal warnings on the result.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from jazz_guru.actions.context import current
from jazz_guru.actions.registry import registry
from jazz_guru.actions.sandbox import resolve_in_safe, resolve_in_workspace
from jazz_guru.music import analyze_practice_take


class AnalyzePracticeTakeInput(BaseModel):
    audio_path: str = Field(
        ..., description="Path to an audio file inside the session workspace (.wav/.flac/.m4a/.mp3)."
    )
    chart: str | None = Field(None, description="Chart/tune name, e.g. 'Autumn Leaves'.")
    instrument: str | None = Field(
        None, description="Performer's instrument hint, e.g. 'tenor-sax'."
    )
    lead_sheet_path: str | None = Field(
        None,
        description=(
            "Optional path to a lead-sheet file (MusicXML or plain-text chord chart). "
            "Resolved under workspace OR data/."
        ),
    )
    expected_key: str | None = Field(None, description="User-declared chart key, e.g. 'G minor'.")
    expected_tempo_bpm: float | None = Field(None, description="User-declared chart tempo (BPM).")
    chord_changes: list[str] | None = Field(
        None, description="Optional flat chord list, e.g. ['Cm7','F7','BbMaj7']."
    )


@registry.register(
    "analyze_practice_take",
    description=(
        "Run the configured music backends against an audio practice take and "
        "return structured feedback (tempo, key, transcription path, timing/pitch "
        "notes, plus warnings for any unavailable optional backends). "
        "Always returns a result — missing optional backends become warnings, "
        "never errors. Configure backends via the MUSIC_*_BACKEND env vars."
    ),
    input_model=AnalyzePracticeTakeInput,
    tags=("music", "audio", "practice"),
)
async def analyze_practice_take_tool(
    audio_path: str,
    chart: str | None = None,
    instrument: str | None = None,
    lead_sheet_path: str | None = None,
    expected_key: str | None = None,
    expected_tempo_bpm: float | None = None,
    chord_changes: list[str] | None = None,
) -> dict[str, Any]:
    sid = current().session_id
    audio = resolve_in_workspace(audio_path, sid)
    lead_resolved: Path | None = None
    if lead_sheet_path:
        # Lead sheets often live under data/ (curated charts) so use the
        # broader safe-root resolver rather than workspace-only.
        lead_resolved = resolve_in_safe(lead_sheet_path, sid)

    feedback = await analyze_practice_take(
        audio,
        chart=chart,
        instrument=instrument,
        lead_sheet_path=lead_resolved,
        expected_key=expected_key,
        expected_tempo_bpm=expected_tempo_bpm,
        chord_changes=chord_changes,
    )
    return feedback.model_dump(exclude_none=True, mode="json")
