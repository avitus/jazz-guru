"""MT3 backend (placeholder).

MT3 (Magenta's "Multi-Task Multitrack Music Transcription") is a strong
general-purpose transcriber but ships as TF/JAX code with a non-trivial
install. Phase 2 will wrap it via ``transformers`` or a shelled-out CLI;
for now this is a stub that conforms to
:class:`~jazz_guru.music.interfaces.TranscriptionBackend`.
"""
from __future__ import annotations

from pathlib import Path

from jazz_guru.music.interfaces import BaseBackend
from jazz_guru.music.models import TranscriptionResult


class MT3Backend(BaseBackend):
    """Stub adapter. Always raises until Phase 2."""

    name: str = "mt3"
    install_hint: str | None = "see https://github.com/magenta/mt3 for the JAX/T5X install"

    @classmethod
    def _probe(cls) -> None:
        import mt3  # type: ignore[import-not-found]  # noqa: F401

    def transcribe_to_midi(
        self, audio_path: Path, *, instrument: str | None = None
    ) -> TranscriptionResult:
        raise self._unavailable("MT3 adapter is a Phase 2 stub")
