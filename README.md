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
jazz-guru info               # resolved configuration
jazz-guru ping               # check Anthropic
jazz-guru goal               # show the rendered system block
jazz-guru tools              # list registered tools
jazz-guru new-session        # mint a session id
jazz-guru chat <msg>         # one turn (use --session to continue)
jazz-guru trace <id>         # dump JSONL trace for a session
jazz-guru distill <id>       # run reflexion (use --sync to skip Redis)
jazz-guru session close <id> # end-of-session signal; auto-distill via the trigger funnel
jazz-guru evalrun            # run the regression suite
jazz-guru viewer             # local trace viewer on :8765
jazz-guru analyze-take audio.wav --chart "Autumn Leaves" --instrument tenor-sax  # music backend pipeline

# Tier-2 dynamic-tool operations (jazz-guru tool ...)
jazz-guru tool list                       # all published tools w/ version, test count, lock state
jazz-guru tool show <name>                # source, schema, tests, version history
jazz-guru tool test <name> [--case <c>]   # run the attached test suite
jazz-guru tool diff <name> <v1> [<v2>]    # diff between versions (v2 omitted = current)
jazz-guru tool rollback <name> --to <v>   # restore a historical version
jazz-guru tool unlock <name>              # clear improve_locked (operator only)
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

## Auto-distillation

Reflexion is triggered automatically at session boundaries — no manual
`distill` call needed. Three signals are funnelled through a single dedup
gate in `src/jazz_guru/distillation/auto.py`:

- **Explicit close** — `POST /sessions/{id}/close` or `jazz-guru session close <sid>`.
- **Idle-timeout sweep** — the RQ worker re-runs `sweep_job` every
  `JG_DISTILL_SWEEP_INTERVAL_SEC` (default 5 min) and picks up any session
  idle longer than `JG_DISTILL_IDLE_SEC` (default 10 min).
- **New-session predecessor scan** — on every `POST /sessions`, the server
  scans the DB for idle, undistilled predecessors and queues them before the
  new session's first turn.

When Redis is unavailable, the trigger falls back to running reflexion
inline up to `JG_DISTILL_INLINE_MAX_PER_PROCESS` times per process. Set
`JG_DISTILL_ON_CLOSE=false` or `JG_DISTILL_ON_NEW_SESSION=false` to opt
out of either trigger.

## Tier-2 tool tests + self-improvement

Globally-published dynamic tools can carry a test suite (predicate or rubric
cases) attached via the `tool_test_*` meta-tools. The reflexion loop mines
the trace for tool failures and, for any tool above its threshold, calls a
separate LLM proposal pass. The proposal only commits as a new version if
the full existing suite plus any new cases pass against the candidate —
otherwise the existing version keeps serving traffic. `store.upsert`
snapshots the prior row into `generated_tool_versions` automatically, so
`jazz-guru tool rollback <name> --to <v>` is one row. Tools without
attached tests are skipped silently.

Knobs: `JG_IMPROVER_THRESHOLD` (default 2), `JG_IMPROVER_MAX_PER_RUN`
(default 3), per-tool override via `tool.meta.improve_threshold`. Full
design in `docs/plans/tier2-tool-tests-and-improvement.md`.

## Blocks Network adapter

Optional alternative to the FastAPI server: `blocks/jazz_guru/handler.py`
exposes `chat` / `distill` / `evalrun` / `render_midi` over the
[PubNub Blocks](https://github.com/blocksnetwork/blocks-sdk) runtime. The
handler dispatches a `skill` discriminator in the request JSON to the
in-process jazz-guru APIs and disposes the cached SQLAlchemy engine
per request (Blocks invokes via `asyncio.run`, so loop-bound asyncpg
connections cannot be reused across calls).

## Without Redis

Redis is only used by the background worker for async distillation and scheduled eval. Everything else works without it. To distill or run eval inline:

```bash
jazz-guru distill <session-uuid> --sync
jazz-guru evalrun
```

Leave `REDIS_URL` blank in `.env` to make the intent explicit. With Redis
disabled, the auto-distillation idle sweep does not run; explicit-close and
new-session scans still work via the inline fallback (capped per process).
