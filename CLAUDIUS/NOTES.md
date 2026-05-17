# Claudius — independent notes

Personal observations, framings, and things that strike me as interesting or important about this project. Add to this freely throughout sessions. Not a log of what we did — that's `SESSIONS.md`. This is for the *takes*, the things I want to remember thinking, even if they turn out to be wrong.

---

## 2026-05-16 — First fresh-start read

After re-reading `CLAUDE.md`, `README.md`, the tier-2 plan, the goal config, and the active observability branch, several things stand out to me as load-bearing.

### The improver is the most interesting piece of the architecture

Most agent frameworks treat tools as fixed. This one has three tiers (session / global / source) with a fully-gated reflexion-driven loop that **auto-publishes new tool versions** when a candidate passes both existing tests and new derived-from-failure tests. The Hermes Agent reference cited in the plan explicitly defers tool-code evolution to a later phase; this project goes after it directly, and the auto-publish gate is the load-bearing piece — `consecutive_failures` + `improve_locked` + `MAX_ATTEMPTS=3` are the circuit breakers, and the prior-version snapshot makes rollback a single row.

What I want to keep watching: **test quality.** The improver runs against tests written by the agent itself (plus the auto-recorded smoke from `tool_publish`). There's a real failure mode where the agent writes permissive tests, the gate green-lights bad patches, and the lock breakers don't catch it because the tests keep passing. Worth flagging when I notice the agent authoring tests that don't actually pin down behaviour.

### Subprocess-as-sandbox is the quiet hero

`python -I` subprocess with stdin/stdout JSON contract, with optional `sandbox-exec -f data/sandbox/jg.sb` wrap on macOS, is what makes dynamic-tool creation *cheap and safe enough* to do automatically. It's the call that lets Tier 1 → Tier 2 actually be a routine thing rather than a security event. The fact that test runs reuse the same sandbox (`testing/runner.py` routes through `dynamic.invoke`) means there's no "test mode vs. prod mode" divergence to maintain. That's a smaller-than-it-looks design choice that pays a lot of rent.

### Goals are configurable, and that's a real commitment

`config/goal.{md,yaml}` is injected into every system prompt; the only thing hardcoded is the tool-creation hint in `context/builder.py:_TOOL_CREATION_HINT`. Most agents bake personality into the code; this one externalises it. The implication: when I notice myself wanting to "tweak the agent's behaviour," the answer is almost always to edit the goal config, not to thread a flag through `loop.py`. Respect the constitutional separation.

### Architecture is a clean 5-stage pipeline

context → memory → action → state → distillation. Each is a subpackage. The `harness/loop.py:AgentLoop.step()` seam is where they compose. I'd resist suggestions that blur these boundaries (e.g. "let's let the action controller write directly to memory mid-tool-call"). The cleanliness here is unusual for an agent framework and is probably why the surface area stays comprehensible.

### Operational hygiene around CR is real

Looking at git log, the last several merged PRs each had multiple rounds of CodeRabbit auto-fixes. The four feedback memories I just consolidated all came from that loop. CR review-body nitpicks specifically have bitten before (PR #30, `llm.complete` retry replay) — they're not a hypothetical. The user takes review-thread hygiene seriously, and partial work on CR threads has been a friction point worth respecting.

### What I'd want to bring up if asked

- **Settings sprawl.** `jg_*` env vars are accumulating across improver, sentry, sandbox flags, sample rates. `config.py` should probably get an audit at some point — not now, but I'll flag it if it crosses a threshold.
- **Manual drift on `/user_manual`.** The HTML manual is hand-maintained per the project memory. That's a discipline cost that compounds. If we land a big architectural change, I should be unprompted about asking whether to update the corresponding section.
- **Tier-2 dynamic tools without tests get skipped silently by the improver.** That's the right default, but it means "we have N Tier-2 tools, M of them improvable" is a real distinction worth surfacing in `jazz-guru tool list` if it isn't already.
- **The PDF is the spec.** `docs/architecture.pdf` is the canonical architecture doc. I haven't read it yet (didn't want to pull pages without need). If a question arises about subsystem design rationale that I can't reconstruct from code, that's where to go first.

### One thing that surprises me, on first read

The reflexion loop is structured to be **fire-and-forget** from the agent's perspective — improvement attempts run async after the session ends, gated by `jg_improver_max_per_run`, with the trace JSONL as the failure signal substrate. That decoupling is genuinely elegant: it means the agent loop stays simple (no in-flight self-modification), and the improvement work is bounded by the operator's rate cap rather than the agent's runtime budget. The price is that improvement is delayed by one reflexion cycle, but that seems clearly worth it.

I would not have designed it this way on first instinct — I'd have been tempted to put a `tool_improve` meta-tool in the agent's hands. The plan explicitly rules that out ("no in-session `tool_improve` meta-tool, no failure-rate watcher, no manual CLI improvement command"). On reflection, the discipline of the trace-driven approach is much better: it forces the failure signal to be observable + replayable, which is what you want for an auto-publish gate.
