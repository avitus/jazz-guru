"""Native microphone capture using sounddevice (PortAudio).

Modes:
- :func:`record_fixed` — record N seconds, write a WAV.
- :class:`PushToTalk` — start/stop manually (e.g. spacebar in TUI).
- :class:`VadStreamer` — continuous stream that yields utterance WAVs separated by silence.
"""
from __future__ import annotations

import contextlib
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf


def _import_sd() -> Any:
    import sounddevice as sd  # type: ignore[import-untyped]

    return sd


def list_input_devices() -> list[dict[str, Any]]:
    sd = _import_sd()
    devs = sd.query_devices()
    out: list[dict[str, Any]] = []
    for i, d in enumerate(devs):
        if d.get("max_input_channels", 0) > 0:
            out.append(
                {
                    "index": i,
                    "name": d["name"],
                    "channels": d["max_input_channels"],
                    "samplerate": int(d["default_samplerate"]),
                }
            )
    return out


def record_fixed(
    out_path: str | Path,
    *,
    seconds: float = 5.0,
    samplerate: int = 44100,
    channels: int = 1,
    device: int | None = None,
) -> Path:
    sd = _import_sd()
    frames = int(seconds * samplerate)
    audio = sd.rec(frames, samplerate=samplerate, channels=channels, dtype="float32", device=device)
    sd.wait()
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(p), audio, samplerate, subtype="PCM_16")
    return p


@dataclass
class PushToTalk:
    """Manual start/stop recorder. Thread-safe."""

    samplerate: int = 44100
    channels: int = 1
    device: int | None = None
    blocksize: int = 1024
    _q: queue.Queue[np.ndarray] = field(default_factory=queue.Queue, repr=False)
    _stream: Any = field(default=None, repr=False)
    _started_at: float | None = field(default=None, repr=False)
    _last_level: float = field(default=0.0, repr=False)

    def _callback(self, indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        if status:
            # log via stderr in CLI; TUI sees status events through a separate hook
            pass
        chunk = indata.copy()
        self._q.put(chunk)
        # crude RMS for level meter
        self._last_level = float(np.sqrt(np.mean(chunk * chunk)))

    @property
    def is_recording(self) -> bool:
        return self._stream is not None

    @property
    def level(self) -> float:
        return self._last_level

    @property
    def elapsed_sec(self) -> float:
        return 0.0 if self._started_at is None else time.monotonic() - self._started_at

    def start(self) -> None:
        if self.is_recording:
            return
        sd = _import_sd()
        self._q = queue.Queue()
        self._started_at = time.monotonic()
        self._stream = sd.InputStream(
            samplerate=self.samplerate,
            channels=self.channels,
            dtype="float32",
            device=self.device,
            blocksize=self.blocksize,
            callback=self._callback,
        )
        self._stream.start()

    def stop_and_save(self, out_path: str | Path) -> Path:
        if not self.is_recording:
            raise RuntimeError("not recording")
        self._stream.stop()
        self._stream.close()
        self._stream = None
        self._started_at = None

        chunks: list[np.ndarray] = []
        while not self._q.empty():
            chunks.append(self._q.get_nowait())
        if not chunks:
            audio = np.zeros((0, self.channels), dtype="float32")
        else:
            audio = np.concatenate(chunks, axis=0)
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(p), audio, self.samplerate, subtype="PCM_16")
        return p


@dataclass
class VadStreamer:
    """Continuous capture that emits utterance WAVs separated by silence.

    Energy-based VAD: simple but adequate for desk-mic use. Tune ``rms_threshold``
    and ``silence_ms`` per environment.
    """

    samplerate: int = 16000
    channels: int = 1
    device: int | None = None
    rms_threshold: float = 0.015
    silence_ms: int = 700
    min_utterance_ms: int = 400
    blocksize: int = 1024
    on_utterance: Callable[[Path], None] | None = None
    out_dir: Path | None = None
    _stream: Any = field(default=None, repr=False)
    _buf: list[np.ndarray] = field(default_factory=list, repr=False)
    _silence_blocks: int = field(default=0, repr=False)
    _running: bool = field(default=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _flush(self) -> Path | None:
        if not self._buf:
            return None
        audio = np.concatenate(self._buf, axis=0)
        self._buf.clear()
        ms = (audio.shape[0] / self.samplerate) * 1000
        if ms < self.min_utterance_ms:
            return None
        ts = int(time.time() * 1000)
        out = (self.out_dir or Path(".")) / f"utt_{ts}.wav"
        out.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(out), audio, self.samplerate, subtype="PCM_16")
        if self.on_utterance is not None:
            with contextlib.suppress(Exception):
                self.on_utterance(out)
        return out

    def _callback(self, indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        rms = float(np.sqrt(np.mean(indata * indata)))
        with self._lock:
            if rms >= self.rms_threshold:
                self._buf.append(indata.copy())
                self._silence_blocks = 0
            elif self._buf:
                self._buf.append(indata.copy())
                self._silence_blocks += 1
                blocks_per_sec = self.samplerate / self.blocksize
                if self._silence_blocks > (self.silence_ms / 1000.0) * blocks_per_sec:
                    self._flush()
                    self._silence_blocks = 0

    def start(self) -> None:
        if self._running:
            return
        sd = _import_sd()
        self._running = True
        self._stream = sd.InputStream(
            samplerate=self.samplerate,
            channels=self.channels,
            dtype="float32",
            device=self.device,
            blocksize=self.blocksize,
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> Path | None:
        if not self._running:
            return None
        self._stream.stop()
        self._stream.close()
        self._stream = None
        self._running = False
        with self._lock:
            return self._flush()
