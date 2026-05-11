"""Regression guards on settings values that have to satisfy SDK contracts.

These tests don't call the network — they re-encode the constants and formulas
the Anthropic SDK enforces client-side, so we catch config drift the moment a
default is bumped past a hard limit. Two prior bugs that would have been caught:

  1. anthropic_max_tokens=65536  ->  exceeds Sonnet 4.5's published 64k output cap
  2. anthropic_max_tokens=64000  ->  exceeds the SDK's 10-min non-streaming guard
"""

from __future__ import annotations

import inspect

import anthropic

from jazz_guru.config import Settings

# Mirror the SDK's formula in anthropic/_base_client.py::_calculate_nonstreaming_timeout
# expected_time = 3600 * max_tokens / 128_000   (seconds)
# the SDK raises ValueError if expected_time > 600 (i.e. > 10 minutes).
_NONSTREAMING_MAX_TOKENS_CAP = 600 * 128_000 // 3600  # == 21333


def test_default_max_tokens_under_nonstreaming_cap() -> None:
    """The code default for anthropic_max_tokens must work with non-streaming requests.

    If you raise the default above the SDK threshold, also switch complete()
    to use client.messages.stream(...) — otherwise every LLM call breaks.

    Reads the field default directly so an operator's local `.env` override
    doesn't mask a code regression. (We still document the limit in
    `.env.example` so .env overrides also stay in bounds.)
    """
    default = Settings.model_fields["anthropic_max_tokens"].default
    assert default <= _NONSTREAMING_MAX_TOKENS_CAP, (
        f"code-default anthropic_max_tokens={default} exceeds the Anthropic SDK's "
        f"non-streaming guard at {_NONSTREAMING_MAX_TOKENS_CAP} (= 600s * 128_000 / 3600). "
        "Either lower the default or migrate complete() to streaming."
    )


def test_sdk_threshold_formula_still_matches() -> None:
    """If the SDK changes its formula, fail loudly so we update our guard."""
    src = inspect.getsource(anthropic._base_client.BaseClient._calculate_nonstreaming_timeout)
    # Sanity: confirm the constants we depend on are still in the SDK source.
    assert "128_000" in src, "SDK formula changed — update _NONSTREAMING_MAX_TOKENS_CAP."
    assert "60 * 10" in src or "600" in src, "SDK 10-min default changed — update guard."


def test_default_max_tokens_under_model_output_cap() -> None:
    """Don't regress past the published per-model output ceiling either.

    Sonnet 4.5's documented sync output cap is 64_000. We pick this up as a
    soft check — if someone bumps the default toward a different model later,
    the value will be re-evaluated by whoever changes the model string.
    """
    default_tokens = Settings.model_fields["anthropic_max_tokens"].default
    default_model = Settings.model_fields["anthropic_model"].default
    if default_model.startswith("claude-sonnet-4-5"):
        assert default_tokens <= 64_000, (
            f"anthropic_max_tokens={default_tokens} > Sonnet 4.5 published "
            "output ceiling (64000)."
        )
