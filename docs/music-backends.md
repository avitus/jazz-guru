# Modular music backends

`jazz-guru` keeps a single LLM (Anthropic Claude) as the agent brain. For
music-specific work — transcription, chord/beat analysis, music
understanding, generation — it delegates to a swappable set of
**specialised backends** behind a clean adapter surface. No single music
foundation model is mandatory, and every optional backend lazy-imports
its dependency so the harness keeps booting if a model is not installed.

```text
LLM agent brain  (anthropic claude)
  + music-theory tools          : music21, MIDI, MusicXML (built-in)
  + transcription backend       : Basic Pitch | MT3 | …
  + chord / beat analysis       : Omnizart | librosa baseline
  + music understanding         : Music Flamingo | …
  + optional generation         : Magenta RealTime | ElevenLabs Music
```

## Why specialised backends, not one monolithic model

* No single open music model covers transcription + chords + structure +
  understanding + generation well. Coupling the agent to one of them
  would leave too much capability on the table.
* Different roles have different cost/latency/quality trade-offs. Basic
  Pitch is tiny and CPU-friendly; MT3 is heavy and JAX-bound; Music
  Flamingo wants a GPU or hosted endpoint. The adapter layer keeps the
  agent code identical regardless.
* Practice-take feedback should still work when *zero* optional backends
  are installed — the librosa baseline gives tempo and key, and missing
  pieces show up as warnings the LLM can describe to the user.

## Recommended backend order

Enable in this order as you install optional dependencies. Each one
unlocks the next layer of the practice-take pipeline.

1. **Basic Pitch** — Spotify's audio→MIDI model. Small, runs on CPU,
   works well for monophonic / lightly-polyphonic acoustic recordings
   like a saxophone take.
   ```bash
   pip install 'basic-pitch>=0.4'
   ```
2. **Omnizart** — chord + beat ASR (TensorFlow-based, CPU-friendly).
   The model checkpoints are not bundled with the wheel; you have to
   pull them on first install:
   ```bash
   pip install omnizart
   omnizart download-checkpoints
   ```
3. **MT3** — denser-material transcription. Magenta's MT3 has no
   stable Python API; this adapter supports two paths:
   - **CLI**: point `JG_MT3_CLI` at any wrapper that accepts
     `--input <audio> --output <midi>`.
   - **Python**: if `import mt3.inference` succeeds, the adapter
     uses it directly. See
     [MT3](https://github.com/magenta/mt3) for the JAX/T5X install.
4. **Music Flamingo** — music-understanding LLM (NVIDIA Audio Flamingo
   3) for free-text descriptions ("medium-swing ballad in E♭, soloist
   mostly stays inside chord tones…").
   ```bash
   pip install 'jazz-guru[audio-ml]'   # transformers + torch
   ```
   Configure the HF model id with `MUSIC_FLAMINGO_MODEL`
   (default: `nvidia/audio-flamingo-3-hf`; use the `-chat` variant for
   the instruction-tuned model).
5. **Magenta RealTime** — local real-time music generation. Like MT3,
   no stable Python API; the adapter supports either a CLI subprocess
   (`JG_MAGENTA_RT_CLI=<path-to-wrapper>`) or an `import magenta_rt`
   path.
6. **ElevenLabs Music** — hosted music generation.
   ```bash
   pip install elevenlabs
   # then set ELEVENLABS_API_KEY in .env
   ```
   The default model id is `music_v1`; override with
   `ELEVENLABS_MUSIC_MODEL`.

## Configuration

Each role is selected via an env var on `.env`:

```ini
MUSIC_TRANSCRIPTION_BACKEND=basic_pitch   # or: none | mt3
MUSIC_ANALYSIS_BACKEND=librosa            # or: omnizart | none
MUSIC_UNDERSTANDING_BACKEND=none          # or: librosa | music_flamingo
MUSIC_GENERATION_BACKEND=none             # or: magenta_rt | elevenlabs_music
```

Defaults are conservative: only `librosa` is on, so a fresh install
returns key + tempo + beats with zero optional packages.

To list every known backend and whether its dependency is loadable:

```bash
.venv/bin/jazz-guru analyze-take some.wav   # prints a backend table
```

…or programmatically:

```python
from jazz_guru.music import available_backends
print(available_backends())
```

## Running an analysis

```bash
.venv/bin/jazz-guru analyze-take solo.wav \
    --chart "Autumn Leaves" \
    --instrument tenor-sax \
    --tempo 132 \
    --key "G minor"
```

The output JSON contains a `summary` line the LLM can paste verbatim,
plus typed sub-objects for transcription, beat tracking, chord
analysis, timing notes, pitch notes, and any warnings for unavailable
optional backends.

The same workflow is exposed to the agent itself as the
`analyze_practice_take` tool:

```jsonc
{
  "name": "analyze_practice_take",
  "arguments": {
    "audio_path": "takes/solo.wav",
    "chart": "Autumn Leaves",
    "instrument": "tenor-sax",
    "expected_key": "G minor",
    "expected_tempo_bpm": 132
  }
}
```

## Adding a new backend

1. Create a module under `src/jazz_guru/music/analysis/` (or
   `.../generation/`) that inherits from
   `jazz_guru.music.interfaces.BaseBackend` and implements the role's
   protocol method (e.g. `transcribe_to_midi`).
2. Lazy-import the optional dependency inside `_probe()` so the package
   import never fails.
3. Add a branch to the matching `get_*_backend(...)` selector in
   `src/jazz_guru/music/registry.py` and to `available_backends()`.
4. Document the env-var value in `.env.example` and in the "Recommended
   backend order" list above.
5. Mock the dependency in tests; no real-model loads in CI.

## Phase plan

* **Phase 1** — interfaces, models, config, Basic Pitch adapter,
  librosa analysis baseline, `analyze_practice_take` tool +
  `analyze-take` CLI, stubs for Omnizart / MT3 / Music Flamingo /
  Magenta RT / ElevenLabs Music, tests, docs.
* **Phase 2 (this PR)** — real Omnizart, MT3, and Music Flamingo
  adapters. Omnizart drives chord + beat ASR through
  `omnizart.chord.app.ChordTranscription` and
  `omnizart.beat.app.BeatTranscription` and parses their CSV outputs.
  MT3 supports both a CLI subprocess (`JG_MT3_CLI`) and the upstream
  `mt3.inference` python entry point. Music Flamingo loads
  `AudioFlamingo3ForConditionalGeneration` + `AutoProcessor` via
  `transformers`, applies the chat template, and scrapes
  key/tempo/time-signature out of the free-text reply.
* **Phase 3 (this PR)** — real Magenta RealTime + ElevenLabs Music
  generation adapters; deterministic backing-track builder
  (`build_backing_track` tool) that turns a chord progression into a
  piano + bass MIDI via music21; MusicXML exercise generator
  (`generate_exercise` tool) for scales, arpeggios, and ii-V-I
  skeletons; per-note timing & pitch feedback when a transcription is
  available (onset-to-beat drift, in-scale vs chromatic tally); new
  `generate_music` agent tool that delegates to the configured
  generation backend. Robust chord-symbol normaliser (`BbMaj7`, `B♭M7`,
  `C△7` → music21-compatible `B-maj7` / `Cmaj7`).
