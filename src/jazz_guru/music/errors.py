"""Errors raised by the music-backend layer.

Backends that depend on optional packages (Basic Pitch, Omnizart, MT3,
Music Flamingo, Magenta-RT, ElevenLabs Music, ...) must not crash the
harness at import time. They should lazy-import their dependency the
first time they are *used* and raise :class:`BackendUnavailableError`
with a clear "install X" message if the import fails. The
``analyze_practice_take`` orchestrator catches this error and turns it
into a non-fatal warning so the rest of the analysis can continue.
"""
from __future__ import annotations


class BackendUnavailableError(RuntimeError):
    """Raised when an optional music backend's dependency is not installed.

    Carries the backend name and a hint about what to install. Catch this
    at the orchestrator boundary; never let it bubble out of the harness.
    """

    def __init__(self, backend: str, reason: str, install_hint: str | None = None) -> None:
        msg = f"music backend '{backend}' unavailable: {reason}"
        if install_hint:
            msg += f" (install: {install_hint})"
        super().__init__(msg)
        self.backend = backend
        self.reason = reason
        self.install_hint = install_hint
