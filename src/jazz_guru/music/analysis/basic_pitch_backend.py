"""Basic Pitch transcription adapter (Spotify's monophonic/poly model).

Adapts the ``basic-pitch`` package's ``predict_and_save`` entry point to
the :class:`~jazz_guru.music.interfaces.TranscriptionBackend` protocol.

The dependency is **optional**. Import happens lazily inside
``transcribe_to_midi`` and on the ``_probe`` classmethod, so the rest of
the harness never crashes if Basic Pitch is not installed. If the
package is missing, calls raise :class:`BackendUnavailableError` with a
clear install hint.

Install (optional):

.. code-block:: bash

    pip install "basic-pitch>=0.4"

A first call may download a small ONNX model; subsequent calls reuse
the cache.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from jazz_guru.music.interfaces import BaseBackend
from jazz_guru.music.models import TranscriptionResult


class BasicPitchBackend(BaseBackend):
    """Audio → MIDI via Spotify Basic Pitch.

    Works well for monophonic / lightly-polyphonic acoustic recordings,
    which fits the "tenor sax practice take" use case the harness
    targets. For dense polyphony or non-pitched audio, prefer
    :class:`~jazz_guru.music.analysis.mt3_backend.MT3Backend` (Phase 2).
    """

    name: str = "basic_pitch"
    install_hint: str | None = "pip install 'basic-pitch>=0.4'"

    @classmethod
    def _probe(cls) -> None:
        # Both submodules are required: `inference` for the predictor and
        # `ICASSP_2022_MODEL_PATH` for the canonical checkpoint path.
        import basic_pitch.inference  # type: ignore[import-not-found]  # noqa: F401
        from basic_pitch import (
            ICASSP_2022_MODEL_PATH,  # type: ignore[import-not-found]  # noqa: F401
        )

    def _predict(self, audio_path: Path, output_dir: Path) -> dict[str, Any]:
        """Invoke Basic Pitch and return a small result dict.

        Kept as a method to make tests easy to stub: a test can subclass
        ``BasicPitchBackend`` and monkeypatch ``_predict`` to avoid the
        real model load.
        """
        try:
            from basic_pitch import ICASSP_2022_MODEL_PATH  # type: ignore[import-not-found]
            from basic_pitch.inference import predict_and_save  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - covered by _probe-based unavailability test
            raise self._unavailable(f"failed to import basic-pitch: {exc}") from exc

        output_dir.mkdir(parents=True, exist_ok=True)
        predict_and_save(
            audio_path_list=[str(audio_path)],
            output_directory=str(output_dir),
            save_midi=True,
            sonify_midi=False,
            save_model_outputs=False,
            save_notes=False,
            model_or_model_path=ICASSP_2022_MODEL_PATH,
        )
        # Basic Pitch names the file ``<stem>_basic_pitch.mid``.
        midi_path = output_dir / f"{audio_path.stem}_basic_pitch.mid"
        return {"midi_path": midi_path, "model": "basic-pitch ICASSP-2022"}

    def transcribe_to_midi(
        self, audio_path: Path, *, instrument: str | None = None
    ) -> TranscriptionResult:
        audio_path = Path(audio_path)
        if not audio_path.exists():
            return TranscriptionResult(
                backend=self.name,
                warnings=[f"audio file not found: {audio_path}"],
            )
        if not self.is_available():
            raise self._unavailable("basic-pitch is not installed")

        # Output sits next to the input by default so the orchestrator
        # can keep everything inside the session workspace.
        output_dir = audio_path.parent / "transcriptions"
        result = self._predict(audio_path, output_dir)
        midi_path: Path = result["midi_path"]

        note_count: int | None = None
        warnings: list[str] = []
        if midi_path.exists():
            try:
                from jazz_guru.music.notation.midi import midi_note_count

                note_count = midi_note_count(midi_path)
            except Exception as exc:  # pragma: no cover - mido is mandatory
                warnings.append(f"midi inspection failed: {exc}")
        else:
            warnings.append(f"basic-pitch did not produce expected midi at {midi_path}")
            midi_path = None  # type: ignore[assignment]

        if instrument:
            warnings.append(
                f"basic-pitch ignores per-instrument hints; '{instrument}' recorded for context only."
            )

        return TranscriptionResult(
            backend=self.name,
            midi_path=midi_path,
            note_count=note_count,
            model_name=result.get("model"),
            warnings=warnings,
        )
