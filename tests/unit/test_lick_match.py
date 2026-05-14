from __future__ import annotations

from pathlib import Path

import pytest

from jazz_guru.actions import ToolContext, register_all, reset_tool_context, set_tool_context
from jazz_guru.config import get_settings
from jazz_guru.lickmatch import (
    NGRAM_SIZE,
    clear_index_cache,
    encode_midi,
    encode_notes,
    load_index,
    quantize_ioi,
    search,
)


@pytest.fixture(autouse=True)
def _fresh_index_cache() -> None:
    """The match index is a process-wide cache; reset it around every test."""
    clear_index_cache()
    yield
    clear_index_cache()


@pytest.fixture
def isolated_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(get_settings(), "jg_workspace_dir", tmp_path)
    return tmp_path


# --- encoding ---------------------------------------------------------------


def test_quantize_ioi_round_half_up_and_floor() -> None:
    assert quantize_ioi(0.25) == 1  # 0.25 beat -> 1 sixteenth
    assert quantize_ioi(0.5) == 2  # eighth note
    assert quantize_ioi(1.0) == 4  # quarter note
    assert quantize_ioi(0.625) == 3  # 2.5 -> round-half-up -> 3
    assert quantize_ioi(0.0) == 1  # never below 1, even for a zero delta


def test_encode_notes_intervals_and_iois() -> None:
    pitches = [60, 64, 67, 72]
    onsets = [0.0, 0.5, 1.0, 2.0]
    intervals, iois = encode_notes(pitches, onsets)
    assert intervals == [4, 3, 5]
    assert iois == [2, 2, 4]


def test_encode_notes_rejects_unequal_lengths() -> None:
    with pytest.raises(ValueError):
        encode_notes([60, 62], [0.0])


def test_encode_midi_round_trip(isolated_workspace: Path) -> None:
    import mido

    mid = mido.MidiFile(ticks_per_beat=480)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    # Notes at beats 0, 0.5, 1.0 -> pitches 60, 64, 67.
    track.append(mido.Message("note_on", note=60, velocity=96, time=0))
    track.append(mido.Message("note_off", note=60, velocity=0, time=240))
    track.append(mido.Message("note_on", note=64, velocity=96, time=0))
    track.append(mido.Message("note_off", note=64, velocity=0, time=240))
    track.append(mido.Message("note_on", note=67, velocity=96, time=0))
    track.append(mido.Message("note_off", note=67, velocity=0, time=240))
    path = isolated_workspace / "lick.mid"
    mid.save(str(path))

    intervals, iois = encode_midi(path)
    assert intervals == [4, 3]
    assert iois == [2, 2]


# --- search (against the committed WJazzD index) ----------------------------


def test_index_loads_and_caches() -> None:
    idx = load_index()
    assert len(idx.sources) == 456
    assert len(idx.phrases) == 456
    assert idx.ngram_size == NGRAM_SIZE
    # Phrases carry parallel interval/ioi vectors.
    for ph in idx.phrases[:5]:
        assert len(ph.intervals) == len(ph.iois)
    # Second call hits the cache -> same object identity.
    assert load_index() is idx


def _long_phrase_slice(n: int = 40) -> tuple[str, list[int], list[int]]:
    """Pick a real phrase with enough intervals; return (source_id, intervals, iois)."""
    idx = load_index()
    for ph in idx.phrases:
        if len(ph.intervals) >= n:
            return ph.sourceId, ph.intervals[:n], ph.iois[:n]
    raise AssertionError("no phrase long enough in the corpus")


def test_search_self_match_is_perfect() -> None:
    source_id, intervals, iois = _long_phrase_slice(40)
    results = search(intervals, iois, min_score=0.75, top_k=3)
    assert results, "a real sub-sequence of the corpus must match something"
    ids = {r.source_id for r in results}
    assert source_id in ids
    self_match = next(r for r in results if r.source_id == source_id)
    assert self_match.score == pytest.approx(1.0)
    # Results come back sorted by score, descending.
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


def test_search_min_score_filters() -> None:
    _, intervals, iois = _long_phrase_slice(40)
    # An impossible threshold drops everything; a permissive one keeps matches.
    assert search(intervals, iois, min_score=1.01) == []
    assert search(intervals, iois, min_score=0.5) != []


def test_search_top_k_limits_results() -> None:
    _, intervals, iois = _long_phrase_slice(40)
    one = search(intervals, iois, min_score=0.5, top_k=1)
    assert len(one) <= 1


def test_search_too_short_returns_empty() -> None:
    # Fewer than NGRAM_SIZE intervals can't produce an n-gram.
    assert search([1, 2, 3], [1, 1, 1], min_score=0.5) == []


def test_search_rejects_unequal_lengths() -> None:
    with pytest.raises(ValueError):
        search([1, 2, 3, 4, 5], [1, 1, 1])


# --- tool surface -----------------------------------------------------------


@pytest.fixture
def registered() -> object:
    return register_all()


async def test_lick_match_intervals_mode_self_match(registered: object) -> None:
    source_id, intervals, iois = _long_phrase_slice(40)
    tok = set_tool_context(ToolContext(session_id="lm1"))
    try:
        out = await registered.invoke(
            "lick_match", {"intervals": intervals, "iois": iois, "min_score": 0.75}
        )
    finally:
        reset_tool_context(tok)
    assert out["query_length"] == 40
    assert out["matches"], "self-query should match"
    top = out["matches"][0]
    assert top["source_id"] == source_id
    assert top["kind"] == "wjazzd"
    assert top["confidence"] == "quote"
    assert top["score"] == pytest.approx(1.0)
    assert top["label"] and top["attribution"].startswith("Weimar Jazz Database:")


async def test_lick_match_notes_mode(registered: object) -> None:
    notes = [
        {"pitch": 60, "start_beat": 0.0},
        {"pitch": None, "start_beat": 0.5},  # rest, skipped
        {"pitch": 64, "start_beat": 1.0},
        {"pitch": 67, "start_beat": 1.5},
        {"pitch": 72, "start_beat": 2.0},
        {"pitch": 71, "start_beat": 2.5},
        {"pitch": 69, "start_beat": 3.0},
    ]
    tok = set_tool_context(ToolContext(session_id="lm2"))
    try:
        out = await registered.invoke("lick_match", {"notes": notes})
    finally:
        reset_tool_context(tok)
    # 6 pitched notes (one rest skipped) -> 5 intervals.
    assert out["query_length"] == 5
    assert isinstance(out["matches"], list)


async def test_lick_match_midi_path_mode(registered: object, isolated_workspace: Path) -> None:
    import mido

    mid = mido.MidiFile(ticks_per_beat=480)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    for pitch in (60, 62, 64, 65, 67, 69):
        track.append(mido.Message("note_on", note=pitch, velocity=96, time=0))
        track.append(mido.Message("note_off", note=pitch, velocity=0, time=240))
    session = isolated_workspace / "sessions" / "lm3"
    session.mkdir(parents=True)
    mid.save(str(session / "scale.mid"))

    tok = set_tool_context(ToolContext(session_id="lm3"))
    try:
        out = await registered.invoke("lick_match", {"midi_path": "scale.mid"})
    finally:
        reset_tool_context(tok)
    assert out["query_length"] == 5
    assert isinstance(out["matches"], list)


async def test_lick_match_requires_exactly_one_mode(registered: object) -> None:
    tok = set_tool_context(ToolContext(session_id="lm4"))
    try:
        none = await registered.invoke("lick_match", {})
        both = await registered.invoke(
            "lick_match", {"notes": [{"pitch": 60, "start_beat": 0.0}], "intervals": [1]}
        )
    finally:
        reset_tool_context(tok)
    assert "error" in none
    assert "error" in both


async def test_lick_match_intervals_iois_length_mismatch(registered: object) -> None:
    tok = set_tool_context(ToolContext(session_id="lm5"))
    try:
        out = await registered.invoke(
            "lick_match", {"intervals": [1, 2, 3, 4, 5], "iois": [1, 1, 1]}
        )
    finally:
        reset_tool_context(tok)
    assert "error" in out


async def test_lick_match_too_short_query(registered: object) -> None:
    tok = set_tool_context(ToolContext(session_id="lm6"))
    try:
        out = await registered.invoke("lick_match", {"intervals": [1, 2], "iois": [1, 1]})
    finally:
        reset_tool_context(tok)
    assert out["matches"] == []
    assert "note" in out


async def test_lick_match_missing_midi_file(registered: object, isolated_workspace: Path) -> None:
    (isolated_workspace / "sessions" / "lm7").mkdir(parents=True)
    tok = set_tool_context(ToolContext(session_id="lm7"))
    try:
        out = await registered.invoke("lick_match", {"midi_path": "nope.mid"})
    finally:
        reset_tool_context(tok)
    assert "error" in out and "not found" in out["error"]


async def test_lick_match_midi_path_escape_rejected(
    registered: object, isolated_workspace: Path
) -> None:
    (isolated_workspace / "sessions" / "lm8").mkdir(parents=True)
    tok = set_tool_context(ToolContext(session_id="lm8"))
    try:
        out = await registered.invoke("lick_match", {"midi_path": "/etc/hosts"})
    finally:
        reset_tool_context(tok)
    assert "error" in out


async def test_lick_match_info(registered: object) -> None:
    tok = set_tool_context(ToolContext(session_id="lm9"))
    try:
        out = await registered.invoke("lick_match_info", {})
    finally:
        reset_tool_context(tok)
    assert out["source_count"] == 456
    assert out["phrase_count"] == 456
    assert out["ngram_size"] == NGRAM_SIZE
    assert out["distinct_performers"] >= 1
    assert out["kinds"].get("wjazzd") == 456
    assert out["year_range"] and out["year_range"][0] <= out["year_range"][1]
