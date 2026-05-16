"""Music Flamingo (Audio Flamingo 3) adapter.

Implements :class:`~jazz_guru.music.interfaces.MusicUnderstandingBackend`
on top of NVIDIA's Audio Flamingo 3 family via the ``transformers``
library. Default model id is ``nvidia/audio-flamingo-3-hf`` (the base
model); set ``MUSIC_FLAMINGO_MODEL=nvidia/audio-flamingo-3-chat`` for
the instruction-tuned variant.

The dependency is **optional**: ``transformers`` lives in the
``audio-ml`` extra and is not imported until the backend is actually
invoked. A first call also pulls the model weights from Hugging Face
(several GB), so this is firmly Phase-2 / opt-in.

Inference path:

* ``transformers.AutoProcessor.from_pretrained(model_id)``
* ``transformers.AudioFlamingo3ForConditionalGeneration.from_pretrained(model_id)``
* ``processor.apply_chat_template(conversation, ...)``
* ``model.generate(**inputs, max_new_tokens=settings.music_flamingo_max_new_tokens)``
* ``processor.batch_decode(...)``

The generated free text lands in
:attr:`~jazz_guru.music.models.MusicAnalysis.summary`. A best-effort
regex sweep pulls out a key signature, tempo, and time signature when
the model mentions them, so the orchestrator can populate the
structured fields too.
"""
from __future__ import annotations

import contextlib
import re
from pathlib import Path
from typing import Any

from jazz_guru.config import get_settings
from jazz_guru.music.interfaces import BaseBackend
from jazz_guru.music.models import MusicAnalysis, MusicContext

_KEY_PATTERNS = (
    re.compile(
        r"\bkey\s+of\s+([A-G][#b♯♭]?\s+(?:major|minor|maj|min))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:detected|likely)\s+key[:\s]+([A-G][#b♯♭]?\s+(?:major|minor|maj|min))\b",
        re.IGNORECASE,
    ),
)
_TEMPO_PATTERN = re.compile(
    r"(?:tempo[:\s]+|around\s+|\babout\s+|\b)(\d{2,3}(?:\.\d+)?)\s*BPM\b",
    re.IGNORECASE,
)
_TIMESIG_PATTERN = re.compile(r"\b([2-9])\s*/\s*([248])\b")


def _extract_fields(text: str) -> dict[str, Any]:
    """Best-effort scrape: key, tempo_bpm, time_signature from free text."""
    out: dict[str, Any] = {}
    for pat in _KEY_PATTERNS:
        match = pat.search(text)
        if match:
            out["detected_key"] = match.group(1).strip()
            break
    tempo_match = _TEMPO_PATTERN.search(text)
    if tempo_match:
        with contextlib.suppress(ValueError):
            out["tempo_bpm"] = float(tempo_match.group(1))
    ts_match = _TIMESIG_PATTERN.search(text)
    if ts_match:
        out["time_signature"] = f"{ts_match.group(1)}/{ts_match.group(2)}"
    return out


class MusicFlamingoBackend(BaseBackend):
    """Audio Flamingo 3 music-understanding model via ``transformers``."""

    name: str = "music_flamingo"
    install_hint: str | None = (
        "pip install 'jazz-guru[audio-ml]'  # transformers + torch; "
        "first call downloads the model from Hugging Face (~several GB)"
    )

    @classmethod
    def _probe(cls) -> None:
        # transformers >= 5.0 is required; earlier versions import cleanly but
        # lack AudioFlamingo3ForConditionalGeneration, so the probe must
        # verify the class itself, not just the package.
        from transformers import (  # type: ignore[import-untyped]
            AudioFlamingo3ForConditionalGeneration,  # noqa: F401
        )

    # ------------------------------------------------------------------
    # model load + inference (kept narrow so tests can monkeypatch)
    # ------------------------------------------------------------------

    def _load(self, model_id: str) -> tuple[Any, Any]:
        """Return ``(processor, model)``. Override in tests."""
        from transformers import AutoProcessor  # type: ignore[import-untyped]

        # Imported by string so a transformers version without this class
        # raises a clean ImportError that we wrap below.
        try:
            from transformers import (  # type: ignore[attr-defined]
                AudioFlamingo3ForConditionalGeneration,
            )
        except ImportError as exc:
            raise self._unavailable(
                "this version of transformers does not expose "
                "AudioFlamingo3ForConditionalGeneration; "
                "upgrade transformers >= 5.0 or pin a release that includes the class"
            ) from exc

        processor = AutoProcessor.from_pretrained(model_id)
        model = AudioFlamingo3ForConditionalGeneration.from_pretrained(
            model_id, device_map="auto"
        )
        return processor, model

    def _predict(
        self,
        audio_path: Path,
        prompt: str,
        *,
        max_new_tokens: int,
        model_id: str,
    ) -> str:
        """Run inference and return the generated text. Override in tests."""
        processor, model = self._load(model_id)

        conversation = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "audio", "path": str(audio_path)},
                ],
            }
        ]
        inputs = processor.apply_chat_template(
            conversation,
            tokenize=True,
            add_generation_prompt=True,
            return_dict=True,
            return_tensors="pt",
        )
        outputs = model.generate(**inputs, max_new_tokens=max_new_tokens)
        # Strip the prompt tokens before decoding so we only see the model's reply.
        new_tokens = outputs[:, inputs["input_ids"].shape[1]:]
        decoded = processor.batch_decode(new_tokens, skip_special_tokens=True)
        return decoded[0] if decoded else ""

    # ------------------------------------------------------------------
    # public protocol method
    # ------------------------------------------------------------------

    def analyze_audio(
        self, audio_path: Path, *, context: MusicContext | None = None
    ) -> MusicAnalysis:
        audio_path = Path(audio_path)
        if not audio_path.exists():
            return MusicAnalysis(
                backend=self.name, warnings=[f"audio file not found: {audio_path}"]
            )
        if not self.is_available():
            raise self._unavailable("transformers is not installed")

        settings = get_settings()
        prompt = settings.music_flamingo_prompt
        if context and context.chart:
            prompt = (
                f"The performer is practising '{context.chart}'"
                + (f" on {context.instrument}" if context.instrument else "")
                + ". "
                + prompt
            )

        warnings: list[str] = []
        try:
            text = self._predict(
                audio_path,
                prompt,
                max_new_tokens=settings.music_flamingo_max_new_tokens,
                model_id=settings.music_flamingo_model,
            )
        except Exception as exc:  # pragma: no cover - depends on optional dep
            return MusicAnalysis(
                backend=self.name,
                warnings=[f"audio-flamingo inference failed: {exc}"],
            )

        scraped = _extract_fields(text)
        return MusicAnalysis(
            backend=self.name,
            summary=text.strip() or None,
            detected_key=scraped.get("detected_key"),
            tempo_bpm=scraped.get("tempo_bpm"),
            time_signature=scraped.get("time_signature"),
            warnings=warnings,
        )
