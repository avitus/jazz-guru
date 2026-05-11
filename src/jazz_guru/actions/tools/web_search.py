from __future__ import annotations

import json

import httpx
from pydantic import BaseModel, Field

from jazz_guru.actions.registry import registry
from jazz_guru.config import get_settings


class WebSearchInput(BaseModel):
    query: str = Field(..., min_length=1, description="Non-empty search query.")
    max_results: int = Field(5, ge=1, le=20)


@registry.register(
    "web_search",
    description="Search the web via Tavily; returns list of {title,url,content,score}.",
    input_model=WebSearchInput,
    tags=("web",),
)
async def web_search(query: str, max_results: int = 5) -> dict[str, object]:
    if not query or not query.strip():
        return {"results": [], "error": "query must be non-empty"}
    s = get_settings()
    if not s.tavily_api_key:
        return {"results": [], "error": "TAVILY_API_KEY not set"}
    payload = {
        "api_key": s.tavily_api_key,
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post("https://api.tavily.com/search", json=payload)
    except httpx.HTTPError as e:
        return {"results": [], "error": f"tavily transport error: {type(e).__name__}: {e}"}
    if r.status_code != 200:
        return {"results": [], "error": f"tavily {r.status_code}: {r.text[:300]}"}
    try:
        data = r.json()
    except json.JSONDecodeError as e:
        return {"results": [], "error": f"tavily returned non-JSON body: {e}"}
    results = [
        {
            "title": it.get("title"),
            "url": it.get("url"),
            "content": it.get("content"),
            "score": it.get("score"),
        }
        for it in data.get("results", [])
    ]
    return {"results": results}
