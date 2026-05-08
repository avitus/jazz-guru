from __future__ import annotations

import base64
import mimetypes

from pydantic import BaseModel, Field

from jazz_guru.actions.context import current
from jazz_guru.actions.registry import registry
from jazz_guru.actions.sandbox import resolve_in_workspace
from jazz_guru.llm import complete


class VisionInput(BaseModel):
    path: str = Field(..., description="Image path in the session workspace.")
    question: str = Field("Describe this image in detail.", description="Prompt about the image.")


@registry.register(
    "vision",
    description="Analyze an image file (JPEG/PNG/GIF/WebP) using Claude vision.",
    input_model=VisionInput,
    tags=("perception",),
)
async def vision(path: str, question: str = "Describe this image in detail.") -> dict[str, object]:
    p = resolve_in_workspace(path, current().session_id)
    media_type = mimetypes.guess_type(str(p))[0] or "image/png"
    if media_type not in {"image/png", "image/jpeg", "image/gif", "image/webp"}:
        return {"error": f"unsupported media type: {media_type}"}
    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    msg = [
        {
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": question},
            ],
        }
    ]
    resp = await complete(msg, max_tokens=1024)
    return {"text": resp.text, "model": resp.raw.model if resp.raw else None}
