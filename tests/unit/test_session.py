from __future__ import annotations

from jazz_guru.harness.session import row_to_api_message


def test_user_turn_text_extracted() -> None:
    msg = row_to_api_message("user", {"text": "hi there"})
    assert msg == {"role": "user", "content": "hi there"}


def test_assistant_turn_with_extra_keys_extracted() -> None:
    msg = row_to_api_message("assistant", {"text": "ok", "messages_added": 4})
    assert msg == {"role": "assistant", "content": "ok"}


def test_non_chat_role_dropped() -> None:
    assert row_to_api_message("system", {"text": "x"}) is None
    assert row_to_api_message("tool", {"text": "x"}) is None


def test_empty_text_dropped() -> None:
    assert row_to_api_message("user", {"text": ""}) is None
    assert row_to_api_message("user", {"text": "   "}) is None
    assert row_to_api_message("user", {}) is None
    assert row_to_api_message("user", None) is None


def test_non_dict_content_dropped() -> None:
    # Defensive: the DB column stores JSON objects, but if anything else
    # leaks in we should drop it rather than crash the agent loop.
    assert row_to_api_message("user", "raw string") is None
    assert row_to_api_message("user", ["array"]) is None
