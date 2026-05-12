"""``clarify`` — pause mid-turn to ask the operator a structured question.

The tool returns a sentinel dict ``{"__clarify__": ...}`` that the controller
recognizes. If an active ``clarify_callback`` is bound on the controller, it
awaits the callback (typically: send a WS frame, await the user's response)
and substitutes the answer as the tool_result. If no callback is bound, the
sentinel is forwarded to the model verbatim and the agent uses its own
judgment instead.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from jazz_guru.actions.registry import registry


class ClarifyInput(BaseModel):
    question: str = Field(..., description="The question to ask the operator.")
    options: list[str] | None = Field(
        default=None,
        description=(
            "Up to ~4 short answer choices. If empty, the operator provides "
            "free-text. If non-empty, the response will be one of these strings."
        ),
    )
    multi: bool = Field(
        default=False,
        description="If true and options is set, multiple options can be selected.",
    )
    header: str | None = Field(
        default=None,
        description="Optional <=12-char tag/label rendered as a chip in the UI.",
    )


@registry.register(
    "clarify",
    description=(
        "Ask the operator a structured question and pause the turn until they "
        "respond. Use sparingly — only when proceeding without the answer would "
        "produce a low-quality or wrong artifact. Provide 2-4 'options' for a "
        "multiple-choice question, or omit them for free-text input. The tool "
        "result will be the operator's answer string. If the runtime has no UI "
        "to surface the question (e.g. one-shot CLI), the sentinel is returned "
        "to you and you should proceed using your best judgment."
    ),
    input_model=ClarifyInput,
    tags=("control",),
)
async def clarify(
    question: str,
    options: list[str] | None = None,
    multi: bool = False,
    header: str | None = None,
) -> dict[str, Any]:
    return {
        "__clarify__": {
            "question": question,
            "options": list(options) if options else [],
            "multi": bool(multi),
            "header": header,
        }
    }
