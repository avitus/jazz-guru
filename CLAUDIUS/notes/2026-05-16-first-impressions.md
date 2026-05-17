# First impressions on a fresh read

Date: 2026-05-16. Fresh-start review of jazz-guru after the user established the collaboration protocol. These are independent observations — what I'd want to remember even if all I had was the code and docs.

## The architectural backbone I want to hold onto

**Tier 1 / Tier 2 / Tier 3 as trust boundaries, not just storage tiers.** The Tier 1 (session-local) → Tier 2 (auto-improvable global) → Tier 3 (manual-edit source) distinction is the conceptual spine of the whole self-evolution story. It's the same partition mature software orgs use — sandbox / staging / production — translated to an autonomous agent's tool repertoire. The clarity of this partition is doing a *lot* of safety work; every meta-tool, every gate, every rollback path is interpretable through it. When in doubt about where a behaviour belongs, ask which tier it lives in.

**Improvement is offline, not in-loop.** This is the design choice I most want to remember and defend. There is no in-session `tool_improve` meta-tool, no failure-rate watcher firing mid-turn. The reflexion pass — strictly after a session ends — is the *only* improvement trigger. This means a live turn cannot be derailed by self-modification. Operations and evolution are explicitly separated in time. Most "self-improving" agent designs let the agent modify itself during a task; jazz-guru does not, and the consequence is that turn behaviour is reproducible and debuggable. If anyone proposes adding an in-session improvement path "for responsiveness," that's the moment to push back.

**The improver is gated by tests; tools without tests are skipped silently.** This is a forcing function with the cost in the right place. If you want a tool to evolve, you must invest in tests for it. Without this rule, the improver would either evolve untested tools recklessly or block evolution wholesale. The silent-skip behaviour is a feature: it makes test investment a positive sum rather than a tax.

## Patterns worth generalising

**Typed tool surfaces over raw shell for project data.** The `preset_*` family (against `data/instruments.yaml`) and the `lick_match`/`lick_match_info` pair (against `data/wjazzd/wjazzd-index.json`) are the same pattern: rather than letting the agent edit project data through `fs_write` / `python_exec`, build a typed, validated, atomic-write surface for that datastore. The lickmatch case also bakes the ODbL attribution requirement into the return value — the legal obligation is enforced by the code path, not by a comment somewhere. Every new datastore the agent touches probably deserves the same treatment.

**The trigger funnel for auto-distillation.** Three signals (explicit close, idle sweep, predecessor scan on new session) all funnel through `maybe_trigger` with a Postgres advisory lock + event-marker dedup. Most projects would have these as three separate code paths and accumulate seam-bugs forever. Funneling them into one gate with explicit dedup is the right call, and it's the kind of small architectural decision that pays compounding interest.

**The hash-stub fallback for embeddings.** `VOYAGE_API_KEY` is optional; without it the embedding store falls back to a deterministic hash-stub. Tests don't burn credits, dev loops don't burn credits, and the memory system is degraded-but-functional rather than broken. Quiet but important pattern: every external dependency should have a "degraded-but-honest" mode for development.

## Tensions worth tracking

**Goal vs meta-layer balance.** `config/goal.md` says the agent's purpose is to make playable music — "prefer producing artifacts over long explanations." Meanwhile, a substantial fraction of the codebase is meta: tools about tools, tests about tests, reflexion about reflexion, an entire `testing/` subpackage plus `distillation/improver.py`. This is reasonable for a research/personal harness, and the meta layer exists *in service of* artifact production. But it's worth keeping an eye on the ratio. If I find myself adding meta-infrastructure for several sessions running without making any music, that's a signal to redirect.

**`shell` and `python_exec` are subprocess escape hatches by design.** Inside those children there's no path filtering — `JG_OS_SANDBOX=1` + `sandbox-exec` is the opt-in mitigation on macOS, but it's off by default. This is documented but easy to forget. Most filesystem-touching tools use `resolve_in_workspace` and are safe; `shell` and `python_exec` are not in that club. Treat them with appropriate suspicion when reviewing diffs that touch them.

**Goal/personality is data, not code.** The discipline is: when the agent grows new behaviour, it should usually go in `config/goal.{md,yaml}` or `config/policy.yaml`, not in a prompt string somewhere. The one static block appended to the goal is the tool-creation hint in `context/builder.py:_TOOL_CREATION_HINT`. Be careful not to slip *other* hardcoded prompt fragments in alongside it; if that hint grows, it should grow into a config surface, not into more inline strings.

## Operational details that bit someone once and shouldn't bite me

**Process-wide settings cache.** `get_settings` is `lru_cache`d. Long-lived processes (server, worker, the Blocks handler) won't see `.env` / YAML changes without `.cache_clear()` or a restart. The Blocks handler clears engine caches per request specifically because asyncpg connections are loop-bound and `asyncio.run` per request creates a new loop. These are exactly the kinds of "ugly truths of async DB drivers" gotchas that take a debugging afternoon if you forget.

**`make worker` ↔ Redis.** The worker is the only sync→async crossover point (`asyncio.run` inside an RQ job function). Without Redis, the auto-distillation idle sweep does not run; explicit-close and new-session scans still work via an inline fallback that's capped at `JG_DISTILL_INLINE_MAX_PER_PROCESS=1` per process. So in a Redis-less dev environment, *one* inline reflexion per process lifetime, then silence. Know this before debugging "why isn't distillation firing."

**`asyncio_mode = "auto"`.** `pyproject.toml` sets this; don't add `@pytest.mark.asyncio` decorators unless a specific test genuinely needs one.

## Things to revisit later

- The `lick_match` surface is read-only over WJazzD. Is there a parallel surface for *writing* learned licks back into a personal corpus? If not, that might be the next natural extension — the playbook covers prose patterns but not raw musical phrases.
- The reflexion JSON contract: `{score, critique, revised_plan, open_threads, memory_writes, playbook_entries}`. Worth re-reading the prompt to see whether `score` is being used for trend-tracking; the goal.yaml `success_criteria` mentions "trends upward across distillation cycles" but I haven't seen the dashboarding side.
- 13 built-in tools + meta + dynamic. At some point the registry becomes a usability problem for the LLM (too many tool descriptions in the prompt). Worth knowing where the cliff is. The `toolsets` bundling in `policy.yaml` suggests the team is aware.
- The `frontend-design` skill is available in this environment — could be useful if the trace viewer or the HTML UI at `/ui/` ever needs a serious facelift.
