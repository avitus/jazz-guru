"""MT3 transcription adapter.

`MT3 <https://github.com/magenta/mt3>`_ (Magenta's "Multi-Task Multitrack
Music Transcription" model) is JAX/T5X-based and famously *not*
pip-installable as a stable Python API. This adapter supports two real
invocation paths and falls back to :class:`BackendUnavailableError`
when neither is configured:

1. **CLI subprocess.** If ``JG_MT3_CLI`` is set on the environment, the
   adapter shells out to it as
   ``<cli> --input <audio> --output <midi_path>``. Use this when you
   have a packaged MT3 binary or your own wrapper script.

2. **Python module.** If the ``mt3`` package is importable, the adapter
   calls ``mt3.inference.transcribe(audio_path, midi_path)``. The
   public API in the upstream repo is unstable, so the adapter is
   defensive about its signature: any exception is caught and surfaced
   as a warning rather than crashing the agent loop.

The MIDI output is written under ``<audio_dir>/transcriptions/`` so it
stays inside the session workspace.
"""
from __future__ import annotations

import asyncio
import shlex
import shutil
from pathlib import Path

from jazz_guru.config import get_settings
from jazz_guru.music._compat import run_coro_sync
from jazz_guru.music.interfaces import BaseBackend
from jazz_guru.music.models import TranscriptionResult

# 10-minute hard cap on the external CLI call so a hung MT3 invocation
# can't block the agent loop forever. Tunable via JG_MT3_CLI_TIMEOUT_SEC.
_DEFAULT_CLI_TIMEOUT_SEC = 600.0


class MT3Backend(BaseBackend):
    """Audio → MIDI via Magenta's MT3 model.

    Heavy: JAX + T5X + multi-GB checkpoint. CPU-only inference works but
    is slow; a GPU is recommended.
    """

    name: str = "mt3"
    install_hint: str | None = (
        "see https://github.com/magenta/mt3 for the JAX/T5X install; "
        "then either set JG_MT3_CLI=<path-to-cli> or `pip install -e .` "
        "the upstream repo so `mt3` imports cleanly"
    )

    @classmethod
    def _probe(cls) -> bool:  # type: ignore[override]
        """Available if either invocation path is wired up.

        Note: returns ``bool`` rather than the parent's ``None``-or-raise
        contract because we want to *measure* availability rather than
        let an ``ImportError`` decide it. The base class's
        ``is_available()`` only checks "did probe raise" — overriding
        that below.
        """
        cli = get_settings().jg_mt3_cli.strip()
        # shlex.split honours quoted executables / paths with spaces, which
        # plain str.split() would shred (e.g. `"/opt/MT3 Wrapper/bin/mt3"`).
        cli_argv = shlex.split(cli) if cli else []
        if cli_argv and shutil.which(cli_argv[0]):
            return True
        try:
            import mt3  # type: ignore[import-not-found]  # noqa: F401
            from mt3 import inference as _inf  # type: ignore[import-not-found]  # noqa: F401
        except ImportError:
            return False
        return True

    @classmethod
    def is_available(cls) -> bool:  # override base
        try:
            return cls._probe()
        except Exception:  # pragma: no cover - defensive
            return False

    # ------------------------------------------------------------------
    # invocation paths
    # ------------------------------------------------------------------

    async def _run_cli(self, cli: str, audio_path: Path, midi_path: Path) -> tuple[int, str]:
        """Shell out to a user-configured MT3 CLI; return (rc, stderr).

        Bounded by ``_DEFAULT_CLI_TIMEOUT_SEC`` so a stuck child can't
        wedge the agent loop. On timeout we kill, reap, and return a
        clear ``124`` exit code so the orchestrator surfaces the
        condition as a warning rather than hanging.
        """
        argv = [*shlex.split(cli), "--input", str(audio_path), "--output", str(midi_path)]
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(
                proc.communicate(), timeout=_DEFAULT_CLI_TIMEOUT_SEC
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, f"MT3 CLI timed out after {_DEFAULT_CLI_TIMEOUT_SEC:.0f}s"
        return proc.returncode or 0, err.decode("utf-8", errors="replace")

    def _run_python(self, audio_path: Path, midi_path: Path) -> None:
        """Call ``mt3.inference.transcribe``; raises on failure.

        Kept narrow on purpose so tests can monkeypatch it without
        loading the JAX stack.
        """
        from mt3 import inference  # type: ignore[import-not-found]

        # The upstream API has shifted over time; we feed both common
        # signatures (``transcribe(audio_path, output_path)`` and
        # ``transcribe(audio_path=..., midi_path=...)``) and let mt3
        # raise if neither matches.
        try:
            inference.transcribe(str(audio_path), str(midi_path))
        except TypeError:
            inference.transcribe(audio_path=str(audio_path), midi_path=str(midi_path))

    # ------------------------------------------------------------------
    # public protocol method
    # ------------------------------------------------------------------

    def transcribe_to_midi(
        self, audio_path: Path, *, instrument: str | None = None
    ) -> TranscriptionResult:
        audio_path = Path(audio_path)
        if not audio_path.exists():
            return TranscriptionResult(
                backend=self.name,
                warnings=[f"audio file not found: {audio_path}"],
            )
        if not self.is_available():
            raise self._unavailable(
                "neither JG_MT3_CLI nor the `mt3` python package is available"
            )

        out_dir = audio_path.parent / "transcriptions"
        out_dir.mkdir(parents=True, exist_ok=True)
        midi_path = out_dir / f"{audio_path.stem}_mt3.mid"
        warnings: list[str] = []
        if instrument:
            warnings.append(
                f"MT3 does not accept a per-instrument hint; '{instrument}' "
                "recorded for context only."
            )

        cli = get_settings().jg_mt3_cli.strip()
        cli_argv = shlex.split(cli) if cli else []
        if cli_argv and shutil.which(cli_argv[0]):
            # ``transcribe_to_midi`` is part of a sync protocol but may be
            # called from inside an event loop (agent tools / tests). Use
            # the compat helper so we never collide with a running loop.
            rc, err = run_coro_sync(self._run_cli(cli, audio_path, midi_path))
            if rc != 0:
                return TranscriptionResult(
                    backend=self.name,
                    warnings=[*warnings, f"MT3 CLI exit {rc}: {err.strip()[:400]}"],
                )
            model_name = f"mt3 cli ({cli_argv[0]})"
        else:
            try:
                self._run_python(audio_path, midi_path)
            except Exception as exc:  # pragma: no cover - depends on optional dep
                return TranscriptionResult(
                    backend=self.name,
                    warnings=[*warnings, f"mt3.inference failed: {exc}"],
                )
            model_name = "mt3 (python)"

        note_count: int | None = None
        if midi_path.exists():
            try:
                from jazz_guru.music.notation.midi import midi_note_count

                note_count = midi_note_count(midi_path)
            except Exception as exc:  # pragma: no cover - mido is mandatory
                warnings.append(f"midi inspection failed: {exc}")
        else:
            warnings.append(f"MT3 did not produce a midi at {midi_path}")
            midi_path = None  # type: ignore[assignment]

        return TranscriptionResult(
            backend=self.name,
            midi_path=midi_path,
            note_count=note_count,
            model_name=model_name,
            warnings=warnings,
        )
