"""Pytest fixtures shared across unit tests."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
async def _dispose_db_engine_per_test() -> None:
    """Dispose the cached SQLAlchemy async engine after each test.

    ``jazz_guru.db.get_engine`` is wrapped in ``lru_cache`` for production
    use (one engine per process). Under pytest-asyncio's default
    function-scoped event loop, the engine survives across tests but its
    asyncpg connections were bound to the prior loop — once that loop
    closes, the next test's GC trips ``RuntimeError: Event loop is closed``
    while trying to tear down those connections. Disposing while the
    test's own loop is still alive avoids that, and the next test
    transparently gets a fresh engine.
    """
    yield
    from jazz_guru import db

    if db.get_engine.cache_info().currsize > 0:
        engine = db.get_engine()
        await engine.dispose()
        db.get_engine.cache_clear()
        db.get_sessionmaker.cache_clear()
