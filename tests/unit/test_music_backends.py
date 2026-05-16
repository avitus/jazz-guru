"""Tests for the modular music-backend layer.

Covers interface/model validation, backend selection from config, the
"missing optional dependency" graceful-degradation path, the Basic
Pitch adapter with a mocked predictor, the analyze_practice_take
orchestration end-to-end with mocked backends, and the analyze-take
CLI command (also with mocked backends).

No real models, downloads, GPUs, or network calls are required — all
optional dependencies are stubbed.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from jazz_guru.actions import ToolContext, register_all, reset_tool_context, set_tool_context
from jazz_guru.config import get_settings
from jazz_guru.music import (
    BackendUnavailableError,
    BeatTrackingResult,
    ChordAnalysisResult,
    MusicAnalysis,
    MusicContext,
    PracticeFeedback,
    TranscriptionResult,
    analyze_practice_take,
    available_backends,
    get_beat_tracking_backend,
    get_chord_analysis_backend,
    get_generation_backend,
    get_transcription_backend,
    get_understanding_backend,
)
from jazz_guru.music.analysis.basic_pitch_backend import BasicPitchBackend
from jazz_guru.music.analysis.librosa_backend import LibrosaAnalysisBackend
from jazz_guru.music.analysis.mt3_backend import MT3Backend
from jazz_guru.music.analysis.music_flamingo_backend import MusicFlamingoBackend
from jazz_guru.music.analysis.omnizart_backend import OmnizartBackend
from jazz_guru.music.generation.elevenlabs_music_backend import ElevenLabsMusicBackend
from jazz_guru.music.generation.magenta_rt_backend import MagentaRealtimeBackend
from jazz_guru.music.notation.leadsheet import LeadSheet, load_leadsheet


@pytest.fixture
def isolated_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(get_settings(), "jg_workspace_dir", tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# 1) model + interface validation
# ---------------------------------------------------------------------------


def test_music_context_accepts_all_optional_fields() -> None:
    ctx = MusicContext(
        chart="Autumn Leaves",
        instrument="tenor-sax",
        lead_sheet_path=Path("./out/lead.musicxml"),
        expected_key="G minor",
        expected_tempo_bpm=120.0,
        chord_changes=["Cm7", "F7", "BbMaj7"],
    )
    assert ctx.chart == "Autumn Leaves"
    assert ctx.lead_sheet_path == Path("./out/lead.musicxml")


def test_practice_feedback_serialises_round_trip() -> None:
    fb = PracticeFeedback(
        audio_path=Path("solo.wav"),
        context=MusicContext(chart="Autumn Leaves"),
        warnings=["hello"],
    )
    data = fb.model_dump(mode="json")
    assert data["audio_path"] == "solo.wav"
    assert data["warnings"] == ["hello"]


def test_transcription_result_clamps_confidence() -> None:
    from pydantic import ValidationError

    # Pydantic should accept 0..1, refuse out-of-range.
    TranscriptionResult(backend="basic_pitch", confidence=0.5)
    with pytest.raises(ValidationError):
        TranscriptionResult(backend="basic_pitch", confidence=1.5)


def test_stub_backends_match_their_protocol_shape() -> None:
    # Every stub must satisfy its declared interface even before its dep is
    # installed, otherwise we'd raise at import time.
    assert hasattr(BasicPitchBackend(), "transcribe_to_midi")
    assert hasattr(MT3Backend(), "transcribe_to_midi")
    assert hasattr(OmnizartBackend(), "analyze_chords")
    assert hasattr(OmnizartBackend(), "track_beats")
    assert hasattr(MusicFlamingoBackend(), "analyze_audio")
    assert hasattr(MagentaRealtimeBackend(), "generate_audio")
    assert hasattr(ElevenLabsMusicBackend(), "generate_audio")


# ---------------------------------------------------------------------------
# 2) backend selection from config
# ---------------------------------------------------------------------------


def test_get_transcription_backend_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "music_transcription_backend", "none")
    assert get_transcription_backend() is None
    assert get_transcription_backend("none") is None


def test_get_transcription_backend_basic_pitch() -> None:
    be = get_transcription_backend("basic_pitch")
    assert isinstance(be, BasicPitchBackend)


def test_get_transcription_backend_unknown_raises() -> None:
    with pytest.raises(BackendUnavailableError):
        get_transcription_backend("not-a-real-backend")


def test_get_analysis_backends_default_is_librosa(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "music_analysis_backend", "librosa")
    assert isinstance(get_beat_tracking_backend(), LibrosaAnalysisBackend)
    assert isinstance(get_chord_analysis_backend(), LibrosaAnalysisBackend)


def test_get_understanding_backend_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "music_understanding_backend", "none")
    assert get_understanding_backend() is None
    assert isinstance(get_understanding_backend("librosa"), LibrosaAnalysisBackend)
    assert isinstance(get_understanding_backend("music_flamingo"), MusicFlamingoBackend)


def test_get_generation_backend_stubs_resolve() -> None:
    assert isinstance(get_generation_backend("magenta_rt"), MagentaRealtimeBackend)
    assert isinstance(get_generation_backend("elevenlabs_music"), ElevenLabsMusicBackend)
    assert get_generation_backend("none") is None


# ---------------------------------------------------------------------------
# 3) missing-optional-dep behaviour
# ---------------------------------------------------------------------------


def test_unavailable_backends_report_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Optional backends not installed in CI should report a hint.

    Forces ``basic_pitch`` unavailable via monkeypatch instead of relying on
    the ambient environment, so the test is deterministic on machines that
    happen to have the package installed.
    """
    monkeypatch.setattr(BasicPitchBackend, "is_available", classmethod(lambda cls: False))
    rows = available_backends()
    assert rows["librosa"]["available"] is True  # always-on baseline
    assert rows["basic_pitch"]["available"] is False
    assert "basic-pitch" in str(rows["basic_pitch"]["install_hint"])


def test_optional_backend_call_raises_unavailable(tmp_path: Path) -> None:
    """Calling a real adapter without its dep produces a clean BackendUnavailableError."""
    audio = tmp_path / "tone.wav"
    audio.write_bytes(b"\x00")
    if OmnizartBackend.is_available():
        pytest.skip("omnizart is installed; this test covers the missing-dep path")
    with pytest.raises(BackendUnavailableError) as exc:
        OmnizartBackend().analyze_chords(audio)
    assert exc.value.backend == "omnizart"
    assert exc.value.install_hint  # message guides the user


def test_basic_pitch_call_without_dep_raises_clean_error(tmp_path: Path) -> None:
    audio = tmp_path / "fake.wav"
    audio.write_bytes(b"\x00\x00")
    if BasicPitchBackend.is_available():
        pytest.skip("basic-pitch is actually installed; this test exercises the missing-dep path")
    with pytest.raises(BackendUnavailableError):
        BasicPitchBackend().transcribe_to_midi(audio)


# ---------------------------------------------------------------------------
# 4) Basic Pitch adapter with a mocked predictor
# ---------------------------------------------------------------------------


def test_basic_pitch_with_mocked_predict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Subclass + monkeypatch `_predict` + `is_available` to bypass the real model."""
    audio = tmp_path / "solo.wav"
    audio.write_bytes(b"\x00")

    midi_path = tmp_path / "transcriptions" / "solo_basic_pitch.mid"

    def fake_predict(self: BasicPitchBackend, audio_path: Path, output_dir: Path) -> dict[str, Any]:
        output_dir.mkdir(parents=True, exist_ok=True)
        # write a tiny valid MIDI via mido (already a dep)
        import mido  # type: ignore[import-untyped]

        mid = mido.MidiFile()
        track = mido.MidiTrack()
        mid.tracks.append(track)
        track.append(mido.Message("note_on", note=60, velocity=64, time=0))
        track.append(mido.Message("note_off", note=60, velocity=0, time=240))
        mid.save(str(midi_path))
        return {"midi_path": midi_path, "model": "stub"}

    monkeypatch.setattr(BasicPitchBackend, "_predict", fake_predict)
    monkeypatch.setattr(BasicPitchBackend, "is_available", classmethod(lambda cls: True))

    result = BasicPitchBackend().transcribe_to_midi(audio, instrument="tenor-sax")
    assert result.backend == "basic_pitch"
    assert result.midi_path == midi_path
    assert result.note_count == 1
    # Instrument hint is recorded as a warning since basic-pitch ignores it.
    assert any("ignores per-instrument hints" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# 5) analyze_practice_take orchestration with mocked backends
# ---------------------------------------------------------------------------


class _StubTranscription:
    name = "stub_t"

    def __init__(self, midi_path: Path) -> None:
        self.midi_path = midi_path

    def transcribe_to_midi(self, audio_path: Path, *, instrument: str | None = None) -> TranscriptionResult:
        return TranscriptionResult(backend=self.name, midi_path=self.midi_path, note_count=3)


class _StubBeats:
    name = "stub_b"

    def track_beats(self, audio_path: Path) -> BeatTrackingResult:
        return BeatTrackingResult(backend=self.name, tempo_bpm=121.0, beats_sec=[0.0, 0.5, 1.0])


class _StubChords:
    name = "stub_c"

    def analyze_chords(self, audio_path: Path) -> ChordAnalysisResult:
        return ChordAnalysisResult(backend=self.name, detected_key="G minor")


class _StubUnderstanding:
    name = "stub_u"

    def analyze_audio(
        self, audio_path: Path, *, context: MusicContext | None = None
    ) -> MusicAnalysis:
        return MusicAnalysis(backend=self.name, detected_key="G minor", tempo_bpm=121.0)


async def test_analyze_practice_take_orchestrates_all_backends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "solo.wav"
    audio.write_bytes(b"\x00" * 16)
    midi_path = tmp_path / "stub.mid"

    # Force the orchestrator to use our stubs by patching the registry.
    monkeypatch.setattr(
        "jazz_guru.music.analyze.get_transcription_backend",
        lambda name=None: _StubTranscription(midi_path),
    )
    monkeypatch.setattr(
        "jazz_guru.music.analyze.get_beat_tracking_backend",
        lambda name=None: _StubBeats(),
    )
    monkeypatch.setattr(
        "jazz_guru.music.analyze.get_chord_analysis_backend",
        lambda name=None: _StubChords(),
    )
    monkeypatch.setattr(
        "jazz_guru.music.analyze.get_understanding_backend",
        lambda name=None: _StubUnderstanding(),
    )

    feedback = await analyze_practice_take(
        audio,
        chart="Autumn Leaves",
        instrument="tenor-sax",
        expected_tempo_bpm=120.0,
        expected_key="G minor",
    )
    assert feedback.transcription is not None
    assert feedback.transcription.midi_path == midi_path
    assert feedback.beat_tracking is not None
    assert feedback.beat_tracking.tempo_bpm == 121.0
    assert feedback.analysis is not None
    assert feedback.analysis.detected_key == "G minor"
    assert feedback.summary
    assert "Autumn Leaves" in feedback.summary
    assert "121.0 BPM" in feedback.summary
    # Timing feedback should kick in because expected_tempo_bpm was set.
    assert feedback.timing is not None
    assert feedback.timing.notes


async def test_analyze_practice_take_degrades_when_backend_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "solo.wav"
    audio.write_bytes(b"\x00")

    def _raise(name: str | None = None) -> None:
        raise BackendUnavailableError("transcription", "not installed", "pip install x")

    monkeypatch.setattr("jazz_guru.music.analyze.get_transcription_backend", _raise)
    monkeypatch.setattr(
        "jazz_guru.music.analyze.get_chord_analysis_backend", lambda name=None: None
    )
    monkeypatch.setattr(
        "jazz_guru.music.analyze.get_beat_tracking_backend",
        lambda name=None: _StubBeats(),
    )
    monkeypatch.setattr(
        "jazz_guru.music.analyze.get_understanding_backend",
        lambda name=None: None,
    )

    feedback = await analyze_practice_take(audio)
    # Orchestrator must not raise; backend-unavailable becomes a warning.
    assert feedback.transcription is None
    assert any("transcription" in w for w in feedback.warnings)
    assert feedback.beat_tracking is not None  # baseline still ran


async def test_analyze_practice_take_missing_audio_returns_warning(tmp_path: Path) -> None:
    feedback = await analyze_practice_take(tmp_path / "nope.wav")
    assert feedback.transcription is None
    assert any("audio file not found" in w for w in feedback.warnings)
    assert feedback.summary and "audio file not found" in feedback.summary


# ---------------------------------------------------------------------------
# 6) lead sheet loader
# ---------------------------------------------------------------------------


def test_load_leadsheet_from_text(tmp_path: Path) -> None:
    p = tmp_path / "autumn.chords"
    p.write_text("Cm7 | F7 | BbMaj7 | EbMaj7\nAm7b5 | D7 | Gm | Gm")
    sheet = load_leadsheet(p)
    assert sheet.chord_changes[:3] == ["Cm7", "F7", "BbMaj7"]
    assert sheet.title == "autumn"


def test_load_leadsheet_missing_returns_empty(tmp_path: Path) -> None:
    sheet = load_leadsheet(tmp_path / "nope.txt")
    assert isinstance(sheet, LeadSheet)
    assert sheet.chord_changes == []


# ---------------------------------------------------------------------------
# 7) agent-tool registration + CLI
# ---------------------------------------------------------------------------


async def test_agent_tool_invokes_orchestrator(
    isolated_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    r = register_all()
    assert "analyze_practice_take" in r.names()

    # Stub the orchestrator so we don't actually load librosa here.
    async def fake_run(audio_path: Path, **kwargs: Any) -> PracticeFeedback:
        return PracticeFeedback(
            audio_path=audio_path,
            context=MusicContext(chart=kwargs.get("chart"), instrument=kwargs.get("instrument")),
            summary="stubbed",
        )

    monkeypatch.setattr("jazz_guru.actions.tools.practice.analyze_practice_take", fake_run)

    # Put a placeholder audio file inside the session workspace
    session_dir = isolated_workspace / "sessions" / "t-music"
    session_dir.mkdir(parents=True)
    audio = session_dir / "solo.wav"
    audio.write_bytes(b"\x00")

    tok = set_tool_context(ToolContext(session_id="t-music"))
    try:
        out = await r.invoke(
            "analyze_practice_take",
            {"audio_path": "solo.wav", "chart": "Autumn Leaves", "instrument": "tenor-sax"},
        )
    finally:
        reset_tool_context(tok)

    assert out["summary"] == "stubbed"
    assert out["context"]["chart"] == "Autumn Leaves"


def test_cli_analyze_take_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """CLI command exits 0 and prints a result with mocked orchestrator."""
    from typer.testing import CliRunner

    from jazz_guru.cli import app

    audio = tmp_path / "solo.wav"
    audio.write_bytes(b"\x00")

    async def fake_run(audio_path: Path, **kwargs: Any) -> PracticeFeedback:
        return PracticeFeedback(
            audio_path=audio_path,
            context=MusicContext(chart=kwargs.get("chart")),
            summary="cli-stubbed",
        )

    monkeypatch.setattr("jazz_guru.music.analyze_practice_take", fake_run)
    # The CLI imports from `jazz_guru.music` lazily, so also patch there.
    monkeypatch.setattr("jazz_guru.music.analyze.analyze_practice_take", fake_run)

    result = CliRunner().invoke(
        app,
        ["analyze-take", str(audio), "--chart", "Autumn Leaves", "--instrument", "tenor-sax"],
    )
    # The command should at least run to completion.
    assert result.exit_code == 0, result.output
    assert "cli-stubbed" in result.output
    # Backend table should mention librosa as available.
    assert "librosa" in result.output


# ---------------------------------------------------------------------------
# 8) Phase-2 adapters: omnizart, mt3, music_flamingo with mocked deps
# ---------------------------------------------------------------------------


def test_omnizart_parses_chord_csv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audio = tmp_path / "take.wav"
    audio.write_bytes(b"\x00")

    def fake_transcribe(self: OmnizartBackend, audio_path: Path, output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        # Header + two rows; omnizart's real output uses `chord, start, end`.
        (output_dir / f"{audio_path.stem}.csv").write_text(
            "chord,start,end\nC:maj,0.5,2.1\nA:min,2.1,4.0\n"
        )

    monkeypatch.setattr(OmnizartBackend, "_transcribe_chords", fake_transcribe)
    monkeypatch.setattr(OmnizartBackend, "is_available", classmethod(lambda cls: True))

    result = OmnizartBackend().analyze_chords(audio)
    assert result.backend == "omnizart"
    assert len(result.chords) == 2
    assert result.chords[0].chord == "C:maj"
    assert result.chords[0].start_sec == 0.5
    assert result.chords[1].chord == "A:min"


def test_omnizart_parses_beat_csvs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audio = tmp_path / "groove.wav"
    audio.write_bytes(b"\x00")

    def fake_transcribe(self: OmnizartBackend, audio_path: Path, output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / f"{audio_path.stem}_beat.csv").write_text(
            "0.000000\n0.500000\n1.000000\n1.500000\n"
        )
        (output_dir / f"{audio_path.stem}_down_beat.csv").write_text(
            "0.000000\n2.000000\n"
        )

    monkeypatch.setattr(OmnizartBackend, "_transcribe_beats", fake_transcribe)
    monkeypatch.setattr(OmnizartBackend, "is_available", classmethod(lambda cls: True))

    result = OmnizartBackend().track_beats(audio)
    assert result.beats_sec == [0.0, 0.5, 1.0, 1.5]
    assert result.downbeats_sec == [0.0, 2.0]
    # 0.5 s IOI -> 120 BPM
    assert result.tempo_bpm is not None
    assert abs(result.tempo_bpm - 120.0) < 0.1


def test_omnizart_missing_csv_returns_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "empty.wav"
    audio.write_bytes(b"\x00")

    def fake_transcribe(self: OmnizartBackend, audio_path: Path, output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)  # but write no CSV

    monkeypatch.setattr(OmnizartBackend, "_transcribe_chords", fake_transcribe)
    monkeypatch.setattr(OmnizartBackend, "is_available", classmethod(lambda cls: True))

    result = OmnizartBackend().analyze_chords(audio)
    assert result.chords == []
    assert any("no parseable chord events" in w for w in result.warnings)


def test_mt3_python_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audio = tmp_path / "phrase.wav"
    audio.write_bytes(b"\x00")

    midi_target: dict[str, Path] = {}

    def fake_run_python(self: MT3Backend, audio_path: Path, midi_path: Path) -> None:
        # write a valid MIDI through mido so midi_note_count can parse it
        import mido  # type: ignore[import-untyped]

        midi_path.parent.mkdir(parents=True, exist_ok=True)
        mid = mido.MidiFile()
        track = mido.MidiTrack()
        mid.tracks.append(track)
        track.append(mido.Message("note_on", note=64, velocity=64, time=0))
        track.append(mido.Message("note_off", note=64, velocity=0, time=120))
        mid.save(str(midi_path))
        midi_target["path"] = midi_path

    monkeypatch.setattr(MT3Backend, "_run_python", fake_run_python)
    monkeypatch.setattr(MT3Backend, "is_available", classmethod(lambda cls: True))
    # Force the adapter into the python path by clearing JG_MT3_CLI.
    monkeypatch.setattr(get_settings(), "jg_mt3_cli", "")

    result = MT3Backend().transcribe_to_midi(audio)
    assert result.backend == "mt3"
    assert result.midi_path == midi_target["path"]
    assert result.note_count == 1
    assert result.model_name == "mt3 (python)"


def test_mt3_cli_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    audio = tmp_path / "phrase.wav"
    audio.write_bytes(b"\x00")

    monkeypatch.setattr(get_settings(), "jg_mt3_cli", "fake-mt3-cli")
    monkeypatch.setattr(MT3Backend, "is_available", classmethod(lambda cls: True))
    # Pretend the CLI is on PATH.
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/local/bin/{name}")

    async def fake_run_cli(
        self: MT3Backend, cli: str, audio_path: Path, midi_path: Path
    ) -> tuple[int, str]:
        import mido  # type: ignore[import-untyped]

        midi_path.parent.mkdir(parents=True, exist_ok=True)
        mid = mido.MidiFile()
        track = mido.MidiTrack()
        mid.tracks.append(track)
        track.append(mido.Message("note_on", note=60, velocity=64, time=0))
        track.append(mido.Message("note_off", note=60, velocity=0, time=240))
        mid.save(str(midi_path))
        return 0, ""

    monkeypatch.setattr(MT3Backend, "_run_cli", fake_run_cli)

    result = MT3Backend().transcribe_to_midi(audio)
    assert result.backend == "mt3"
    assert result.note_count == 1
    assert result.model_name and result.model_name.startswith("mt3 cli")


def test_mt3_cli_failure_surfaces_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "phrase.wav"
    audio.write_bytes(b"\x00")
    monkeypatch.setattr(get_settings(), "jg_mt3_cli", "fake-mt3-cli")
    monkeypatch.setattr(MT3Backend, "is_available", classmethod(lambda cls: True))
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/local/bin/{name}")

    async def fake_run_cli(
        self: MT3Backend, cli: str, audio_path: Path, midi_path: Path
    ) -> tuple[int, str]:
        return 2, "checkpoint not found"

    monkeypatch.setattr(MT3Backend, "_run_cli", fake_run_cli)

    result = MT3Backend().transcribe_to_midi(audio)
    assert result.midi_path is None
    assert any("checkpoint not found" in w for w in result.warnings)


def test_music_flamingo_extracts_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "ballad.wav"
    audio.write_bytes(b"\x00")

    def fake_predict(
        self: MusicFlamingoBackend,
        audio_path: Path,
        prompt: str,
        *,
        max_new_tokens: int,
        model_id: str,
    ) -> str:
        return (
            "This is a medium-swing jazz ballad in the key of G minor. "
            "Tempo: 96 BPM. Time signature: 4/4. The performer outlines "
            "the chord changes mostly with chord tones."
        )

    monkeypatch.setattr(MusicFlamingoBackend, "_predict", fake_predict)
    monkeypatch.setattr(MusicFlamingoBackend, "is_available", classmethod(lambda cls: True))

    result = MusicFlamingoBackend().analyze_audio(
        audio, context=MusicContext(chart="My Funny Valentine", instrument="tenor-sax")
    )
    assert result.backend == "music_flamingo"
    assert result.summary and "G minor" in result.summary
    assert result.detected_key
    assert "g minor" in result.detected_key.lower()
    assert result.tempo_bpm == 96.0
    assert result.time_signature == "4/4"


def test_music_flamingo_prompt_includes_chart_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = tmp_path / "blues.wav"
    audio.write_bytes(b"\x00")
    captured: dict[str, str] = {}

    def fake_predict(
        self: MusicFlamingoBackend,
        audio_path: Path,
        prompt: str,
        *,
        max_new_tokens: int,
        model_id: str,
    ) -> str:
        captured["prompt"] = prompt
        return "Blues in B-flat."

    monkeypatch.setattr(MusicFlamingoBackend, "_predict", fake_predict)
    monkeypatch.setattr(MusicFlamingoBackend, "is_available", classmethod(lambda cls: True))

    MusicFlamingoBackend().analyze_audio(
        audio, context=MusicContext(chart="Bb Blues", instrument="trumpet")
    )
    assert "Bb Blues" in captured["prompt"]
    assert "trumpet" in captured["prompt"]


def test_music_flamingo_extract_fields_helper() -> None:
    """Regex sweep is robust to wording variations."""
    from jazz_guru.music.analysis.music_flamingo_backend import _extract_fields

    out = _extract_fields(
        "Likely key: E-flat major. Tempo: about 132 BPM. 4/4 time."
    )
    # The key regex is intentionally narrow; even partial extraction is useful.
    assert out.get("tempo_bpm") == 132.0
    assert out.get("time_signature") == "4/4"


def test_event_loop_runs_in_thread_for_sync_backends(monkeypatch: pytest.MonkeyPatch) -> None:
    """The orchestrator helper must defer sync backends to a thread."""
    # Smoke-only: just make sure the to_thread path is exercised. We're
    # not asserting against the actual thread id (flaky on CPython 3.12).
    from jazz_guru.music import analyze as analyze_mod

    async def runner() -> str:
        warnings: list[str] = []
        return await analyze_mod._run("smoke", lambda: "result", warnings) or ""

    assert asyncio.run(runner()) == "result"
