from __future__ import annotations

import sys
import types
from pathlib import Path

import numpy as np
import pytest


class _FakeStream:
    def __init__(self, *, callback, samplerate, channels, blocksize, **_kw) -> None:
        self.callback = callback
        self.samplerate = samplerate
        self.channels = channels
        self.blocksize = blocksize
        self._running = False

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def close(self) -> None:
        self._running = False

    def push(self, audio: np.ndarray) -> None:
        if not self._running:
            return
        self.callback(audio, len(audio), None, None)


@pytest.fixture
def fake_sd(monkeypatch: pytest.MonkeyPatch):
    """Stub out the sounddevice module so audio tests don't require a real mic."""
    captured: dict[str, object] = {}

    class FakeSd(types.ModuleType):
        def __init__(self) -> None:
            super().__init__("sounddevice")
            self.last_stream: _FakeStream | None = None

        def InputStream(self, **kw):
            self.last_stream = _FakeStream(**kw)
            captured["stream"] = self.last_stream
            return self.last_stream

        def query_devices(self):
            return [
                {"name": "fake mic", "max_input_channels": 1, "default_samplerate": 44100.0},
            ]

        def rec(self, frames, *, samplerate, channels, dtype, device=None):
            return np.zeros((frames, channels), dtype="float32")

        def wait(self) -> None:
            return None

    fake = FakeSd()
    monkeypatch.setitem(sys.modules, "sounddevice", fake)
    return fake, captured


def test_list_input_devices(fake_sd) -> None:
    from jazz_guru.client.audio import list_input_devices

    devs = list_input_devices()
    assert len(devs) == 1
    assert devs[0]["name"] == "fake mic"


def test_record_fixed_writes_wav(fake_sd, tmp_path: Path) -> None:
    from jazz_guru.client.audio import record_fixed

    p = record_fixed(tmp_path / "out.wav", seconds=0.1, samplerate=8000, channels=1)
    assert p.exists()
    import soundfile as sf

    audio, sr = sf.read(str(p))
    assert sr == 8000
    assert len(audio) == 800


def test_push_to_talk_records_and_saves(fake_sd, tmp_path: Path) -> None:
    from jazz_guru.client.audio import PushToTalk

    _fake, captured = fake_sd
    ptt = PushToTalk(samplerate=8000, channels=1, blocksize=400)
    ptt.start()
    stream: _FakeStream = captured["stream"]  # type: ignore[assignment]
    chunk = (np.random.randn(400, 1) * 0.1).astype("float32")
    stream.push(chunk)
    stream.push(chunk)
    out = ptt.stop_and_save(tmp_path / "ptt.wav")
    assert out.exists()
    import soundfile as sf

    audio, sr = sf.read(str(out))
    assert sr == 8000
    assert len(audio) == 800
