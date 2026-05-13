"""Magenta RealTime backend (placeholder).

Magenta RealTime is the recommended on-device generation backend. This
stub conforms to :class:`~jazz_guru.music.interfaces.MusicGenerationBackend`
so the rest of the layer can already reference it; the actual
implementation is Phase 3 work (likely wrapping the ``magenta-realtime``
CLI/SDK or a local model checkpoint).
"""
from __future__ import annotations

from jazz_guru.music.interfaces import BaseBackend
from jazz_guru.music.models import MusicGenerationRequest, MusicGenerationResult


class MagentaRealtimeBackend(BaseBackend):
    """Stub adapter. Always raises until Phase 3."""

    name: str = "magenta_rt"
    install_hint: str | None = "pending — Phase 3 wiring"

    @classmethod
    def _probe(cls) -> None:
        import magenta_realtime  # type: ignore[import-not-found]  # noqa: F401

    def generate_audio(self, request: MusicGenerationRequest) -> MusicGenerationResult:
        raise self._unavailable("Magenta RealTime adapter is a Phase 3 stub")
