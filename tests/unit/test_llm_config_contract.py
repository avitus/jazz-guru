"""Regression guards on settings values and LLM call-site contracts.

These tests don't call the network — they re-encode the constants and formulas
the Anthropic SDK enforces client-side, so we catch config drift the moment a
default is bumped past a hard limit. Two prior bugs that would have been caught:

  1. anthropic_max_tokens=65536  ->  exceeds Sonnet 4.5's published 64k output cap
  2. anthropic_max_tokens=64000  ->  exceeds the SDK's 10-min non-streaming guard
     (no longer applies now that complete() uses streaming, but the streaming
     guarantee is itself locked in by ``test_complete_uses_streaming_path``)
"""

from __future__ import annotations

import inspect

from jazz_guru import llm as llm_mod
from jazz_guru.config import Settings


def test_default_max_tokens_under_model_output_cap() -> None:
    """Don't regress past the published per-model output ceiling.

    Sonnet 4.5's documented sync output cap is 64_000. We read the field
    default directly so an operator's local `.env` override can't mask a
    code-level regression.
    """
    default_tokens = Settings.model_fields["anthropic_max_tokens"].default
    default_model = Settings.model_fields["anthropic_model"].default
    if default_model.startswith("claude-sonnet-4-5"):
        assert default_tokens <= 64_000, (
            f"anthropic_max_tokens={default_tokens} > Sonnet 4.5 published "
            "output ceiling (64000)."
        )


def test_complete_uses_streaming_path() -> None:
    """``complete()`` must call ``messages.stream``, not ``messages.create``.

    The previous (non-streaming) path imposed an SDK-side 21333-token cap on
    ``max_tokens`` because non-streaming calls have a 10-minute worst-case
    timeout. Switching back to ``create()`` would silently reintroduce that
    cap on the next ``max_tokens`` bump.
    """
    src = inspect.getsource(llm_mod.complete)
    assert "messages.stream" in src, (
        "complete() no longer uses messages.stream(...). "
        "If you switched back to messages.create(...), cap "
        "anthropic_max_tokens at 21333 (per the SDK's non-streaming guard)."
    )
    assert "messages.create" not in src, (
        "complete() appears to still call messages.create(...). "
        "Streaming is required to avoid the SDK's 10-minute non-streaming guard."
    )
