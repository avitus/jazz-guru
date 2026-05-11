from __future__ import annotations

from abc import ABC, abstractmethod
from functools import lru_cache

from jazz_guru.config import get_settings


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


@lru_cache(maxsize=1)
def get_embeddings() -> EmbeddingProvider:
    s = get_settings()
    provider = (s.embedding_provider or "").lower()
    if provider == "voyage":
        if s.voyage_api_key:
            return VoyageProvider()
        # Fall through to the hash stub silently only when the operator did
        # not pick a real provider. If they did pick one, treat the missing
        # key as a configuration error so the system doesn't quietly run
        # with non-comparable hash-stub embeddings.
        raise RuntimeError(
            "EMBEDDING_PROVIDER=voyage but VOYAGE_API_KEY is unset. "
            "Set the key, or set EMBEDDING_PROVIDER=hash for the offline stub."
        )
    if provider in ("", "hash", "stub", "none"):
        return HashStubProvider()
    raise RuntimeError(f"unknown EMBEDDING_PROVIDER: {s.embedding_provider!r}")
