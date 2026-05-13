"""Phase-3 tests: generation backends, accompaniment, exercises, deeper feedback.

All optional dependencies are stubbed — no real model loads, no network
calls, no GPU. Tests run in well under a second.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jazz_guru.actions import ToolContext, register_all, reset_tool_context, set_tool_context
from jazz_guru.config import get_settings
from jazz_guru.music.accompaniment import (
    BackingTrackResult,
    build_backing_track,
    normalize_chord_symbol,
)
from jazz_guru.music.exercises import arpeggio_exercise, ii_v_i_exercise, scale_exercise
from jazz_guru.music.feedback import compute_pitch_feedback, compute_timing_feedback
from jazz_guru.music.generation.elevenlabs_music_backend import ElevenLabsMusicBackend
from jazz_guru.music.generation.magenta_rt_backend import MagentaRealtimeBackend
from jazz_guru.music.models import (
    BeatTrackingResult,
    MusicGenerationRequest,
)


@pytest.fixture
def isolated_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(get_settings(), "jg_workspace_dir", tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# normalize_chord_symbol
# ---------------------------------------------------------------------------


def test_normalize_handles_flats_and_majors() -> None:
    assert normalize_chord_symbol("BbMaj7") == "B-maj7"
    assert normalize_chord_symbol("F#m7") == "F#m7"
    assert normalize_chord_symbol("E♭maj7") == "E-maj7"
    assert normalize_chord_symbol("C△7") == "Cmaj7"
    assert normalize_chord_symbol("CM7") == "Cmaj7"
    assert normalize_chord_symbol("Bbm7b5") == "B-m7b5"


# ---------------------------------------------------------------------------
# build_backing_track
# ---------------------------------------------------------------------------


def test_backing_track_writes_midi(tmp_path: Path) -> None:
    result = build_backing_track(
        ["Cm7", "F7", "BbMaj7", "EbMaj7"],
        tmp_path / "bt.mid",
        tempo_bpm=140,
        bars_per_chord=2,
        key="Bb major",
    )
    assert isinstance(result, BackingTrackResult)
    assert result.midi_path.exists()
    assert result.chord_count == 4
    assert result.bar_count == 8
    assert result.warnings == []

    # Spot-check the actual MIDI: it should contain note_on events for at least
    # the bass roots (4 chords * 2 bars * 4 beats = 32 bass notes).
    import mido  # type: ignore[import-untyped]

    mid = mido.MidiFile(str(result.midi_path))
    note_on = sum(
        1
        for track in mid.tracks
        for msg in track
        if msg.type == "note_on" and msg.velocity > 0
    )
    assert note_on >= 32


def test_backing_track_skips_unparseable_chord(tmp_path: Path) -> None:
    result = build_backing_track(
        ["Cm7", "ZZZ-not-a-chord"],
        tmp_path / "bt.mid",
        tempo_bpm=100,
    )
    assert result.chord_count == 1
    assert any("ZZZ" in w for w in result.warnings)
    # The unparseable chord still produces a rest so the bar grid is preserved.
    assert result.bar_count == 2


def test_backing_track_requires_at_least_one_chord(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        build_backing_track([], tmp_path / "bt.mid")


# ---------------------------------------------------------------------------
# exercises
# ---------------------------------------------------------------------------


def test_scale_exercise_writes_musicxml(tmp_path: Path) -> None:
    r = scale_exercise("Bb", tmp_path / "scale.musicxml", mode="major", octaves=2)
    assert r.musicxml_path.exists()
    assert r.notes >= 15  # 15 notes for one-octave scale up+down; more for two octaves
    body = r.musicxml_path.read_text(encoding="utf-8")
    assert "Bb" in body or "B-flat" in body or "B flat" in body


def test_arpeggio_exercise_writes_musicxml(tmp_path: Path) -> None:
    r = arpeggio_exercise("Cmaj7", tmp_path / "arp.musicxml", octaves=2)
    assert r.musicxml_path.exists()
    assert r.notes >= 8


def test_ii_v_i_exercise_writes_musicxml(tmp_path: Path) -> None:
    r = ii_v_i_exercise("Bb", tmp_path / "iiVi.musicxml")
    assert r.musicxml_path.exists()
    assert r.notes == 4
    body = r.musicxml_path.read_text(encoding="utf-8")
    # music21 emits MusicXML <harmony> elements rather than the literal
    # chord string; check for the structured tags it writes for each
    # chord in the ii-V-I sequence.
    assert "<harmony>" in body
    assert "minor-seventh" in body
    assert "dominant" in body
    assert "major-seventh" in body
    # And the I chord's root is B-flat:
    assert "<root-step>B</root-step>" in body
    assert "<root-alter>-1</root-alter>" in body


def test_scale_exercise_rejects_unknown_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        scale_exercise("C", tmp_path / "x.musicxml", mode="bogus")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# deeper feedback
# ---------------------------------------------------------------------------


def _write_simple_midi(path: Path, onsets_seconds: list[float], bpm: float = 120.0) -> None:
    """Write a tiny MIDI with notes at the given onset seconds."""
    import mido  # type: ignore[import-untyped]

    ticks_per_beat = 480
    mid = mido.MidiFile(ticks_per_beat=ticks_per_beat)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm), time=0))
    sec_per_tick = (60.0 / bpm) / ticks_per_beat
    last_tick = 0
    for sec in onsets_seconds:
        abs_tick = int(sec / sec_per_tick)
        delta = max(0, abs_tick - last_tick)
        track.append(mido.Message("note_on", note=60, velocity=64, time=delta))
        track.append(mido.Message("note_off", note=60, velocity=0, time=240))
        last_tick = abs_tick + 240
    track.append(mido.MetaMessage("end_of_track", time=0))
    mid.save(str(path))


def test_timing_feedback_detects_late_drift(tmp_path: Path) -> None:
    midi = tmp_path / "p.mid"
    # Beat grid at 120 BPM: beat times 0.0, 0.5, 1.0, 1.5
    # Onsets land 30 ms late on every beat.
    _write_simple_midi(midi, [0.030, 0.530, 1.030, 1.530])
    beats = BeatTrackingResult(
        backend="stub",
        tempo_bpm=120.0,
        beats_sec=[0.0, 0.5, 1.0, 1.5],
    )
    tf = compute_timing_feedback(midi, beats)
    assert tf is not None
    assert tf.mean_drift_ms is not None
    assert 20.0 < tf.mean_drift_ms < 50.0
    assert tf.late_count >= 2
    assert tf.early_count == 0


def test_timing_feedback_returns_none_when_no_midi() -> None:
    tf = compute_timing_feedback(None, BeatTrackingResult(backend="x"))
    assert tf is None


def test_pitch_feedback_with_chord_changes(tmp_path: Path) -> None:
    midi = tmp_path / "p.mid"
    # Three notes; ChordSymbol("Cmaj7") = {C, E, G, B}.
    # Pitches 60, 64, 65 -> C, E, F. F is out of Cmaj7 so 1 out of key.
    import mido  # type: ignore[import-untyped]

    mid = mido.MidiFile(ticks_per_beat=480)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    for pitch in (60, 64, 65):
        track.append(mido.Message("note_on", note=pitch, velocity=64, time=0))
        track.append(mido.Message("note_off", note=pitch, velocity=0, time=240))
    mid.save(str(midi))

    pf = compute_pitch_feedback(midi, chord_changes=["Cmaj7"])
    assert pf is not None
    assert pf.out_of_key_count == 1


def test_pitch_feedback_with_detected_key(tmp_path: Path) -> None:
    midi = tmp_path / "p.mid"
    # G major scale: G(67), A(69), B(71), C(72), D(74), E(76), F#(78), G(79)
    # plus one chromatic note Bb(70) which should land out-of-key.
    import mido  # type: ignore[import-untyped]

    mid = mido.MidiFile(ticks_per_beat=480)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    for pitch in (67, 69, 71, 70, 72):
        track.append(mido.Message("note_on", note=pitch, velocity=64, time=0))
        track.append(mido.Message("note_off", note=pitch, velocity=0, time=240))
    mid.save(str(midi))

    pf = compute_pitch_feedback(midi, detected_key="G major")
    assert pf is not None
    assert pf.out_of_key_count == 1  # only Bb is out
    assert pf.detected_key == "G major"


def test_pitch_feedback_returns_none_without_key_or_chords(tmp_path: Path) -> None:
    midi = tmp_path / "p.mid"
    _write_simple_midi(midi, [0.0])
    pf = compute_pitch_feedback(midi)
    assert pf is None


# ---------------------------------------------------------------------------
# generation backends with mocked SDKs
# ---------------------------------------------------------------------------


def test_elevenlabs_unavailable_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "elevenlabs_api_key", "")
    assert ElevenLabsMusicBackend.is_available() is False


def test_elevenlabs_generate_with_mocked_compose(
    isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "elevenlabs_api_key", "sk-test")
    monkeypatch.setattr(ElevenLabsMusicBackend, "is_available", classmethod(lambda cls: True))

    def fake_compose(
        self: ElevenLabsMusicBackend,
        *,
        prompt: str,
        duration_sec: float,
        model_id: str,
        api_key: str,
    ) -> bytes:
        assert api_key == "sk-test"
        return b"FAKEAUDIO" * 100

    monkeypatch.setattr(ElevenLabsMusicBackend, "_compose", fake_compose)
    result = ElevenLabsMusicBackend().generate_audio(
        MusicGenerationRequest(prompt="medium swing in F", duration_sec=15.0)
    )
    assert result.backend == "elevenlabs_music"
    assert result.output_path.exists()
    assert result.output_path.read_bytes().startswith(b"FAKEAUDIO")
    assert result.model_name and "elevenlabs" in result.model_name


def test_magenta_rt_python_path(
    isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "jg_magenta_rt_cli", "")
    monkeypatch.setattr(MagentaRealtimeBackend, "is_available", classmethod(lambda cls: True))

    out: dict[str, Path] = {}

    def fake_run_python(
        self: MagentaRealtimeBackend,
        request: MusicGenerationRequest,
        output_path: Path,
    ) -> None:
        output_path.write_bytes(b"WAV-DATA")
        out["path"] = output_path

    monkeypatch.setattr(MagentaRealtimeBackend, "_run_python", fake_run_python)

    result = MagentaRealtimeBackend().generate_audio(
        MusicGenerationRequest(prompt="bossa nova", duration_sec=20.0)
    )
    assert result.output_path == out["path"]
    assert result.duration_sec == 20.0
    assert result.model_name == "magenta_rt (python)"


def test_magenta_rt_cli_path(
    isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "jg_magenta_rt_cli", "fake-magenta-rt-cli")
    monkeypatch.setattr(MagentaRealtimeBackend, "is_available", classmethod(lambda cls: True))
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/local/bin/{name}")

    async def fake_run_cli(
        self: MagentaRealtimeBackend,
        cli: str,
        request: MusicGenerationRequest,
        output_path: Path,
    ) -> tuple[int, str]:
        output_path.write_bytes(b"WAV")
        return 0, ""

    monkeypatch.setattr(MagentaRealtimeBackend, "_run_cli", fake_run_cli)
    result = MagentaRealtimeBackend().generate_audio(
        MusicGenerationRequest(prompt="walking bass blues", duration_sec=10.0)
    )
    assert result.model_name and result.model_name.startswith("magenta_rt cli")
    assert result.output_path.exists()


# ---------------------------------------------------------------------------
# agent tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_backing_track_tool(isolated_workspace: Path) -> None:
    r = register_all()
    session_dir = isolated_workspace / "sessions" / "t-p3"
    session_dir.mkdir(parents=True)
    tok = set_tool_context(ToolContext(session_id="t-p3"))
    try:
        out = await r.invoke(
            "build_backing_track",
            {
                "out_path": "bt.mid",
                "chord_changes": ["Cm7", "F7", "BbMaj7", "EbMaj7"],
                "tempo_bpm": 120,
                "bars_per_chord": 1,
                "key": "Bb major",
            },
        )
    finally:
        reset_tool_context(tok)
    assert out["chord_count"] == 4
    assert Path(out["midi_path"]).exists()


@pytest.mark.asyncio
async def test_generate_exercise_tool(isolated_workspace: Path) -> None:
    r = register_all()
    session_dir = isolated_workspace / "sessions" / "t-p3"
    session_dir.mkdir(parents=True)
    tok = set_tool_context(ToolContext(session_id="t-p3"))
    try:
        out = await r.invoke(
            "generate_exercise",
            {
                "kind": "scale",
                "out_path": "scale.musicxml",
                "tonic": "Bb",
                "mode": "major",
                "octaves": 1,
            },
        )
    finally:
        reset_tool_context(tok)
    assert "error" not in out
    assert Path(out["musicxml_path"]).exists()


@pytest.mark.asyncio
async def test_generate_music_tool_with_mocked_backend(
    isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = register_all()
    session_dir = isolated_workspace / "sessions" / "t-p3"
    session_dir.mkdir(parents=True)

    # Force the registry to hand us our fake backend.
    class _FakeGen:
        name = "fake"

        def generate_audio(
            self, request: MusicGenerationRequest
        ) -> Any:
            from jazz_guru.music.models import MusicGenerationResult

            target = Path(request.output_path) if request.output_path else session_dir / "g.mp3"
            target.write_bytes(b"AUDIO")
            return MusicGenerationResult(
                backend=self.name,
                output_path=target,
                duration_sec=request.duration_sec,
                model_name="fake/v1",
            )

    monkeypatch.setattr(
        "jazz_guru.actions.tools.music_workflows.get_generation_backend",
        lambda name=None: _FakeGen(),
    )

    tok = set_tool_context(ToolContext(session_id="t-p3"))
    try:
        out = await r.invoke(
            "generate_music",
            {"prompt": "swing trio", "out_path": "gen.mp3", "duration_sec": 5.0},
        )
    finally:
        reset_tool_context(tok)
    assert out["backend"] == "fake"
    assert Path(out["output_path"]).exists()


@pytest.mark.asyncio
async def test_generate_music_tool_when_disabled(
    isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = register_all()
    monkeypatch.setattr(
        "jazz_guru.actions.tools.music_workflows.get_generation_backend",
        lambda name=None: None,
    )
    tok = set_tool_context(ToolContext(session_id="t-p3"))
    session_dir = isolated_workspace / "sessions" / "t-p3"
    session_dir.mkdir(parents=True)
    try:
        out = await r.invoke("generate_music", {"prompt": "test"})
    finally:
        reset_tool_context(tok)
    assert "error" in out and "MUSIC_GENERATION_BACKEND" in out["error"]
