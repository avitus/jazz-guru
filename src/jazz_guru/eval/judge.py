from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from jazz_guru.llm import complete
from jazz_guru.logging import get_logger

log = get_logger(__name__)


JUDGE_SYSTEM = """You are a strict, calibrated rubric judge. Score the agent's response
against the rubric. Reply with ONLY a JSON object:

{
  "scores": {"<criterion>": 0.0-1.0, ...},
  "weighted_total": 0.0-1.0,
  "rationale": "2-4 sentences"
}

Use the per-criterion weights as given. weighted_total is sum(weight*score) / sum(weight)."""


@dataclass
class JudgeResult:
    scores: dict[str, float]
    weighted_total: float
    rationale: str
    raw: dict[str, Any] = field(default_factory=dict)


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip().lstrip("`").rstrip("`")
    if text.lower().startswith("json"):
        text = text[4:]
    a, b = text.find("{"), text.rfind("}")
    return json.loads(text[a : b + 1])


async def judge(
    *,
    task: str,
    response: str,
    rubric: dict[str, float],
    expected: str | None = None,
    artifacts: list[str] | None = None,
) -> JudgeResult:
    rubric_block = "\n".join(f"- ({w:.2f}) {k}" for k, w in rubric.items())
    parts = [
        f"## Task\n{task}",
        f"## Rubric (criterion: weight)\n{rubric_block}",
    ]
    if expected:
        parts.append(f"## Expected (reference)\n{expected}")
    if artifacts:
        parts.append("## Artifacts produced\n" + "\n".join(f"- {a}" for a in artifacts))
    parts.append(f"## Agent response\n{response}")
    user = "\n\n".join(parts)
    resp = await complete(
        [{"role": "user", "content": user}],
        system=JUDGE_SYSTEM,
        max_tokens=1024,
        temperature=0.0,
    )
    try:
        data = _extract_json(resp.text)
    except Exception as e:
        log.warning("judge.parse_failed", err=str(e), text=resp.text[:200])
        return JudgeResult(scores={}, weighted_total=0.0, rationale=resp.text[:300])
    return JudgeResult(
        scores={k: float(v) for k, v in data.get("scores", {}).items()},
        weighted_total=float(data.get("weighted_total", 0.0)),
        rationale=str(data.get("rationale", "")),
        raw=data,
    )
