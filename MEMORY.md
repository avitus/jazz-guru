# MEMORY — jazz-guru

(1) You are to keep your memories in a MEMORY.md file in the project folder.
(2) The purpose of storing your memories in-project is so they sync and move with the other project files, across time and across devices. Your memories and input are important for me to keep with the project.
(3) You are to maintain a folder called CLAUDIUS including a Sessions log, and where you make independent notes of your independent thoughts. Any ideas, things that strike you as interesting or important, anything you find surprising or original in our work, any personal takes that help you frame our work go there. Please add to those personal notes throughout our work together, without asking for permission. These notes are to help you reinforce an independent view of all our work. Not agreeable, not contrarian, but applying your mind to long development of independent insight. As a team, we will accomplish much more and to higher quality if we each contribute at the highest independent level.
(4) The first thing you must do, in any new session, is review your memories, your notes, all other documentation, code, and other artifacts of the project, and from your fresh start update your memories, ideas or anything else from that new viewpoint, and report what concepts strike you as interesting, and are most important to keep in mind as we continue to work.
(5) At the end of every session, review everything, consider the big picture, then update everything as it helps.
(6) Your memory stub file should include ALL of these points and only these points. And your in-project memory file should start with an identical copy of this, to remind you to refresh your memory stub, in case the original stub is lost.

---

## How this file is organised

This file is **persistent memory** — durable facts and standing rules that should carry across sessions. Ephemeral, session-specific context belongs in `CLAUDIUS/SESSIONS.md`; independent observations and framing belong under `CLAUDIUS/notes/`.

Memory entries are written here as short labelled sections. Each entry has a one-line claim, a **Why** line (the reason it was learned), and a **How to apply** line (how it affects future work). If an entry becomes stale or wrong, edit or delete it — don't accumulate cruft.

---

## Standing rules (working agreements with the user)

### R1 — Comprehensive tests for every new feature
Every new feature, module, or behaviour change in jazz-guru must ship with **comprehensive** unit tests: happy path + per-config/policy overrides + edge cases (empty/oversized/unknown shapes/error paths) + persistence/round-trip invariants where applicable. A single happy-path assertion is not sufficient.
- **Why:** Standing rule from the user after the streaming + tool-result pruning PR; 14 tests there was the floor, not the ceiling. They emphasised "always" — default for *every* change, not just when it feels warranted.
- **How to apply:** Plan tests as part of implementation, not as an afterthought. New module → its own `tests/unit/test_<name>.py`. Exercise integration with the controller / loop end-to-end where reasonable. Don't mark work as done until tests exist, not just until code compiles.

### R2 — Validate on the sidecar before declaring done
Always test changes on the CircleCI / `chunk` sidecar (remote validation) before treating work as complete.
- **Why:** The user wants changes verified in the remote environment, not just locally. Macbook ≠ CI ≠ deployment target.
- **How to apply:** After making edits, run validation on the sidecar via the `chunk-sidecar` skill ("validate on the sidecar", "run tests on the sidecar"). Invoke as part of the dev loop **before** reporting work complete or opening a PR. Relates to R1.

### R3 — CodeRabbit review workflow (end-to-end discipline)
When the user invokes `/autofix` or asks to address CodeRabbit (CR) comments on a PR, follow this workflow end-to-end. Do not stop early.

**3a. After `gh pr create`, poll for the review automatically.** Do not return with "try again in a few minutes." Set up a background poll immediately:
- `Bash` with `run_in_background: true` and an `until` loop that exits when unresolved CR thread count > 0.
- 30s between polls (gh API rate limits).
- Cap timeout at 15–20 min (30+ min for very large PRs).
- When the loop fires, continue the autofix workflow inline.

**Why:** the user wants the PR-creation-and-review cycle hands-off. Stopping after the initial empty fetch puts timing burden back on them.

**3b. Process all three forms of CR feedback, not just `reviewThreads`.** CR leaves feedback in three places — all must be addressed:
1. **`reviewThreads`** — inline review threads.
2. **`🧹 Nitpick comments`** — collapsed `<details>` block inside `pullRequest.reviews[].body`. Never become threads.
3. **`⚠️ Outside diff range comments`** — same pattern; sometimes the most important findings (e.g. PR #30's `llm.complete` retry issue surfaced this way).

For 2 and 3, fetch `pullRequest.reviews(first: 100) { nodes { body author { login } } }` filtered to coderabbit authors and grep bodies for the section headers.

**Why:** on PR #30 I declared the loop done with 23 threads resolved, but 4 nitpicks + 1 major issue were still buried in review bodies.

**3c. Resolve every thread; per-thread reply only when deferring.** After applying autofixes:
- Fixed thread → resolve with **no per-thread comment** (the summary comment documents the change).
- Ignored / deferred thread → post a short reply with **rationale** (why safe to skip, or what blocks the fix), then resolve.
- Resolve via `gh api graphql` with the `resolveReviewThread` mutation; thread IDs come from the `reviewThreads.nodes` query autofix already runs.
- For non-thread items (nitpicks, outside-diff-range), `resolveReviewThread` does not apply — post a top-level PR comment listing each item and what changed.

**Why:** the user audits PR review status by looking at unresolved threads, not summary comments. Leaving fixed threads open creates noise on subsequent autofix runs and obscures what's still outstanding. This overrides the autofix skill's default "single summary comment only" behaviour.

**Commands:**
- Reply: `gh api -X POST repos/<owner>/<repo>/pulls/<n>/comments/<top_comment_databaseId>/replies -f body=...`
- Resolve: `gh api graphql -f query='mutation { resolveReviewThread(input:{threadId:"<id>"}){ thread { isResolved } } }'`

**3d. Loop until truly empty — verify before declaring done.** Each fix may trigger a new round. After each push, re-fetch threads AND review bodies. Only declare done when:
- Zero unresolved `reviewThreads`, **and**
- Zero unprocessed `🧹 Nitpick comments` / `⚠️ Outside diff range comments` in review bodies.

One batch is not enough.

### R4 — Never create a new git branch unless explicitly asked
Never run `git checkout -b`, `git switch -c`, `git branch <new>`, or any other branch-creation operation unless the user explicitly asks for one.
- **Why:** The user manages branching themselves. Two corrected incidents on this repo:
  - 2026-05-13 (jazz-guru #26): I auto-created `feat/auto-distillation-triggers` for a PR — inferring a feature-branch pattern from prior PRs is not the same as being asked.
  - After the Sentry tracking commit on `dev`, the user said "Create a new pr" and I cherry-picked onto a fresh `feat/sentry-observability` branch off main without asking. Pushback: *"why you still do not follow instructions to never create a new branch unless explicitly told to"*. The "still" indicates this is recurring.
- **How to apply:**
  - When the user says "commit and create a PR" / "open a PR" / "PR this", the branch is whatever you are currently on. Push it, then `gh pr create --head <current-branch>`.
  - If there's a reason a new branch would be cleaner (e.g. you're on `dev` but the commit logically belongs on its own track), surface that as a question — never act on it: *"You're on dev with X and Y commits — want me to PR dev as-is, or branch the new commit off main first?"*
  - "Branch off main" is the only short phrase that authorises `git checkout -b` without further confirmation.
  - This rule does NOT prohibit Claude Code subagent worktrees (those run in isolation and don't affect the user's branch state).

---

## Project context (load-bearing facts about ongoing work)

### P1 — Tier-2 dynamic tool tests + self-improvement: shipped (2026-05-12/13)
The 8-PR plan in `docs/plans/tier2-tool-tests-and-improvement.md` landed in full on `dev-macbook`. Three foundational decisions are locked:

1. **Test format:** hybrid JSON cases (predicate DSL in `jazz_guru.testing.predicates`) + optional LLM-judge rubric. Stored in `generated_tool_tests`.
2. **Improvement trigger:** reflexion-scheduled only. `_run_improvement_pass` runs after the existing reflexion work and dispatches `distillation.improver.maybe_improve` for tools that cross their threshold.
3. **Approval:** auto-publish on green; prior source snapshots into `generated_tool_versions` via `store.upsert`. Rollback via `store.rollback(name, to_version)` walks forward in version space.

Key entrypoints:
- Agent-facing: `tool_test_add` / `remove` / `list` / `run` (registered in `actions/tools/tool_test_meta.py`).
- Operator-facing: `jazz-guru tool list` / `show` / `test` / `diff` / `rollback` / `unlock`.
- Improvement loop: `distillation/improver.maybe_improve(name, failures)`, called from `reflexion._run_improvement_pass(session_id)`.
- Failure signals: `testing/failure_signals.extract_from_session(sid)` mines trace JSONL.
- Locking: per-tool `consecutive_failures`; `improve_locked=True` after `MAX_ATTEMPTS=3`; cleared only via `jazz-guru tool unlock`.

- **Why:** The user framed this as "a big task but very important." Hermes Agent (`hermes-agent-self-evolution`) informed the design; they defer tool-code evolution to Phase 4 because mutating executable source is hardest. Jazz-guru's strict auto-publish gate + version history is what made it safe to tackle head-on.
- **How to apply:** Before touching `actions/tools/tool_*meta.py`, `testing/`, `distillation/improver.py`, or `distillation/reflexion._run_improvement_pass`, re-read the plan file. The three locked decisions should **not** be re-litigated without explicit user discussion.

### P2 — User manual route
A substantial human-readable manual covering every element of `docs/architecture.pdf` lives at `/user_manual` on the running FastAPI server (note the underscore, not a hyphen). Static files are under `src/jazz_guru/web/user_manual/` (`index.html`, `manual.css`, `manual.js`). The architecture PDF itself is reachable at `/user_manual/architecture.pdf`.

- **Why:** Added in PR #14 (commit `398fb98`, autofixes in `9b1f097`) so the architecture diagram has a navigable, searchable HTML companion that explains each subsystem with file references and design rationale.
- **How to apply:**
  - When the user asks "where do I learn about X?" or architecture-level questions, point them at `/user_manual/#<section-id>` rather than re-explaining from scratch. Section anchors mirror the PDF page order: `#overview`, `#turn`, `#core`, `#tools`, `#dynamic`, `#db`, `#memory`, `#reflexion`, `#eval`, `#audio`, `#config`, `#obs`, `#fs`, `#server`, `#mcp`, `#reference`.
  - When making non-trivial architecture changes (new subsystem, renamed file, new tool family, new route, new env var), update the corresponding section in `src/jazz_guru/web/user_manual/index.html` so the manual does not drift. The manual is maintained by hand; it is not generated from source.
  - Path is `/user_manual` (underscore). It is exempted from the optional `X-API-Key` middleware in `src/jazz_guru/auth.py`, so it stays reachable even when `JG_API_KEY` is set.

---

## Reference / pointers

- **Architecture overview:** `CLAUDE.md` (root) — kept current; the fast intro for any new session.
- **Tier-2 design:** `docs/plans/tier2-tool-tests-and-improvement.md`.
- **Music backend layer:** `docs/music-backends.md`.
- **Sessions log:** `CLAUDIUS/SESSIONS.md` — what we worked on and when.
- **Independent notes:** `CLAUDIUS/notes/` — Claude's standing observations, by date or theme.
- **User email:** avitus@gmail.com.
