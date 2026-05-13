# jazz-guru

Local agent harness oriented around music (MusicXML, MIDI, WAV/FLAC) and text. Modular components: context construction, memory retrieval, action control (with code generation), state externalization, distillation loop, and logging/eval. Anthropic Claude for the LLM, Postgres + pgvector for memory and event log, optional Redis + RQ for background distillation, FastAPI for the server.

No Docker required.

## Quick start (macOS)

```bash
./scripts/setup.sh         # installs Postgres+pgvector, Redis, FluidSynth via Homebrew,
                           # creates the DB + extension, sets up the venv, runs alembic
$EDITOR .env               # paste your ANTHROPIC_API_KEY (and VOYAGE_API_KEY if you want real embeddings)
make ping                  # verify Anthropic connectivity
make server                # FastAPI + websocket on http://127.0.0.1:8000
```

Single-shot chat:
```bash
.venv/bin/jazz-guru chat "compose a 4-bar Cmaj7 arpeggio as out/arp.mid"
```

## Manual setup (no Homebrew)

1. Install Postgres 14+ with the [pgvector](https://github.com/pgvector/pgvector) extension and start it.
2. Install Redis (only if you want the background worker; everything else runs without it).
3. Create the database:
   ```bash
   createdb jazz_guru
   psql jazz_guru -c "CREATE EXTENSION vector;"
   ```
4. Python deps:
   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -e ".[dev]"
   alembic upgrade head
   cp .env.example .env  # then edit
   ```

## Layout

- `config/goal.md`, `config/goal.yaml` — configurable north star, structured objectives.
- `config/policy.yaml` — tool allow/deny + budgets (all auto-approved in v1).
- `src/jazz_guru/context/` — prompt assembly.
- `src/jazz_guru/memory/` — pgvector store + summarizer + Voyage/hash-stub embeddings.
- `src/jazz_guru/actions/` — tool registry, controller, built-in tools (incl. `analyze_practice_take`).
- `src/jazz_guru/music/` — modular music-backend layer (transcription, chord/beat, music understanding, generation).
- `src/jazz_guru/state/` — schema, event log, snapshots, externalized self-model.
- `src/jazz_guru/distillation/` — reflexion loop + playbook + scheduler.
- `src/jazz_guru/logging/` — structured logs + JSONL traces + trace viewer.
- `src/jazz_guru/eval/` — trace replay, LLM-as-judge, regression suite.
- `src/jazz_guru/io/` — text + audio adapters.

## Music backends

The agent delegates music-specific work to a swappable adapter layer:
Basic Pitch / MT3 (transcription), Omnizart / librosa baseline (chords +
beats), Music Flamingo (music understanding), Magenta RealTime /
ElevenLabs Music (optional generation). Optional dependencies
lazy-import, so the harness boots regardless of which models are
installed. See [docs/music-backends.md](docs/music-backends.md) for the
architecture, install order, and the `jazz-guru analyze-take` CLI.

## CLI

```
jazz-guru info           # resolved configuration
jazz-guru ping           # check Anthropic
jazz-guru goal           # show the rendered system block
jazz-guru tools          # list registered tools
jazz-guru new-session    # mint a session id
jazz-guru chat <msg>     # one turn (use --session to continue)
jazz-guru trace <id>     # dump JSONL trace for a session
jazz-guru distill <id>   # run reflexion (use --sync to skip Redis)
jazz-guru evalrun        # run the regression suite
jazz-guru viewer         # local trace viewer on :8765
jazz-guru analyze-take audio.wav --chart "Autumn Leaves" --instrument tenor-sax  # music backend pipeline
```

## Make targets

```
make setup       # one-shot bootstrap (calls scripts/setup.sh)
make install     # venv + python deps
make migrate     # alembic upgrade head
make test        # pytest
make lint        # ruff
make typecheck   # mypy
make server      # FastAPI
make worker      # RQ worker (needs Redis)
make viewer      # trace viewer
```

## Without Redis

Redis is only used by the background worker for async distillation and scheduled eval. Everything else works without it. To distill or run eval inline:

```bash
jazz-guru distill <session-uuid> --sync
jazz-guru evalrun
```

Leave `REDIS_URL` blank in `.env` to make the intent explicit.
