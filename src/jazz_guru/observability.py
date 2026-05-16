"""Sentry initialization.

One entrypoint: :func:`init_sentry`. Each process (server, worker, CLI) calls
it once, very early — before importing heavy modules — so import-time failures
also land in Sentry. The call is a no-op when ``SENTRY_DSN`` is empty so tests
and operators who opt out of error reporting don't ship anything to Sentry.

Config knobs live alongside the other settings in
:mod:`jazz_guru.config` and are mirrored in ``.env.example``.
"""
from __future__ import annotations

import sys

from jazz_guru.config import get_settings

_initialized = False


def init_sentry() -> bool:
    """Initialize Sentry from settings. Returns True if Sentry was activated.

    Safe to call more than once: subsequent calls are no-ops after the first
    successful init. If ``sentry-sdk`` is not installed or the DSN is empty,
    this returns False silently so the process keeps running.

    Also skipped under pytest — the default DSN is committed for ease of
    setup, and we don't want unit tests to ship events to production Sentry.
    """
    global _initialized
    if _initialized:
        return True

    # Pytest sets sys.modules["pytest"] at startup before any test code runs,
    # so this catches both collection-time imports and per-test fixtures.
    if "pytest" in sys.modules:
        return False

    settings = get_settings()
    if not settings.sentry_dsn:
        return False

    try:
        import sentry_sdk
    except ImportError:
        return False

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.sentry_environment,
        # See https://docs.sentry.io/platforms/python/data-management/data-collected/
        send_default_pii=settings.sentry_send_default_pii,
        # Forwards structlog / stdlib logging events to Sentry as breadcrumbs/logs.
        enable_logs=settings.sentry_enable_logs,
        # 1.0 captures every transaction; tune down via SENTRY_TRACES_SAMPLE_RATE
        # if this becomes noisy in production.
        traces_sample_rate=settings.sentry_traces_sample_rate,
        profile_session_sample_rate=settings.sentry_profile_session_sample_rate,
        # "trace" runs the profiler while a transaction is active; "manual"
        # leaves it to explicit start/stop calls.
        profile_lifecycle=settings.sentry_profile_lifecycle,
    )
    _initialized = True
    return True
