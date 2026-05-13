"""ElevenLabs Music backend (placeholder).

ElevenLabs Music is the recommended hosted generation backend. The
stub raises :class:`BackendUnavailableError` until the actual HTTP
client + credential wiring lands in Phase 3.
"""
from __future__ import annotations

from jazz_guru.music.interfaces import BaseBackend
from jazz_guru.music.models import MusicGenerationRequest, MusicGenerationResult


class ElevenLabsMusicBackend(BaseBackend):
    """Stub adapter. Always raises until Phase 3."""

    name: str = "elevenlabs_music"
    install_hint: str | None = (
        "pending — set ELEVENLABS_API_KEY and install the official SDK in Phase 3"
    )

    @classmethod
    def _probe(cls) -> None:
        import elevenlabs  # type: ignore[import-not-found]  # noqa: F401

    def generate_audio(self, request: MusicGenerationRequest) -> MusicGenerationResult:
        raise self._unavailable("ElevenLabs Music adapter is a Phase 3 stub")
