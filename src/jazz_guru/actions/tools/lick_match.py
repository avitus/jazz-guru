"""Typed read-only surface for Weimar Jazz Database lick matching.

Lets the agent ask "what does this melodic line sound like / where does it
come from?" against the WJazzD corpus of transcribed jazz solos. This is the
only sanctioned path to the index — the agent should not reach for
``fs_read`` / ``python_exec`` to parse ``data/wjazzd/wjazzd-index.json``
directly.

The WJazzD data is ODbL-licensed: every match carries source attribution
(performer + title + year) so credit follows the data — see
``data/wjazzd/ATTRIBUTION.md``.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from jazz_guru.actions.context import current
from jazz_guru.actions.registry import registry
from jazz_guru.actions.sandbox import resolve_in_safe
from jazz_guru.lickmatch import (
    MAX_SEQUENCE_LENGTH,
    NGRAM_SIZE,
    QUOTE_CONFIDENCE_THRESHOLD,
    encode_midi,
    encode_notes,
    format_attribution,
    format_label,
    load_index,
    search,
)


class _Empty(BaseModel):
    pass


class LickMatchInput(BaseModel):
    midi_path: str | None = Field(
        None,
        description=(
            "Path to a .mid file in the session workspace (or data/). Its note "
            "line is extracted (monophonic, ordered by onset) and matched."
        ),
    )
    notes: list[dict[str, Any]] | None = Field(
        None,
        description=(
            "Monophonic melody as [{pitch:int, start_beat:float}, ...], ordered "
            "by onset. `start_beat` is in quarter-note beats from phrase start. "
            "Entries with pitch null (rests) are skipped."
        ),
    )
    intervals: list[int] | None = Field(
        None,
        description=(
            "Pre-computed semitone intervals between consecutive notes. Use with "
            "`iois` to query a feature vector directly."
        ),
    )
    iois: list[int] | None = Field(
        None,
        description=(
            "Pre-computed inter-onset intervals in 16th-note ticks. Must be the "
            "same length as `intervals`."
        ),
    )
    min_score: float = Field(
        0.75,
        ge=0.0,
        le=1.0,
        description="Minimum 0..1 similarity for a match to be reported.",
    )
    top_k: int = Field(
        3, ge=1, le=50, description="Maximum number of ranked matches to return."
    )


def _intervals_from_notes(notes: list[dict[str, Any]]) -> tuple[list[int], list[int]]:
    pitches: list[int] = []
    onsets: list[float] = []
    prev_onset: float | None = None
    for i, n in enumerate(notes):
        if not isinstance(n, dict):
            raise ValueError(f"notes[{i}] must be an object with pitch and start_beat")
        pitch = n.get("pitch")
        if pitch is None:  # rest
            continue
        if "start_beat" not in n:
            raise ValueError(f"notes[{i}] is missing start_beat")
        try:
            pitch_int = int(pitch)
            onset = float(n["start_beat"])
        except (TypeError, ValueError) as e:
            raise ValueError(f"notes[{i}] has a non-numeric pitch/start_beat: {e}") from e
        if prev_onset is not None and onset < prev_onset:
            raise ValueError(
                f"notes[{i}] start_beat {onset} precedes the previous note "
                f"({prev_onset}); notes must be ordered by non-decreasing start_beat"
            )
        pitches.append(pitch_int)
        onsets.append(onset)
        prev_onset = onset
    return encode_notes(pitches, onsets)


@registry.register(
    "lick_match",
    description=(
        "Match a melodic line against the Weimar Jazz Database corpus of "
        "transcribed jazz solos. Supply exactly one of: `midi_path` (a .mid in "
        "the workspace), `notes` (a {pitch,start_beat} list), or `intervals`+"
        "`iois` (a pre-computed feature vector). Returns ranked attribution "
        "candidates — performer, title, year, and a 0..1 similarity score. "
        "Matching is transposition- and tempo-invariant."
    ),
    input_model=LickMatchInput,
    tags=("music", "lick_match"),
)
async def lick_match(
    midi_path: str | None = None,
    notes: list[dict[str, Any]] | None = None,
    intervals: list[int] | None = None,
    iois: list[int] | None = None,
    min_score: float = 0.75,
    top_k: int = 3,
) -> dict[str, Any]:
    modes = [
        midi_path is not None,
        notes is not None,
        intervals is not None or iois is not None,
    ]
    if sum(modes) != 1:
        return {
            "error": (
                "supply exactly one input mode: `midi_path`, `notes`, or "
                "`intervals`+`iois`"
            )
        }

    try:
        if midi_path is not None:
            p = resolve_in_safe(midi_path, current().session_id)
            if not p.exists():
                return {"error": f"midi file not found: {p}"}
            q_intervals, q_iois = encode_midi(p)
        elif notes is not None:
            q_intervals, q_iois = _intervals_from_notes(notes)
        else:
            if intervals is None or iois is None:
                return {"error": "`intervals` and `iois` must both be supplied"}
            if len(intervals) != len(iois):
                return {"error": "`intervals` and `iois` must have equal length"}
            q_intervals, q_iois = intervals, iois
    except PermissionError as e:
        return {"error": f"path rejected: {e}"}
    except ValueError as e:
        return {"error": str(e)}
    except Exception as e:  # mido parse failures, etc.
        return {"error": f"could not encode query: {e}"}

    if len(q_intervals) > MAX_SEQUENCE_LENGTH:
        return {"error": f"query too long: {len(q_intervals)} intervals (max {MAX_SEQUENCE_LENGTH})"}
    if len(q_intervals) < NGRAM_SIZE:
        return {
            "query_length": len(q_intervals),
            "matches": [],
            "note": (
                f"query has {len(q_intervals)} intervals; needs at least "
                f"{NGRAM_SIZE} ({NGRAM_SIZE + 1} notes) to match"
            ),
        }

    try:
        results = search(q_intervals, q_iois, min_score=min_score, top_k=top_k)
    except FileNotFoundError as e:
        return {"error": str(e)}
    return {
        "query_length": len(q_intervals),
        "matches": [
            {
                "kind": r.source.kind,
                "source_id": r.source_id,
                "label": format_label(r.source, r.start_bar),
                "attribution": format_attribution(r.source),
                "confidence": (
                    "quote" if r.score >= QUOTE_CONFIDENCE_THRESHOLD else "reminiscent"
                ),
                "score": round(r.score, 4),
                "matched": r.matched,
                "query_length": r.query_length,
            }
            for r in results
        ],
    }


@registry.register(
    "lick_match_info",
    description=(
        "Summarize the lick-matching corpus: source count, distinct performers, "
        "year range, and the n-gram size used for matching. Read-only."
    ),
    input_model=_Empty,
    tags=("music", "lick_match"),
)
async def lick_match_info() -> dict[str, Any]:
    try:
        idx = load_index()
    except FileNotFoundError as e:
        return {"error": str(e)}
    years = [s.year for s in idx.sources if s.year is not None]
    kinds: dict[str, int] = {}
    for s in idx.sources:
        kinds[s.kind] = kinds.get(s.kind, 0) + 1
    return {
        "source_count": len(idx.sources),
        "phrase_count": len(idx.phrases),
        "kinds": kinds,
        "distinct_performers": len({s.performer for s in idx.sources}),
        "year_range": [min(years), max(years)] if years else None,
        "ngram_size": idx.ngram_size,
        "license": "ODbL v1.0 — see data/wjazzd/ATTRIBUTION.md",
    }
