"""Music Flamingo backend (placeholder).

Music Flamingo is the music-understanding LLM the architecture
recommends for free-text musical descriptions ("this is a medium-swing
ballad in E♭, the soloist mostly stays inside the chord tones..."). The
hosted/open weights story is still moving; Phase 2 will wire this up
either through Hugging Face ``transformers`` or a vendor API, gated by
:class:`~jazz_guru.music.errors.BackendUnavailableError` until then.
"""
from __future__ import annotations

from pathlib import Path

from jazz_guru.music.interfaces import BaseBackend
from jazz_guru.music.models import MusicAnalysis, MusicContext


class MusicFlamingoBackend(BaseBackend):
    """Stub adapter. Always raises until Phase 2."""

    name: str = "music_flamingo"
    install_hint: str | None = (
        "see https://github.com/nvidia/Audio-Flamingo for install / API access"
    )

    @classmethod
    def _probe(cls) -> None:
        # The official package layout is not stable yet; treat any import
        # failure as 'unavailable'.
        import audio_flamingo  # type: ignore[import-not-found]  # noqa: F401

    def analyze_audio(
        self, audio_path: Path, *, context: MusicContext | None = None
    ) -> MusicAnalysis:
        raise self._unavailable("Music Flamingo adapter is a Phase 2 stub")
