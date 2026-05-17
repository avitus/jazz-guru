# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`jazz-guru` is a local agent harness for music + text work (MusicXML/MIDI/WAV) built on Anthropic Claude. It is a Python 3.12 package distributed as console scripts (`jazz-guru`, `jazz-guru-server`, `jazz-guru-worker`) backed by Postgres + pgvector for memory/event log, with optional Redis + RQ for background reflexion/eval.

There is no Docker. Local Postgres, Redis, FluidSynth, and ffmpeg are expected on the host (Homebrew on macOS — see `scripts/setup.sh`).

## Commands

```bash
make setup          # one-shot: brew deps + venv + DB + alembic (calls scripts/setup.sh)
make install        # venv + python deps only
make migrate        # alembic upgrade head
make test           # pytest -q
make lint           # ruff check
make format         # ruff format
make typecheck      # mypy
make server         # FastAPI + websocket on 127.0.0.1:8000
make worker         # RQ worker (needs Redis)
make viewer         # local trace viewer on :8765
make tui            # Textual TUI client (server must be running)
```

Single test (the test files all live under `tests/unit/`):

```bash
.venv/bin/pytest tests/unit/test_dynamic_tools.py::test_invoke_subprocess_round_trip -q
```

Operational CLI (use this for ad-hoc agent work, not curl):

```bash
.venv/bin/jazz-guru info | tools | goal | ping
.venv/bin/jazz-guru new-session
.venv/bin/jazz-guru chat "<msg>" [--session <uuid>]
.venv/bin/jazz-guru distill <session-uuid> [--sync]   # reflexion loop
.venv/bin/jazz-guru session close <session-uuid> [--sync]   # end-of-session signal; auto-distill via trigger funnel
.venv/bin/jazz-guru evalrun [--only <task-id>]        # regression suite
.venv/bin/jazz-guru trace <session-uuid>              # JSONL trace dump
.venv/bin/jazz-guru tool list                         # Tier-2 tools w/ version + test count
.venv/bin/jazz-guru tool show <name>                  # source, schema, tests, version history
.venv/bin/jazz-guru tool test <name> [--case <c>]     # run a tool's test suite
.venv/bin/jazz-guru tool diff <name> <v1> [<v2>]      # diff between versions (v2 omitted = current)
.venv/bin/jazz-guru tool rollback <name> --to <v>     # restore a historical version
.venv/bin/jazz-guru tool unlock <name>                # clear improve_locked (operator only)
```

`pytest` is configured with `asyncio_mode = "auto"`; do not add `@pytest.mark.asyncio` decorators unless a specific test needs them. Ruff lint set: `E,F,I,B,UP,SIM,RUF` (E501 ignored, line-length 100). Mypy is non-strict (`disallow_untyped_defs = false`, `check_untyped_defs = true`).

## Configuration

Three config sources, all centralized in `src/jazz_guru/config.py` (cached via `lru_cache`):

- **`.env`** — loaded by pydantic-settings. Required for any LLM call: `ANTHROPIC_API_KEY`. `VOYAGE_API_KEY` is optional; with it blank the embedding store falls back to a deterministic hash-stub (tests rely on this).
- **`config/goal.md` + `config/goal.yaml`** — the agent's north star. `goal.md` is freeform prose; `goal.yaml` adds structured `objectives`, `constraints`, `success_criteria`, `style`. Both are concatenated by `GoalConfig.render_system_block()` and prepended to every system prompt.
- **`config/policy.yaml`** — per-tool allow/deny + budgets. `ActionController` filters the registry by `policy.for_tool(name).mode == "allow"` AND, if `feature_flag` is set, the corresponding env flag (e.g. `FEATURE_TTS=1`) must be truthy. `budgets.per_turn.tool_calls` doubles as the agent loop's `max_rounds`.

Settings cache is process-wide. If you change `.env` or any YAML inside a long-lived process (server, worker), the cache must be cleared (`get_settings.cache_clear()` etc.) or the process restarted.

## Architecture

The agent loop is a textbook perceive → plan → act → observe cycle. Stages live in their own subpackages under `src/jazz_guru/`:

- **`harness/loop.py` — `AgentLoop.step()`** is the seam everything else feeds into. One call per user turn:
  1. Records a `user` Turn row.
  2. Pulls retrieved memory (`memory.MemoryStore.search`) + playbook (top-N `PlaybookEntry`) + the latest snapshot via `state.load_latest`.
  3. Builds the prompt with `context.ContextBuilder` (goal block + tool-creation hint + state doc + playbook + retrieved memory + history).
  4. Hydrates the session-local `DynamicRegistry` from globally-published tools (`actions.store.load_all_specs`), attaches it to the static `registry`, and recomputes `controller.allowed`.
  5. Runs `ActionController.run` until the model returns `stop_reason != "tool_use"` or budgets are exhausted.
  6. Records the `assistant` Turn, writes a memory item summarizing the exchange, snapshots state, emits `turn_start`/`turn_end` events.

- **`actions/` — tool registry + controller.**
  - `registry.py` is a single global `ToolRegistry`; tools register via `@registry.register(name, description=, input_model=)`. `register_all()` imports every tool module so the decorators run; calling it is idempotent.
  - `controller.ActionController.run` drives the Anthropic tool_use loop, enforcing `policy.budgets.per_turn.tool_calls` as `max_rounds` AND as the per-turn tool-call cap. Each step emits `llm_request`/`llm_response`/`tool_use`/`tool_result`/`error` via the optional `on_event` callback (the trace writer and the WS server both subscribe).
  - `tools/*.py` are the 13 built-in tools (fs/shell/http/python_exec/code_gen/web_search/vision/audio_analyze/music_xml/midi/render/tts) plus `tool_meta.py` (the meta-tools that let the agent author its own tools).
  - `dynamic.py` + `store.py` implement the **3-tier dynamic-tool system**:
    - **Tier 1 (session)** — `tool_create` validates name/schema/source, writes `<workspace>/sessions/<sid>/tools/<name>.py`, and adds a `DynamicSpec` to the session's `DynamicRegistry`. Default execution is a **`python -I` subprocess** (cwd = session workspace, stdin = JSON kwargs, stdout = JSON result, timeout from `python_exec` policy). `inproc` is also supported for trusted helpers.
    - **Tier 2 (global)** — `tool_publish(name)` upserts the tool into the `generated_tools` table; future sessions get it automatically when `_hydrate_dynamic_registry` runs. Each upsert snapshots the prior row into `generated_tool_versions` for rollback. The reflexion loop will propose patches for Tier-2 tools that have at least one test attached (see `testing/` below); tools without tests are skipped silently.
    - **Tier 3 (source)** — `tool_promote_to_source(name)` writes the file into `src/jazz_guru/actions/tools/`. Requires server restart to take effect; tier 2 vs tier 3 is a deliberate distinction — Tier 3 is the manual-edit floor, not subject to automatic improvement.
  - `sandbox.resolve_in_workspace` is the only sanctioned way to convert a user/tool-supplied path into a real one — it refuses anything outside `<workspace>/sessions/<sid>/`. All filesystem-touching tools must use it.
  - `context.py` carries the per-call `ToolContext` (session id, turn idx) via a contextvar so tools can find their session workspace without threading it through arguments.

- **`state/` — durable state.**
  - `schema.py` defines all SQLAlchemy ORM models: `Session`, `Turn`, `Event`, `Snapshot`, `MemoryItem` (with a pgvector `Vector` column sized to `settings.embedding_dim`), `PlaybookEntry`, `GeneratedTool`, `GeneratedToolVersion` (history snapshots), `GeneratedToolTest` (per-tool test suite), `GeneratedToolTestRun` (test execution log), `EvalRun`. Migrations live in `alembic/`.
  - `externalize.StateDoc` is the "self-model" that gets injected into every system prompt. It is rebuilt from the latest snapshot on disk (`workspace/state/<sid>/latest.json`) plus the live artifact list (`list_session_artifacts` walks `workspace/sessions/<sid>/`).

- **`memory/`** — pgvector store + history summarizer + Voyage embeddings (with hash-stub fallback when no key is set). Search/write are both async; failures are swallowed and logged so a memory outage does not break a turn.

- **`actions/tools/render.py`** + **`presets.py`** — multi-engine MIDI→audio renderer plus a typed preset surface. Three engines (`fluidsynth` SF2/SF3, `sfizz` SFZ via `sfizz_render`, `liquidsfz` SFZ via `liquidsfz`) plus an ffmpeg post-processing chain (`lowpass`, `vibrato`, `volume`, `loudnorm`). Presets live in **`data/instruments.yaml`** — the agent mutates them via the `preset_list` / `preset_get` / `preset_upsert` / `preset_delete` tools (validated, atomic-write); never via `fs_*` or `shell`. `src/jazz_guru/presets.py` is the only sanctioned read/write path; `render_midi` reloads on every call (mtime-cached). Library paths in the YAML resolve against `JG_INSTRUMENTS_ROOT` (default `~/.local/share/jazz-guru/instruments`). Per-render `post_process` overrides only the fields it sets; everything else falls through to preset defaults.

- **`distillation/reflexion.py`** — offline reflexion loop. `run_reflexion(session_id)` summarizes the transcript, prompts Claude (with strict JSON contract) for `{score, critique, revised_plan, open_threads, memory_writes, playbook_entries}`, and writes the durable bits back into memory + the playbook table + a fresh snapshot. Triggered async via Redis/RQ (`reflexion_job`) by `make worker`, or inline via `--sync`. After the main reflexion work, `_run_improvement_pass(sid)` mines the trace via `testing.failure_signals` and dispatches `distillation.improver.maybe_improve` for each tool that crossed its threshold (capped per run by `jg_improver_max_per_run`).

- **`distillation/auto.py`** — auto-distillation triggers. Three signals (explicit `close` endpoint/CLI, RQ `sweep_job` idle tick, and a `new_session` predecessor scan fired on every `POST /sessions`) funnel through `maybe_trigger`, which dedups on event markers (`reflexion` / `distillation_queued` / `distillation_inline`) vs the newest assistant turn and serialises concurrent fires per session via a Postgres advisory lock. When Redis is down, falls back to running reflexion inline up to `jg_distill_inline_max_per_process` (default 1, resets on restart). Knobs: `jg_distill_on_close`, `jg_distill_on_new_session`, `jg_distill_sweep_interval_sec` (300 s), `jg_distill_idle_sec` (600 s).

- **`testing/` — Tier-2 tool tests + improvement (see `docs/plans/tier2-tool-tests-and-improvement.md` for the full design).**
  - `predicates.py` — deterministic JSON-tree predicate DSL. Ops: `eq`, `ne`, `gt/gte/lt/lte`, `len` (scalar or nested), `contains`, `regex`, `type`, `absent`, `present`, `all`, `any`. Path syntax with dots, `[N]`, and `[*]` quantifier. No `eval`.
  - `runner.py` — executes a test case against a `DynamicSpec` via the same subprocess sandbox as the tool itself. Reuses `eval.judge` for optional rubric scoring.
  - `failure_signals.py` — trace-mining for the improvement loop. Detects three failure modes from `tool_result` events: handler-raised (`ok: False`), policy denial (bare `error`), and dynamic-tool subprocess errors (`result_has_error` — the controller emits this when a tool returns `{"__error__": "..."}`).
  - **Agent-facing meta-tools** in `actions/tools/tool_test_meta.py`: `tool_test_add` / `tool_test_remove` / `tool_test_list` / `tool_test_run`. Policy entries under the `testing` toolset. `tool_publish` auto-records a smoke case from the most recent successful invocation in the session trace.

- **`distillation/improver.py`** — the auto-publish gate (plan §B.3-B.5). `maybe_improve(name, failures)` calls Claude with a strict-JSON contract (`{source, rationale, new_test_cases, schema_unchanged}`), runs the full existing suite + new cases against the candidate, and only commits via `store.upsert(origin="improver")` when everything passes. `store.upsert` automatically snapshots the prior row into `generated_tool_versions` for one-row rollback. Lock breakers: per-tool `consecutive_failures` counter; reaching `MAX_ATTEMPTS=3` sets `improve_locked`, cleared only via `jazz-guru tool unlock`.

- **`server.py`** — FastAPI app. Routes: `POST /sessions`, `POST /sessions/{id}/chat`, `WS /ws/sessions/{id}/chat` (streams every controller event over the socket), `POST /sessions/{id}/distill`, `POST /sessions/{id}/close` (end-of-session auto-distill via the trigger funnel), `POST /memory/search`, `POST /eval/run`, `GET /mcp/status`, `POST /mcp/reload`, artifact list/download, file uploads. The HTML UI is served from `src/jazz_guru/web/static/` at `/ui/`, the user manual at `/user_manual/`.

- **`blocks/jazz_guru/`** — optional PubNub Blocks Network adapter. `handler.py` exposes the same skills (`chat` / `distill` / `evalrun` / `render_midi`) as the FastAPI server, routed via a `skill` discriminator in the request JSON. Blocks calls the handler synchronously and drives jazz-guru's async coroutines via `asyncio.run` per request, so the handler disposes the cached SQLAlchemy engine and clears the `db.get_engine` / `get_sessionmaker` `lru_cache`s after every call (asyncpg connections are loop-bound).

- **`lickmatch.py`** + **`actions/tools/lick_match.py`** — read-only surface over the Weimar Jazz Database (`data/wjazzd/wjazzd-index.json`, ODbL). `lick_match(midi_path | notes | intervals+iois)` and `lick_match_info()` are the only sanctioned access path; the agent should not parse the index via `fs_read` / `python_exec`. Every match carries WJazzD attribution (performer + title + year) per the license. Wired into the `music` toolset.

- **`auth.py`** — optional `X-API-Key` middleware. Off unless `JG_API_KEY` is set in the env. `/`, `/health`, `/docs`, `/ui/*` are exempt. WS routes have to call `auth.require_ws(token)` themselves because Starlette middleware does not run for WebSockets.

- **`logging/`** — structlog + a per-session JSONL trace under `workspace/traces/<sid>.jsonl` written by `TraceWriter`, plus a small uvicorn-based viewer (`jazz-guru viewer`).

- **`eval/`** — trace replay + LLM-as-judge regression suite, runnable via `evalrun` or `POST /eval/run`. Tasks are loaded from `eval/tasks/`.

- **`client/`** — `sdk.py` (typed HTTP/WS client) and `tui.py` (Textual TUI with mic capture).

## Conventions for new code

- **Tools**: define a Pydantic input model for `input_model=`, register with snake_case names, and pull the session id from `actions.context.current()` rather than accepting it as an arg. Sandbox helpers in `actions/sandbox.py`:
  - **`resolve_in_workspace(path, sid)`** — strict session-only resolver. Use for **writes** and any user-supplied path that should be confined to `workspace/sessions/<sid>/`.
  - **`resolve_in_safe(path, sid)`** — read-oriented; additionally allows `data/` and any `JG_SAFE_EXTRA_PATHS`. Use when a tool legitimately reads project data (e.g. presets via `presets.load_presets()`).
  - Tools whose data lives outside the session workspace (presets, fixtures, library catalogues) should expose a dedicated typed tool surface — see `actions/tools/presets.py` for the pattern. **Never** require the agent to call `fs_write`/`shell`/`python_exec` against project data.
  - Add the tool to `config/policy.yaml` either as a per-name entry (`tools.<name>: { mode: allow }`) or by listing it in a `toolsets:` bundle (per-name wins). `register_all()` in `actions/registry.py` is the one place that imports tool modules.
  - `shell` and `python_exec` are subprocess escape hatches by design (no path filtering inside the child). Set `JG_OS_SANDBOX=1` on macOS to additionally wrap them and dynamic-tool subprocesses with `sandbox-exec -f data/sandbox/jg.sb`, which restricts writes to the session workspace and reads to workspace + `data/` + brew prefixes. The flag is opt-in; off by default.
- **Database**: use the async `db.session_scope()` context manager. The sync URL (`DATABASE_URL_SYNC`) exists for alembic only.
- **LLM calls**: route through `llm.complete` — it adds tenacity retries on connection/timeout/rate/5xx and is responsible for usage accounting (`LLMUsage` is summed into the `Turn` row).
- **Async**: pretty much everything is async (`asyncio_mode = "auto"`). The CLI wraps coroutines in `asyncio.run`. The RQ worker is the one place where we go sync→async via `asyncio.run` inside a job function.
- **Goals are configurable, not hardcoded**: keep agent personality and objectives in `config/goal.{md,yaml}`; do not bake them into prompts. The tool-creation hint is the only static block appended to the goal — see `context/builder.py:_TOOL_CREATION_HINT`.
