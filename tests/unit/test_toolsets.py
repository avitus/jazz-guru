from __future__ import annotations

from jazz_guru.config import Policy, ToolPolicy, ToolsetSpec


def test_toolset_membership_allows_new_tool() -> None:
    p = Policy(
        toolsets={"music": ToolsetSpec(tools=["render_midi", "preset_list"])},
    )
    # Tool not in `tools` dict, but in a toolset:
    tp = p.for_tool("preset_list")
    assert tp.mode == "allow"


def test_per_tool_entry_beats_toolset() -> None:
    p = Policy(
        tools={"render_midi": ToolPolicy(mode="deny")},
        toolsets={"music": ToolsetSpec(tools=["render_midi"], mode="allow")},
    )
    assert p.for_tool("render_midi").mode == "deny"


def test_toolset_feature_flag_propagates() -> None:
    p = Policy(
        toolsets={
            "audio": ToolsetSpec(tools=["tts"], mode="allow", feature_flag="FEATURE_AUDIO")
        },
    )
    tp = p.for_tool("tts")
    assert tp.mode == "allow"
    assert tp.feature_flag == "FEATURE_AUDIO"


def test_toolset_deny_propagates() -> None:
    p = Policy(
        toolsets={"shell": ToolsetSpec(tools=["shell", "python_exec"], mode="deny")},
    )
    assert p.for_tool("shell").mode == "deny"
    assert p.for_tool("python_exec").mode == "deny"


def test_default_when_no_match() -> None:
    p = Policy(default="deny")
    assert p.for_tool("unknown").mode == "deny"


def test_toolset_for_tool_lookup() -> None:
    p = Policy(
        toolsets={
            "music": ToolsetSpec(tools=["render_midi"]),
            "fs":    ToolsetSpec(tools=["fs_read"]),
        },
    )
    ts = p.toolset_for_tool("render_midi")
    assert ts is not None
    assert "render_midi" in ts.tools
    assert p.toolset_for_tool("nope") is None


def test_toolset_for_tool_rejects_duplicate_membership() -> None:
    """A tool listed in two toolsets has YAML-order-dependent policy — flag it."""
    import pytest

    p = Policy(
        toolsets={
            "music": ToolsetSpec(tools=["render_midi"]),
            "audio": ToolsetSpec(tools=["render_midi", "tts"]),
        },
    )
    with pytest.raises(ValueError, match="multiple toolsets"):
        p.toolset_for_tool("render_midi")
    # Unambiguous lookups must still work even when one tool is duplicated.
    assert p.toolset_for_tool("tts") is not None
    assert p.toolset_for_tool("nope") is None


def test_real_policy_yaml_groups_preset_tools() -> None:
    from jazz_guru.config import get_policy

    policy = get_policy()
    music = policy.toolsets.get("music")
    assert music is not None
    assert "preset_upsert" in music.tools
    assert "render_midi" in music.tools
