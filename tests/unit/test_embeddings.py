from __future__ import annotations

from jazz_guru.memory.embeddings import HashStubProvider


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
