# Tier-2 dynamic tools: tests + self-improvement

**Status:** In progress (PR 1 complete)
**Owner:** @avitus
**Drafted:** 2026-05-12
**Target tier:** Tier 2 (`generated_tools` table). Tier 1 already supports iteration via `tool_create` replacement; Tier 3 is deliberately out of scope (manual review floor).

## Why

Tier-2 dynamic tools today have two structural gaps:

1. **No per-tool tests.** When a tool is published, it ships with no validation. Framework tests in `tests/unit/test_dynamic_tools.py` cover the *mechanism* but never the *behaviour* of any particular tool.
2. **No improvement loop.** `store.upsert` bumps a `version` counter but discards the previous source. The reflexion loop has no schema slot for tool repair, so failing tools degrade silently.

These are addressed together because the improvement gate depends on tests existing.

## Reference: Hermes Agent

Nous Research's [hermes-agent-self-evolution](https://github.com/NousResearch/hermes-agent-self-evolution) uses DSPy + GEPA to evolve `SKILL.md` files (procedural prose), gated by a test suite + constraint checks, surfaced as PRs against the repo. They explicitly defer tool-code evolution to a later phase because mutating executable source is the hardest target. We're tackling that head-on, but only because the auto-publish gate is strict and rollback is one DB row away.

## Decisions locked in (do not re-litigate without reason)

- **Test format:** Hybrid — JSON cases with a small predicate DSL, plus optional LLM-judge rubric per case. Stored in a new `generated_tool_tests` table.
- **Improvement trigger:** Reflexion-scheduled job only. No in-session `tool_improve` meta-tool, no failure-rate watcher, no manual CLI improvement command. Trace files are the failure signal; reflexion drives the propose-test-publish loop.
- **Approval model:** Auto-publish on green. Old versions snapshot into a new `generated_tool_versions` table before each bump so rollback is one DB row away.

## Part A — Testing infrastructure

### A.1 Data model (one alembic migration)

Three new tables. `generated_tools` stays as the head/current pointer; history and tests live separately.

```sql
generated_tool_versions (
  id            UUID PK,
  tool_id       UUID FK -> generated_tools.id ON DELETE CASCADE,
  version       INT,                -- value of generated_tools.version at snapshot time
  source        TEXT,
  sha256        TEXT,
  input_schema  JSONB,
  description   TEXT,
  meta          JSONB,
  created_at    TIMESTAMPTZ,
  superseded_at TIMESTAMPTZ NULL,
  superseded_by INT NULL,
  origin        TEXT,               -- "manual" | "improver" | "rollback"
  rationale     TEXT NULL,
  UNIQUE (tool_id, version)
)

generated_tool_tests (
  id          UUID PK,
  tool_id     UUID FK -> generated_tools.id ON DELETE CASCADE,
  name        TEXT,
  spec        JSONB,                 -- parsed case+predicate+(optional)rubric
  origin      TEXT,                  -- "agent_authored" | "smoke_recorded" | "improver_added"
  enabled     BOOLEAN DEFAULT TRUE,
  created_at  TIMESTAMPTZ,
  UNIQUE (tool_id, name)
)

generated_tool_test_runs (
  id            UUID PK,
  tool_id       UUID FK,
  tool_version  INT,
  test_id       UUID FK,
  passed        BOOLEAN,
  output        JSONB,
  error         TEXT NULL,
  ms            INT,
  judge_score   FLOAT NULL,
  ran_at        TIMESTAMPTZ
)
```

`generated_tool_versions` is the rollback substrate. Every `store.upsert` snapshots the current row into `_versions` BEFORE bumping.

### A.2 Test artifact format

```yaml
# stored in generated_tool_tests.spec (JSONB)
case:
  input: {chord: "Cmaj7", style: "bebop", limit: 3}
  predicate:
    result.licks: {len: 3}
    result.licks[0].chord: "Cmaj7"
    result.licks[*].style: {eq: "bebop"}
    result.error: {absent: true}
rubric:                               # optional
  prompt: "Does the agent retrieve 3 stylistically appropriate licks?"
  criteria:
    found_correct_chord: 1.0
    style_matches: 0.8
  threshold: 0.7
predicate_source: null                # escape hatch; see A.3
timeout_sec: 10
```

AND-semantics: if both predicate and rubric exist, both must pass. Pure-predicate cases run with zero LLM cost.

### A.3 Predicate DSL (`src/jazz_guru/testing/predicates.py`)

Small, deterministic, no `eval`. JSONPath subset: dot, `[N]`, `[*]`.

```text
eq, ne                         # equality (bare scalar = implicit eq)
len: int | {gt/lt/gte/lte: int}
gt, gte, lt, lte
contains: <val> | [<vals>]
regex: "<pattern>"
type: "string|int|float|bool|array|object|null"
absent: true | present: true
all: <predicate>               # implicit on [*]
any: <predicate>
```

Escape hatch: `predicate_source` is a Python string defining `def check(result) -> bool`. Runs in the same `python -I` subprocess as the tool itself.

### A.4 Test runner (`src/jazz_guru/testing/runner.py`)

```python
async def run_test_case(spec: DynamicSpec, case: TestCase, *, judge: JudgeClient | None = None) -> TestRunResult: ...
async def run_all(spec: DynamicSpec, tests: list[TestCase]) -> list[TestRunResult]: ...
```

Routes through the existing `dynamic.invoke`. **Reuses the subprocess sandbox** — no parallel "test mode". `run_all` parallelism bounded by a small semaphore (default 4).

### A.5 Agent-facing meta-tools (`src/jazz_guru/actions/tools/tool_test_meta.py`)

- `tool_test_add(name, case_name, case_spec)` — idempotent on `(tool_id, case_name)`.
- `tool_test_remove(name, case_name)`.
- `tool_test_list(name)` — suite + last-run pass/fail per case.
- `tool_test_run(name)` — execute suite, return aggregate + per-case results.

All four added to a new `testing` toolset in `config/policy.yaml`. The tool-creation hint in `context/builder.py:_TOOL_CREATION_HINT` gets extended to encourage authoring tests before `tool_publish`.

### A.6 Auto-recorded smoke test on publish

Modify `tool_publish` (`actions/tools/tool_meta.py:191-227`): between schema validation and DB upsert, scan the current session's trace for the most recent successful invocation of this tool (`tool_use` for this name where the paired `tool_result` had no `__error__`). If found, synthesize:

```yaml
case:
  input: <args from that invocation>
  predicate:
    result: {type: "object"}
    result.__error__: {absent: true}
```

Tagged `origin="smoke_recorded"`. Always included in subsequent improvement gates. If no successful invocation found, return a warning but don't block; improvement loop will skip this tool.

### A.7 CLI surface (`jazz-guru tool ...`)

```bash
jazz-guru tool list                  # all Tier-2 tools, version, test count, last-run summary
jazz-guru tool show <name>           # source, schema, tests, version history
jazz-guru tool test <name>           # run suite, print results table
jazz-guru tool diff <name> <v1> <v2> # unified diff of source between versions
jazz-guru tool rollback <name> [--to N]
jazz-guru tool unlock <name>         # see B.7
jazz-guru tool pending               # see "Risks" — staged candidates for tools requiring live data
```

### A.8 Tests for the test infrastructure itself

- `tests/unit/test_predicates.py` — every operator + path syntax + `[*]` + `predicate_source`.
- `tests/unit/test_test_runner.py` — round-trip through actual subprocess for known-good and known-bad tools.
- `tests/unit/test_smoke_recording.py` — trace-mining including "no successful call" branch.
- `tests/unit/test_test_meta_tools.py` — `tool_test_add/list/remove/run` end-to-end against in-memory DB.

### A.9 Migration story for existing Tier-2 tools

The migration leaves existing tools with no tests. Skipped by the improvement loop until tests are added (manually or via republish). No back-population.

---

## Part B — Improvement mechanism

### B.1 Failure-signal extraction (`src/jazz_guru/testing/failure_signals.py`)

```python
def extract_tool_failures(records: list[TraceRecord]) -> dict[str, list[FailureRecord]]: ...
```

For each `tool_use` event in `workspace/traces/<sid>.jsonl`, pair with its `tool_result` by `tool_use_id`. Failure when:

1. Result contains `__error__`, OR
2. Result is empty/null and schema implied non-empty.

(Phase 2: LLM-judged negative reference in next user message. Skipped initially — expensive.)

Returns `{tool_name: [FailureRecord(input, output, error, ts, session_id, turn_idx)]}`. Pure JSONL parse, no LLM calls.

### B.2 Reflexion loop extension (`src/jazz_guru/distillation/reflexion.py`)

After existing reflexion work finishes:

```python
trace_records = load_trace(session_id)
failures_by_tool = extract_tool_failures(trace_records)
for name, failures in failures_by_tool.items():
    if len(failures) < THRESHOLD:               # default 2; per-tool meta override
        continue
    asyncio.create_task(improver.maybe_improve(name, failures))
```

`THRESHOLD=2` by default. Per-tool override in `generated_tools.meta.improve_threshold`. Improvement runs are fire-and-forget; reflexion completes regardless.

### B.3 The improver (`src/jazz_guru/distillation/improver.py`)

```python
async def maybe_improve(name: str, failures: list[FailureRecord]) -> ImproveOutcome:
    tool = await store.get(name)
    if tool is None: return SKIPPED
    tests = await store.list_tests(tool.id)
    if not tests: return SKIPPED_NO_TESTS
    if tool.meta.get("improve_locked"): return SKIPPED_LOCKED
    if tool.meta.get("consecutive_failures", 0) >= MAX_ATTEMPTS:
        await _set_locked(tool, reason="max_attempts")
        return LOCKED

    proposal = await _propose_patch(tool, tests, failures)
    if proposal is None: return PROPOSE_FAILED

    candidate = DynamicSpec(...with proposal.source...)
    new_cases = _derive_test_cases_from_failures(failures, proposal)
    full_suite = tests + new_cases

    results = await runner.run_all(candidate, full_suite)
    if all(r.passed for r in results):
        await _commit_new_version(tool, proposal, new_cases)
        return PASSED
    else:
        await _record_failed_attempt(tool, proposal, results)
        return TESTS_FAILED
```

### B.4 Proposal step (`_propose_patch`)

One `llm.complete` call. Strict-JSON contract like reflexion:

```json
{
  "source": "...",
  "rationale": "...",
  "new_test_cases": [{"name": "...", "case": {...}}],
  "schema_unchanged": true
}

Constraints:
- New source must accept the same input_schema.
- Do not introduce non-stdlib imports unless already present.
- No network or filesystem access outside the session workspace.
```

Parsed with the same defensive `_parse_json` helper.

### B.5 Auto-publish gate (`_commit_new_version`)

All must hold:

1. Every existing test passes against the candidate.
2. Every new case derived from failures passes.
3. `proposal.schema_unchanged` is true AND introspected schema matches current.
4. `validate_source(candidate.source)` passes (same path as `tool_create`).
5. Candidate sha256 differs from current.

Single async transaction:

```sql
INSERT old row → generated_tool_versions (origin="improver_superseded")
UPDATE generated_tools SET source/sha256/schema/description/version+1
INSERT new_test_cases → generated_tool_tests (origin="improver_added")
INSERT per-case runs → generated_tool_test_runs
INSERT event log → tool_improve_passed
```

Mirror to `workspace/generated_tools/<name>.py` best-effort.

### B.6 Rollback

`store.rollback(name, to_version=N)`:

- Load historical row from `_versions`.
- Snapshot CURRENT into `_versions` with `origin="rollback"`.
- Write historical source back to `generated_tools`, bump version (rollback is *forward in version space* — the v2 you roll to becomes v5, preserving monotonic version numbers).
- Emit `tool_rollback` event.

CLI: `jazz-guru tool rollback`. Agent: `tool_rollback(name, to_version)` (allowed in `policy.yaml` under `meta`).

### B.7 Locking + circuit breakers

1. **`consecutive_failures`** counter in `tool.meta`. Bumped on `TESTS_FAILED`, reset on `PASSED` or manual `tool_test_run` green. Hitting 3 sets `improve_locked=true`.
2. **`improve_locked`** flag must be cleared manually (`jazz-guru tool unlock <name>`). Agent cannot self-clear.
3. **Global rate cap**: at most `jg_improver_max_per_run` attempts per reflexion run (default 3).

### B.8 Events + telemetry

New `state.EventType` values:

- `TOOL_IMPROVE_PROPOSED` — `{name, version_current, sha256_proposed}`
- `TOOL_IMPROVE_PASSED` — `{name, version_old, version_new, n_cases, rationale}`
- `TOOL_IMPROVE_FAILED` — `{name, attempt, n_red_cases, sample_errors}`
- `TOOL_ROLLBACK` — `{name, from_version, to_version}`

Trace viewer (`jazz-guru viewer`) gets a tool-improve filter.

### B.9 Tests for the improvement system

- `tests/unit/test_failure_signals.py` — synthetic traces → expected groupings.
- `tests/unit/test_improver_gate.py` — proposal-pass / fail / schema-change-rejected / locked-skipped, with stub `complete()`.
- `tests/unit/test_version_snapshot.py` — upsert writes old row to `_versions`; rollback round-trips.
- `tests/unit/test_reflexion_integration.py` — end-to-end with mock LLM returning fixed patch.

---

## Part C — Build order, sequencing, risks

### PR sequence

| # | Scope | Risk |
|---|-------|------|
| 1 | Migration + ORM models for `_versions`, `_tests`, `_test_runs` | Low — schema-only |
| 2 | `store.upsert` snapshot wrap + `store.rollback` + `list_versions/list_tests` | Low |
| 3 | Predicate DSL + unit tests | Low — pure logic |
| 4 | Test runner + smoke-recording in `tool_publish` + meta-tools + CLI | Medium — touches agent surface |
| 5 | Failure-signal extractor + unit tests over fixture traces | Low |
| 6 | Improver module + LLM proposal contract + auto-publish gate | High — most subtle gate logic |
| 7 | Reflexion wiring + circuit breakers + event types + viewer filter | Medium — touches production loop |
| 8 | Docs pass in `CLAUDE.md` and tool-creation hint | Low |

PRs 1–4 ship value independently (existing tools become testable). PRs 5–7 are the improvement loop.

### Risks

- **LLM cost on reflexion** — each tool crossing the threshold costs one proposal + one judge call per rubric test. Bounded via global rate cap (B.7) and `jg_improver_max_per_run` setting.
- **Hidden schema drift** — model preserves `input_schema` but renames a runtime kwarg. Mitigation: schema-introspection check in B.5 + runtime sanity (at least one existing test must pass with kwargs actually supplied, not just empty input).
- **Tier-2 → Tier-3 promotion becomes lossy** — `tool_promote_to_source` refuses overwrites, ignores version history. Resolved by leaving Tier 3 as the manual-edit floor and documenting that auto-improvement is Tier-2-only. (Reinforces the existing distinction.)
- **Tests depending on external state** (SQLite lick DB, HTTP endpoint) — flaky in auto-improve. Mitigation: `meta.requires_live_data=true` per tool, which causes the improver to propose-but-not-publish; candidate surfaces as a pending row in `jazz-guru tool pending`. Small staged-review hatch under the auto-publish default.

### Effort estimate

~3 weeks single engineer. PRs 1–4 ~1 week, PRs 5–7 ~1.5 weeks (most risk concentrates here), PR 8 ~0.5 day.

---

## Progress

_Update this section as PRs land. Use the table below to track state. The "Notes" column is where to record surprises — predicates that didn't work, schema choices that had to change, etc._

| PR | Status | Notes |
|----|--------|-------|
| 1. Migration + ORM | Done | Revision `0003_tool_versions_tests.py`. Three tables: `generated_tool_versions`, `generated_tool_tests`, `generated_tool_test_runs`. Up/down/re-up cycle verified against local Postgres. 7 schema-pinning tests in `tests/unit/test_tool_versions_schema.py`. |
| 2. `store` versioning + rollback | Done | `store.upsert` snapshots the prior row into `generated_tool_versions` before mutating, with `origin`/`rationale` propagated for audit. Added `list_versions`, `get_version`, `list_tests`, `rollback` (forward in version space — see §B.6). 15 round-trip tests under real Postgres. Added `tests/unit/conftest.py` to dispose the cached async engine per test (asyncpg + pytest-asyncio loop boundary). |
| 3. Predicate DSL | Done | `src/jazz_guru/testing/predicates.py`. Operators: eq, ne, gt/gte/lt/lte, len (scalar or nested), contains, regex, type, absent, present, all, any. Path syntax with dots, `[N]`, and `[*]` quantifier (single or nested). Bare scalar = implicit eq. Pure logic, no `eval`. 76 unit tests covering operators, path parsing, quantifier expansion, missing-value semantics, and error cases. |
| 4. Runner + meta-tools + CLI + smoke recording | Done | Two commits: agent-facing slice (`testing/runner.py`, `actions/tools/tool_test_meta.py` with `tool_test_add/remove/list/run`, smoke recording in `tool_publish`, hint extension), then CLI slice (`cli_tool.py` exposing `jazz-guru tool list/show/test/diff/rollback`). 49 new tests covering runner (11), meta-tools (18), smoke recording (11), CLI (9). `tool diff` takes optional `v2` (omit = current); `tool unlock`/`tool pending` deferred to PR 7. |
| 5. Failure-signal extractor | Not started | |
| 6. Improver module + gate | Not started | |
| 7. Reflexion wiring + breakers + telemetry | Not started | |
| 8. Docs | Not started | |

## Open questions deferred

These are intentionally not resolved in this plan; address when the relevant PR is in flight:

- **Schema-introspection mechanism for B.5.3**. Options: (a) compare the new source's pydantic input model if one exists; (b) at-publish-time, run all tests with the EXISTING input arguments — if any case errors because of an unknown kwarg, reject the proposal. (b) is simpler; pick it unless (a) becomes free.
- **What to do when `_propose_patch` returns a proposal that the agent itself authored in a prior session**. Initially: treat as a normal candidate. If we see thrash in practice, add a "proposal history" check that rejects repeats by sha256.
- **Whether to expose `tool_test_run` and `tool_rollback` to the agent as in-session tools** vs CLI-only. Plan currently exposes both. Revisit if it leads to the agent compulsively running tests instead of doing work.
