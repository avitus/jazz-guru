"""Omnizart adapter.

Implements :class:`~jazz_guru.music.interfaces.ChordAnalysisBackend` and
:class:`~jazz_guru.music.interfaces.BeatTrackingBackend` against the
official ``omnizart`` package. The two transcribers it wraps are
``omnizart.chord.app.ChordTranscription`` (chord-symbol ASR) and
``omnizart.beat.app.BeatTranscription`` (beats + downbeats).

Optional dependency. Both the package and its checkpoints are large
(TensorFlow-based + multi-hundred-MB model files). The adapter
lazy-imports the package, and after install the user must run
``omnizart download-checkpoints`` to populate the checkpoint dirs —
absent checkpoints surface as a clean warning on the returned result
rather than crashing the harness.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from jazz_guru.music.interfaces import BaseBackend
from jazz_guru.music.models import BeatTrackingResult, ChordAnalysisResult, ChordEvent


class OmnizartBackend(BaseBackend):
    """Omnizart-backed chord + beat ASR."""

    name: str = "omnizart"
    install_hint: str | None = (
        "pip install omnizart  &&  omnizart download-checkpoints"
    )

    @classmethod
    def _probe(cls) -> None:
        # All three imports must succeed: the top-level package, plus the
        # two task-level apps we drive. Probing only the top-level package
        # would let a partially-installed wheel masquerade as available.
        import omnizart  # type: ignore[import-not-found]  # noqa: F401
        from omnizart.beat.app import (
            BeatTranscription,  # type: ignore[import-not-found]  # noqa: F401
        )
        from omnizart.chord.app import (
            ChordTranscription,  # type: ignore[import-not-found]  # noqa: F401
        )

    # ------------------------------------------------------------------
    # transcribe entry points (subclass / monkeypatch in tests)
    # ------------------------------------------------------------------

    def _transcribe_chords(self, audio_path: Path, output_dir: Path) -> None:
        from omnizart.chord.app import ChordTranscription  # type: ignore[import-not-found]

        ChordTranscription().transcribe(str(audio_path), output=str(output_dir))

    def _transcribe_beats(self, audio_path: Path, output_dir: Path) -> None:
        from omnizart.beat.app import BeatTranscription  # type: ignore[import-not-found]

        BeatTranscription().transcribe(str(audio_path), output=str(output_dir))

    # ------------------------------------------------------------------
    # CSV parsers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_chord_csv(path: Path) -> list[ChordEvent]:
        """Read omnizart's chord CSV (``chord, start, end``).

        Tolerates an optional header row and trailing whitespace. Rows
        that cannot be parsed are skipped — partial output is more
        useful than no output.
        """
        events: list[ChordEvent] = []
        if not path.exists():
            return events
        with path.open("r", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            for row in reader:
                if len(row) < 3:
                    continue
                chord_label = row[0].strip()
                if not chord_label or chord_label.lower() == "chord":
                    continue
                try:
                    start = float(row[1])
                    end = float(row[2])
                except ValueError:
                    continue
                events.append(ChordEvent(chord=chord_label, start_sec=start, end_sec=end))
        return events

    @staticmethod
    def _parse_beat_csv(path: Path) -> list[float]:
        """Read a single-column timestamps file."""
        out: list[float] = []
        if not path.exists():
            return out
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.lower() in {"time", "beat", "downbeat"}:
                continue
            try:
                out.append(float(line))
            except ValueError:
                continue
        return out

    # ------------------------------------------------------------------
    # public protocol methods
    # ------------------------------------------------------------------

    def analyze_chords(self, audio_path: Path) -> ChordAnalysisResult:
        audio_path = Path(audio_path)
        if not audio_path.exists():
            return ChordAnalysisResult(
                backend=self.name, warnings=[f"audio file not found: {audio_path}"]
            )
        if not self.is_available():
            raise self._unavailable("omnizart is not installed")

        output_dir = audio_path.parent / "omnizart"
        output_dir.mkdir(parents=True, exist_ok=True)
        warnings: list[str] = []
        try:
            self._transcribe_chords(audio_path, output_dir)
        except Exception as exc:  # pragma: no cover - depends on optional dep
            return ChordAnalysisResult(
                backend=self.name,
                warnings=[
                    "omnizart chord transcription failed "
                    f"(missing checkpoints? run `omnizart download-checkpoints`): {exc}"
                ],
            )

        csv_path = output_dir / f"{audio_path.stem}.csv"
        chords = self._parse_chord_csv(csv_path)
        if not chords:
            warnings.append(
                f"omnizart produced no parseable chord events at {csv_path}"
            )

        return ChordAnalysisResult(
            backend=self.name,
            chords=chords,
            warnings=warnings,
        )

    def track_beats(self, audio_path: Path) -> BeatTrackingResult:
        audio_path = Path(audio_path)
        if not audio_path.exists():
            return BeatTrackingResult(
                backend=self.name, warnings=[f"audio file not found: {audio_path}"]
            )
        if not self.is_available():
            raise self._unavailable("omnizart is not installed")

        output_dir = audio_path.parent / "omnizart"
        output_dir.mkdir(parents=True, exist_ok=True)
        warnings: list[str] = []
        try:
            self._transcribe_beats(audio_path, output_dir)
        except Exception as exc:  # pragma: no cover - depends on optional dep
            return BeatTrackingResult(
                backend=self.name,
                warnings=[
                    "omnizart beat transcription failed "
                    f"(missing checkpoints? run `omnizart download-checkpoints`): {exc}"
                ],
            )

        beat_csv = output_dir / f"{audio_path.stem}_beat.csv"
        downbeat_csv = output_dir / f"{audio_path.stem}_down_beat.csv"
        beats = self._parse_beat_csv(beat_csv)
        downbeats = self._parse_beat_csv(downbeat_csv)

        tempo_bpm: float | None = None
        if len(beats) >= 2:
            # Use the median inter-onset interval; robust against a few
            # missed beats at the start/end. 60 / IOI gives BPM.
            iois = sorted(
                beats[i + 1] - beats[i] for i in range(len(beats) - 1) if beats[i + 1] > beats[i]
            )
            if iois:
                median = iois[len(iois) // 2]
                if median > 0:
                    tempo_bpm = 60.0 / median

        return BeatTrackingResult(
            backend=self.name,
            tempo_bpm=tempo_bpm,
            beats_sec=beats,
            downbeats_sec=downbeats,
            warnings=warnings,
        )

    # The librosa baseline exposes ``analyze_audio`` so the orchestrator
    # can populate ``MusicAnalysis`` from a beat-tracking backend as a
    # fallback. Provide the same shape here so a user who picks
    # ``MUSIC_ANALYSIS_BACKEND=omnizart`` still gets tempo into the
    # ``MusicAnalysis`` slot.
    def analyze_audio(self, audio_path: Path, *, context: Any = None) -> Any:
        from jazz_guru.music.models import MusicAnalysis

        beats = self.track_beats(audio_path)
        warnings = list(beats.warnings)
        return MusicAnalysis(
            backend=self.name,
            tempo_bpm=beats.tempo_bpm,
            warnings=warnings,
        )
