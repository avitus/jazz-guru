from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from jazz_guru.config import get_settings
from jazz_guru.memory import embeddings as emb_mod
from jazz_guru.memory.embeddings import (
    HashStubProvider,
    OllamaProvider,
    _ollama_available,
    get_embeddings,
)


@pytest.fixture(autouse=True)
def _reset_caches() -> Iterator[None]:
    """Reset the singleton + settings caches so each test sees fresh provider
    resolution from the env it sets up."""
    get_embeddings.cache_clear()
    get_settings.cache_clear()
    yield
    get_embeddings.cache_clear()
    get_settings.cache_clear()


async def test_hash_stub_provides_unit_norm_vectors() -> None:
    p = HashStubProvider(dim=64)
    vecs = await p.embed(["hello", "world"])
    assert len(vecs) == 2
    for v in vecs:
        assert len(v) == 64
        norm = sum(x * x for x in v) ** 0.5
        assert abs(norm - 1.0) < 1e-6


async def test_hash_stub_deterministic() -> None:
    p = HashStubProvider(dim=32)
    a = (await p.embed(["foo"]))[0]
    b = (await p.embed(["foo"]))[0]
    assert a == b


# ---------- _ollama_available probe ----------


class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *a: object) -> None:
        return None


def _patch_urlopen(monkeypatch: pytest.MonkeyPatch, body: bytes | Exception) -> None:
    def fake(_req: Any, timeout: float = 0) -> _FakeResp:
        if isinstance(body, Exception):
            raise body
        return _FakeResp(body)

    monkeypatch.setattr(emb_mod.urllib.request, "urlopen", fake)


def test_ollama_probe_ok_when_model_present(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_urlopen(monkeypatch, b'{"models":[{"name":"mxbai-embed-large:latest"}]}')
    ok, why = _ollama_available("http://localhost:11434", "mxbai-embed-large", 0.5)
    assert ok is True
    assert why == "ok"


def test_ollama_probe_strips_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    # The probe should match `model` against `name.split(':')[0]` on both sides.
    _patch_urlopen(monkeypatch, b'{"models":[{"name":"mxbai-embed-large:latest"}]}')
    ok, _ = _ollama_available("http://localhost:11434", "mxbai-embed-large:latest", 0.5)
    assert ok is True


def test_ollama_probe_fails_when_model_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_urlopen(monkeypatch, b'{"models":[{"name":"llama3:latest"}]}')
    ok, why = _ollama_available("http://localhost:11434", "mxbai-embed-large", 0.5)
    assert ok is False
    assert "not pulled" in why
    assert "mxbai-embed-large" in why


def test_ollama_probe_fails_when_daemon_down(monkeypatch: pytest.MonkeyPatch) -> None:
    import urllib.error

    _patch_urlopen(monkeypatch, urllib.error.URLError("connection refused"))
    ok, why = _ollama_available("http://localhost:11434", "mxbai-embed-large", 0.5)
    assert ok is False
    assert "unreachable" in why


def test_ollama_probe_rejects_scheme_less_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # urllib accepts a scheme-less Request but urlopen later raises an
    # ambiguous ValueError("unknown url type"). Reject early with a clear
    # message AND without making the network call.
    called = False

    def fake_urlopen(*_a: Any, **_kw: Any) -> Any:
        nonlocal called
        called = True
        raise AssertionError("urlopen should not be called for invalid URL")

    monkeypatch.setattr(emb_mod.urllib.request, "urlopen", fake_urlopen)
    ok, why = _ollama_available("localhost:11434", "mxbai-embed-large", 0.5)
    assert ok is False
    assert "unsupported Ollama URL" in why
    assert called is False


def test_ollama_probe_rejects_file_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    # file:// URLs would let urlopen read local files; treat as unsupported.
    monkeypatch.setattr(emb_mod.urllib.request, "urlopen", lambda *_a, **_kw: None)
    ok, why = _ollama_available("file:///tmp/api/tags", "mxbai-embed-large", 0.5)
    assert ok is False
    assert "unsupported Ollama URL" in why


def test_ollama_probe_accepts_https(monkeypatch: pytest.MonkeyPatch) -> None:
    # https://host[:port] is a valid scheme; should pass validation and
    # reach the urlopen-driven happy path.
    _patch_urlopen(monkeypatch, b'{"models":[{"name":"mxbai-embed-large:latest"}]}')
    ok, why = _ollama_available("https://ollama.example.com", "mxbai-embed-large", 0.5)
    assert ok is True
    assert why == "ok"


# ---------- OllamaProvider.embed ----------


class _FakeHttpxResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeHttpxClient:
    def __init__(self, *, vectors: list[list[float]]) -> None:
        self._vectors = list(vectors)
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, *, json: dict[str, Any]) -> _FakeHttpxResponse:
        self.calls.append({"url": url, "json": json})
        v = self._vectors.pop(0)
        return _FakeHttpxResponse({"embedding": v})


async def test_ollama_provider_embeds_each_text(monkeypatch: pytest.MonkeyPatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "ollama_url", "http://localhost:11434")
    monkeypatch.setattr(s, "ollama_embedding_model", "mxbai-embed-large")
    monkeypatch.setattr(s, "embedding_dim", 4)

    fake = _FakeHttpxClient(vectors=[[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]])
    p = OllamaProvider()
    p._client = fake  # type: ignore[assignment]

    out = await p.embed(["foo", "bar"])
    assert out == [[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]]
    assert len(fake.calls) == 2
    assert fake.calls[0]["url"].endswith("/api/embeddings")
    assert fake.calls[0]["json"] == {"model": "mxbai-embed-large", "prompt": "foo"}


async def test_ollama_provider_rejects_dim_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    s = get_settings()
    monkeypatch.setattr(s, "embedding_dim", 4)
    p = OllamaProvider()
    p._client = _FakeHttpxClient(vectors=[[0.1, 0.2]])  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="dim mismatch"):
        await p.embed(["foo"])


async def test_ollama_provider_empty_input_short_circuits() -> None:
    p = OllamaProvider()
    # No client patching needed — empty input must not touch the network.
    assert await p.embed([]) == []


# ---------- get_embeddings cascade ----------


def _force_provider_resolution(
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider: str,
    voyage_key: str = "",
    ollama_ok: bool = False,
    ollama_reason: str = "stubbed",
) -> None:
    """Patch settings + ollama probe so get_embeddings resolves deterministically."""
    get_settings.cache_clear()
    get_embeddings.cache_clear()
    s = get_settings()
    monkeypatch.setattr(s, "embedding_provider", provider)
    monkeypatch.setattr(s, "voyage_api_key", voyage_key)
    monkeypatch.setattr(
        emb_mod,
        "_ollama_available",
        lambda _u, _m, _t: (ollama_ok, "ok" if ollama_ok else ollama_reason),
    )
    # Prevent the real VoyageProvider from importing voyageai or hitting the
    # network — if a test expects voyage, it can flip this.
    monkeypatch.setattr(emb_mod, "VoyageProvider", _StubVoyage)
    monkeypatch.setattr(emb_mod, "OllamaProvider", _StubOllama)


class _StubVoyage(emb_mod.EmbeddingProvider):
    @property
    def dim(self) -> int:
        return 1024

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * 1024 for _ in texts]


class _StubOllama(emb_mod.EmbeddingProvider):
    @property
    def dim(self) -> int:
        return 1024

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] * 1024 for _ in texts]


def test_auto_picks_voyage_when_key_set(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_provider_resolution(monkeypatch, provider="auto", voyage_key="pa-fake")
    assert isinstance(get_embeddings(), _StubVoyage)


def test_auto_picks_ollama_when_voyage_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_provider_resolution(monkeypatch, provider="auto", voyage_key="", ollama_ok=True)
    assert isinstance(get_embeddings(), _StubOllama)


def test_auto_falls_to_hash_with_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _force_provider_resolution(
        monkeypatch,
        provider="auto",
        voyage_key="",
        ollama_ok=False,
        ollama_reason="daemon unreachable at http://localhost:11434",
    )
    p = get_embeddings()
    assert isinstance(p, HashStubProvider)
    stderr = capsys.readouterr().err
    assert "HashStubProvider" in stderr
    assert "NOT SEMANTIC" in stderr
    assert "VOYAGE_API_KEY is unset" in stderr
    assert "daemon unreachable" in stderr


def test_strict_voyage_errors_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_provider_resolution(monkeypatch, provider="voyage", voyage_key="")
    with pytest.raises(RuntimeError, match="VOYAGE_API_KEY is unset"):
        get_embeddings()


def test_strict_ollama_errors_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_provider_resolution(
        monkeypatch, provider="ollama", ollama_ok=False, ollama_reason="model not pulled"
    )
    with pytest.raises(RuntimeError, match=r"not usable.*model not pulled"):
        get_embeddings()


def test_strict_ollama_resolves_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_provider_resolution(monkeypatch, provider="ollama", ollama_ok=True)
    assert isinstance(get_embeddings(), _StubOllama)


def test_explicit_hash_returns_stub_without_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _force_provider_resolution(monkeypatch, provider="hash")
    p = get_embeddings()
    assert isinstance(p, HashStubProvider)
    # Explicit hash is an operator choice, not a fallback — no banner.
    assert "HashStubProvider" not in capsys.readouterr().err


def test_unknown_provider_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_provider_resolution(monkeypatch, provider="madeup")
    with pytest.raises(RuntimeError, match="unknown EMBEDDING_PROVIDER"):
        get_embeddings()
