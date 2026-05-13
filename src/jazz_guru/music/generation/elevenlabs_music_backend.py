"""ElevenLabs Music adapter.

Hosted music generation via the ElevenLabs API (the ``music_v1``
endpoint, exposed through the official ``elevenlabs`` Python SDK).
Requires ``ELEVENLABS_API_KEY`` to be set; the model id is configurable
through ``ELEVENLABS_MUSIC_MODEL`` (default ``music_v1``).

The dependency is **optional**. The ``elevenlabs`` package is imported
lazily inside :meth:`_compose`, so the harness keeps running if you
have not opted into the hosted backend.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from jazz_guru.config import get_settings
from jazz_guru.music.interfaces import BaseBackend
from jazz_guru.music.models import MusicGenerationRequest, MusicGenerationResult


class ElevenLabsMusicBackend(BaseBackend):
    """ElevenLabs Music API adapter."""

    name: str = "elevenlabs_music"
    install_hint: str | None = (
        "pip install elevenlabs && set ELEVENLABS_API_KEY (and optionally "
        "ELEVENLABS_MUSIC_MODEL) in .env"
    )

    @classmethod
    def _probe(cls) -> None:
        import elevenlabs  # type: ignore[import-untyped]  # noqa: F401

    @classmethod
    def is_available(cls) -> bool:  # override base
        try:
            cls._probe()
        except Exception:
            return False
        # The SDK can be installed without a key, but the backend isn't
        # usable until both are present — treat a missing key as unavailable
        # so the orchestrator's warning is concrete.
        return bool(get_settings().elevenlabs_api_key)

    # ------------------------------------------------------------------
    # SDK call (monkeypatched in tests)
    # ------------------------------------------------------------------

    def _compose(
        self,
        *,
        prompt: str,
        duration_sec: float,
        model_id: str,
        api_key: str,
    ) -> bytes:
        """Call the ElevenLabs music endpoint and return audio bytes.

        Tries a couple of method names because the SDK has been iterating
        on its music API surface. Tests stub this method to avoid the
        real HTTP call.
        """
        from elevenlabs.client import ElevenLabs  # type: ignore[import-untyped]

        client = ElevenLabs(api_key=api_key)
        music = client.music

        kwargs: dict[str, Any] = {
            "prompt": prompt,
            "music_length_ms": int(duration_sec * 1000),
            "model_id": model_id,
        }

        for method_name in ("compose", "generate", "create"):
            method = getattr(music, method_name, None)
            if method is None:
                continue
            result = method(**kwargs)
            # The SDK returns either bytes or an iterator of byte chunks
            # depending on version. Normalise both.
            if isinstance(result, bytes | bytearray):
                return bytes(result)
            if hasattr(result, "__iter__"):
                return b"".join(chunk for chunk in result)
            return bytes(result)

        raise self._unavailable(
            "elevenlabs SDK exposes no compose/generate/create on client.music; "
            "upgrade `elevenlabs` >= 1.5 or set ELEVENLABS_MUSIC_MODEL to a "
            "supported value"
        )

    # ------------------------------------------------------------------
    # public protocol method
    # ------------------------------------------------------------------

    def _default_output(self) -> Path:
        s = get_settings()
        ts = int(time.time() * 1000)
        out_dir = s.jg_workspace_dir / "generation"
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir / f"elevenlabs_music_{ts}.mp3"

    def generate_audio(self, request: MusicGenerationRequest) -> MusicGenerationResult:
        settings = get_settings()
        if not settings.elevenlabs_api_key:
            raise self._unavailable("ELEVENLABS_API_KEY is not set")
        if not self.is_available():
            raise self._unavailable("elevenlabs SDK is not installed")

        output_path = Path(request.output_path) if request.output_path else self._default_output()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        warnings: list[str] = []

        try:
            audio = self._compose(
                prompt=request.prompt,
                duration_sec=request.duration_sec,
                model_id=settings.elevenlabs_music_model,
                api_key=settings.elevenlabs_api_key,
            )
        except Exception as exc:  # pragma: no cover - depends on optional dep
            return MusicGenerationResult(
                backend=self.name,
                output_path=output_path,
                duration_sec=0.0,
                warnings=[f"elevenlabs compose failed: {exc}"],
            )

        output_path.write_bytes(audio)
        return MusicGenerationResult(
            backend=self.name,
            output_path=output_path,
            duration_sec=request.duration_sec,
            model_name=f"elevenlabs ({settings.elevenlabs_music_model})",
            warnings=warnings,
        )
