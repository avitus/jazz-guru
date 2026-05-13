"""Magenta RealTime adapter.

`Magenta RealTime <https://github.com/magenta/magenta-realtime>`_ is
research-grade and (like MT3) does not ship a stable pip API. The
adapter therefore supports two invocation paths and falls back to
:class:`BackendUnavailableError` when neither works:

1. **CLI subprocess.** If ``JG_MAGENTA_RT_CLI`` is set, the adapter
   shells out to it as
   ``<cli> --prompt <text> --duration <sec> --output <wav>``.
2. **Python module.** If the ``magenta_rt`` package imports cleanly,
   the adapter calls ``magenta_rt.compose(prompt=..., duration_sec=..., output=...)``
   and tolerates a few signature variants.

The generated WAV lands at ``request.output_path`` (or under
``<workspace>/generation/<timestamp>.wav`` when the caller didn't pin
one). All paths are honored verbatim — the orchestrator/tool layer is
responsible for sandboxing.
"""
from __future__ import annotations

import asyncio
import shlex
import shutil
import time
from pathlib import Path

from jazz_guru.config import get_settings
from jazz_guru.music._compat import run_coro_sync
from jazz_guru.music.interfaces import BaseBackend
from jazz_guru.music.models import MusicGenerationRequest, MusicGenerationResult

# Hard cap on the external CLI so a hung magenta-rt invocation can't
# wedge the agent loop. Five minutes is generous for typical 30s clips.
_DEFAULT_CLI_TIMEOUT_SEC = 300.0


class MagentaRealtimeBackend(BaseBackend):
    """Audio generation via Magenta RealTime."""

    name: str = "magenta_rt"
    install_hint: str | None = (
        "see https://github.com/magenta/magenta-realtime; then either set "
        "JG_MAGENTA_RT_CLI=<path-to-cli> or `pip install -e .` the upstream "
        "repo so `magenta_rt` imports cleanly"
    )

    @classmethod
    def _probe(cls) -> bool:  # type: ignore[override]
        cli = get_settings().jg_magenta_rt_cli.strip()
        # shlex.split tolerates quoted paths with spaces; plain str.split
        # would shred `"/opt/Magenta RT/bin/magenta-rt"`. Malformed quoting
        # raises ValueError — treat that as "no usable CLI configured" so
        # the Python-backend fallback still gets a chance to run.
        try:
            cli_argv = shlex.split(cli) if cli else []
        except ValueError:
            cli_argv = []
        if cli_argv and shutil.which(cli_argv[0]):
            return True
        try:
            import magenta_rt  # type: ignore[import-not-found]  # noqa: F401
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

    async def _run_cli(
        self, cli: str, request: MusicGenerationRequest, output_path: Path
    ) -> tuple[int, str]:
        """Shell out to a user-configured magenta-rt CLI.

        Bounded by ``_DEFAULT_CLI_TIMEOUT_SEC``; on timeout we kill, reap,
        and return exit code 124 so the orchestrator can surface the
        condition as a warning rather than hanging.
        """
        argv = [
            *shlex.split(cli),
            "--prompt",
            request.prompt,
            "--duration",
            str(request.duration_sec),
            "--output",
            str(output_path),
        ]
        if request.seed is not None:
            argv += ["--seed", str(request.seed)]
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
            return 124, f"magenta-rt CLI timed out after {_DEFAULT_CLI_TIMEOUT_SEC:.0f}s"
        return proc.returncode or 0, err.decode("utf-8", errors="replace")

    def _run_python(
        self, request: MusicGenerationRequest, output_path: Path
    ) -> None:
        """Call ``magenta_rt.compose``; raises on failure. Monkeypatched in tests."""
        from magenta_rt import compose  # type: ignore[import-not-found]

        try:
            compose(
                prompt=request.prompt,
                duration_sec=request.duration_sec,
                output=str(output_path),
                seed=request.seed,
            )
        except TypeError:
            # Older signature variant.
            compose(request.prompt, str(output_path), request.duration_sec)

    # ------------------------------------------------------------------
    # public protocol method
    # ------------------------------------------------------------------

    def _default_output(self) -> Path:
        # Generation outputs sit under <workspace>/generation/<timestamp>.wav
        # by default. The tool layer can pin a session-scoped path via
        # ``MusicGenerationRequest.output_path``.
        s = get_settings()
        ts = int(time.time() * 1000)
        out_dir = s.jg_workspace_dir / "generation"
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir / f"magenta_rt_{ts}.wav"

    def generate_audio(self, request: MusicGenerationRequest) -> MusicGenerationResult:
        if not self.is_available():
            raise self._unavailable(
                "neither JG_MAGENTA_RT_CLI nor the `magenta_rt` python package is available"
            )

        output_path = Path(request.output_path) if request.output_path else self._default_output()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        warnings: list[str] = []

        cli = get_settings().jg_magenta_rt_cli.strip()
        try:
            cli_argv = shlex.split(cli) if cli else []
        except ValueError as exc:
            cli_argv = []
            warnings.append(
                f"invalid JG_MAGENTA_RT_CLI shell quoting ({exc}); "
                "falling back to the python backend"
            )
        if cli_argv and shutil.which(cli_argv[0]):
            # See note in MT3Backend.transcribe_to_midi — the helper avoids
            # ``RuntimeError: asyncio.run() cannot be called from a running
            # event loop`` if a caller invokes this from async context.
            rc, err = run_coro_sync(self._run_cli(cli, request, output_path))
            if rc != 0:
                return MusicGenerationResult(
                    backend=self.name,
                    output_path=output_path,
                    duration_sec=0.0,
                    warnings=[f"magenta-rt CLI exit {rc}: {err.strip()[:400]}"],
                )
            model_name = f"magenta_rt cli ({cli_argv[0]})"
        else:
            try:
                self._run_python(request, output_path)
            except Exception as exc:  # pragma: no cover - depends on optional dep
                return MusicGenerationResult(
                    backend=self.name,
                    output_path=output_path,
                    duration_sec=0.0,
                    warnings=[f"magenta_rt.compose failed: {exc}"],
                )
            model_name = "magenta_rt (python)"

        if not output_path.exists():
            warnings.append(f"backend did not write {output_path}")

        return MusicGenerationResult(
            backend=self.name,
            output_path=output_path,
            duration_sec=request.duration_sec,
            model_name=model_name,
            warnings=warnings,
        )
