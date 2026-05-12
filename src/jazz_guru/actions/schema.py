"""Shared JSON-schema normalization for tool inputs.

Both static (`@registry.register(..., input_model=...)`) and dynamic
(`tool_create(input_schema={...})`) tool definitions flow through this
function before being handed to the Anthropic SDK. The goal is one
canonical shape so the two code paths produce byte-identical schemas for
equivalent inputs.
"""
from __future__ import annotations

from typing import Any


def normalize_input_schema(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Return a canonical JSON-schema dict for an Anthropic tool's ``input_schema``.

    Rules:
      * Strip ``title`` at every level (Pydantic auto-injects them; they
        bloat the prompt and contribute nothing to the model).
      * Force ``type: "object"`` at top level.
      * Ensure ``properties: {}`` is present.
      * Default ``additionalProperties: False`` unless the caller set it.
      * Preserve ``required`` and ``$defs`` verbatim (Anthropic supports them).
      * Sort ``properties`` keys for stable output.
    """
    if not raw:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    out = _strip_titles(raw)
    out.setdefault("type", "object")
    if out["type"] != "object":
        # Tool input schemas must be objects per the Anthropic protocol;
        # leave non-object schemas untouched so the caller sees the failure
        # mode in their actual data rather than a silent rewrite.
        return out
    out.setdefault("properties", {})
    out.setdefault("additionalProperties", False)
    out["properties"] = {k: out["properties"][k] for k in sorted(out["properties"])}
    return out


def _strip_titles(node: Any) -> Any:
    """Recursively drop ``title`` keys. Returns a fresh structure."""
    if isinstance(node, dict):
        return {k: _strip_titles(v) for k, v in node.items() if k != "title"}
    if isinstance(node, list):
        return [_strip_titles(v) for v in node]
    return node
