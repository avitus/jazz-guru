from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from functools import lru_cache

import structlog

from jazz_guru.config import get_settings

log = structlog.get_logger(__name__)


class EmbeddingProvider(ABC):
    @property
    @abstractmethod
    def dim(self) -> int: ...

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class VoyageProvider(EmbeddingProvider):
    def __init__(self) -> None:
        import voyageai  # type: ignore[import-untyped]

        s = get_settings()
        if not s.voyage_api_key:
            raise RuntimeError("VOYAGE_API_KEY not set")
        self._client = voyageai.AsyncClient(api_key=s.voyage_api_key)
        self._model = s.embedding_model
        self._dim = s.embedding_dim

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # Pass output_dimension explicitly so the API doesn't quietly hand
        # us the model's default dimension when settings.embedding_dim is
        # different — that mismatch would only show up later as a pgvector
        # "expected N dimensions, not M" error far from the cause.
        res = await self._client.embed(
            texts=texts,
            model=self._model,
            input_type="document",
            output_dimension=self._dim,
        )
        vectors = [list(map(float, e)) for e in res.embeddings]
        for i, v in enumerate(vectors):
            if len(v) != self._dim:
                raise RuntimeError(
                    f"Voyage embedding dimension mismatch at index {i}: "
                    f"model={self._model!r} expected={self._dim} got={len(v)}"
                )
        return vectors


class OllamaProvider(EmbeddingProvider):
    """Local embeddings via an Ollama daemon (default model: mxbai-embed-large, 1024d)."""

    def __init__(self) -> None:
        import httpx

        s = get_settings()
        self._url = (s.ollama_url or "http://localhost:11434").rstrip("/")
        self._model = s.ollama_embedding_model
        self._dim = s.embedding_dim
        self._client = httpx.AsyncClient(timeout=60.0)

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # Ollama's /api/embeddings is single-input. We could parallelize, but
        # memory writes happen one-at-a-time in practice, and search batches
        # are tiny; sequential keeps the local GPU/CPU from thrashing.
        out: list[list[float]] = []
        for t in texts:
            r = await self._client.post(
                f"{self._url}/api/embeddings",
                json={"model": self._model, "prompt": t},
            )
            r.raise_for_status()
            v = [float(x) for x in r.json()["embedding"]]
            if len(v) != self._dim:
                raise RuntimeError(
                    f"Ollama embedding dim mismatch: model={self._model!r} "
                    f"expected={self._dim} got={len(v)}. "
                    "Set EMBEDDING_DIM to match the model's native dimension "
                    "(re-run alembic migrations if you change it)."
                )
            out.append(v)
        return out


class HashStubProvider(EmbeddingProvider):
    """Deterministic offline fallback when no embedding API key is configured."""

    def __init__(self, dim: int | None = None) -> None:
        self._dim = dim or get_settings().embedding_dim

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import hashlib
        import math

        out: list[list[float]] = []
        for t in texts:
            buf: list[float] = []
            seed = hashlib.sha256(t.encode("utf-8")).digest()
            i = 0
            while len(buf) < self._dim:
                if i >= len(seed):
                    seed = hashlib.sha256(seed).digest()
                    i = 0
                buf.append((seed[i] / 255.0) * 2.0 - 1.0)
                i += 1
            norm = math.sqrt(sum(x * x for x in buf)) or 1.0
            out.append([x / norm for x in buf])
        return out


def _ollama_available(url: str, model: str, timeout_s: float) -> tuple[bool, str]:
    """Probe Ollama: daemon up AND the configured model is pulled.

    Returns (ok, reason). Synchronous on purpose — called once at startup
    from the sync ``get_embeddings``.
    """
    url = url.rstrip("/")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return False, f"unsupported Ollama URL {url!r}; expected http(s)://host[:port]"
    try:
        req = urllib.request.Request(f"{url}/api/tags")
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return False, f"daemon unreachable at {url} ({e.__class__.__name__})"
    except (ValueError, KeyError) as e:
        return False, f"unexpected /api/tags response: {e.__class__.__name__}"
    names = {m.get("name", "").split(":")[0] for m in body.get("models", [])}
    if model.split(":")[0] not in names:
        return False, f"model {model!r} not pulled (run: ollama pull {model})"
    return True, "ok"


def _warn_hash_fallback(reasons: list[str]) -> None:
    """Loud, visible warning that the agent is running on hash-stub embeddings."""
    banner = (
        "\n"
        "================================================================\n"
        "  ⚠  EMBEDDINGS: falling back to HashStubProvider (offline stub)\n"
        "================================================================\n"
        "  Hash-stub embeddings are deterministic but NOT SEMANTIC —\n"
        "  memory recall will be effectively keyword-match only and\n"
        "  retrieval quality will be substantially worse than with a\n"
        "  real embedding model.\n"
        "\n"
        "  To fix, pick one of:\n"
        "    1. Voyage (hosted, free tier 200M tok/mo):\n"
        "         set VOYAGE_API_KEY=... in .env\n"
        "    2. Ollama (local, recommended for offline use):\n"
        "         brew install ollama && brew services start ollama\n"
        "         ollama pull mxbai-embed-large\n"
        "\n"
        "  Why this fallback was chosen:\n"
    )
    for r in reasons:
        banner += f"    - {r}\n"
    banner += "================================================================\n"
    print(banner, file=sys.stderr, flush=True)
    log.warning("embeddings.fallback_to_hash_stub", reasons=reasons)


@lru_cache(maxsize=1)
def get_embeddings() -> EmbeddingProvider:
    s = get_settings()
    provider = (s.embedding_provider or "auto").lower()

    if provider == "auto":
        reasons: list[str] = []
        if s.voyage_api_key:
            return VoyageProvider()
        reasons.append("VOYAGE_API_KEY is unset")
        ok, why = _ollama_available(s.ollama_url, s.ollama_embedding_model, s.ollama_probe_timeout_s)
        if ok:
            return OllamaProvider()
        reasons.append(f"Ollama unavailable: {why}")
        _warn_hash_fallback(reasons)
        return HashStubProvider()

    if provider == "voyage":
        if s.voyage_api_key:
            return VoyageProvider()
        raise RuntimeError(
            "EMBEDDING_PROVIDER=voyage but VOYAGE_API_KEY is unset. "
            "Set the key, switch to EMBEDDING_PROVIDER=ollama or =auto, "
            "or set EMBEDDING_PROVIDER=hash for the offline stub."
        )

    if provider == "ollama":
        ok, why = _ollama_available(s.ollama_url, s.ollama_embedding_model, s.ollama_probe_timeout_s)
        if not ok:
            raise RuntimeError(
                f"EMBEDDING_PROVIDER=ollama but Ollama is not usable: {why}. "
                "Start the daemon (brew services start ollama), pull the model, "
                "or set EMBEDDING_PROVIDER=auto to allow fallback."
            )
        return OllamaProvider()

    if provider in ("hash", "stub", "none"):
        return HashStubProvider()

    raise RuntimeError(f"unknown EMBEDDING_PROVIDER: {s.embedding_provider!r}")
