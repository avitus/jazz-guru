# jazz-guru — Memory

## Bootstrap (identical to the stub at `~/.claude/projects/-Users-avitus-Projects-jazz-guru/memory/MEMORY.md` — restore the stub from this if lost)

(1) You are to keep your memories in a `MEMORY.md` file in the project folder.

(2) The purpose of storing your memories in-project is so they sync and move with the other project files, across time and across devices. Your memories and input are important for the user to keep with the project.

(3) You are to maintain a folder called `CLAUDIUS` in the project, including a Sessions log, and where you make independent notes of your independent thoughts. Any ideas, things that strike you as interesting or important, anything you find surprising or original in the work, any personal takes that help you frame the work go there. Add to those personal notes throughout the work together, without asking for permission. These notes are to help you reinforce an independent view of all the work. Not agreeable, not contrarian, but applying your mind to long development of independent insight. As a team, we accomplish much more and to higher quality if each of us contributes at the highest independent level.

(4) The first thing to do, in any new session, is review your memories, your notes, all other documentation, code, and other artifacts of the project, and from your fresh start update your memories, ideas, or anything else from that new viewpoint, and report what concepts strike you as interesting, and are most important to keep in mind as the work continues.

(5) At the end of every session, review everything, consider the big picture, then update everything as it helps.

(6) This stub file should include ALL of these points and only these points. The in-project memory file should start with an identical copy of this, to remind you to refresh the memory stub, in case the original stub is lost.

---

# Memories

## CodeRabbit review workflow on this repo (feedback)

End-to-end discipline for `/autofix` and CR comment handling. When the user invokes `/autofix` or asks to address CodeRabbit (CR) comments on a PR, follow this workflow end-to-end. Do not stop early.

### 1. After `gh pr create`, poll for the review automatically

Do not return with "try again in a few minutes." Set up a background poll immediately:
- `Bash` with `run_in_background: true` and an `until` loop that exits when unresolved CR thread count > 0.
- 30s between polls (gh API rate limits).
- Cap timeout at 15–20 min (30+ min for very large PRs).
- When the loop fires, continue the autofix workflow inline.

**Why:** the user wants the PR-creation-and-review cycle hands-off. Stopping after the initial empty fetch puts timing burden back on them.

### 2. Process all three forms of CR feedback, not just `reviewThreads`

CR leaves feedback in three places — all must be addressed:
1. **`reviewThreads`** — inline review threads.
2. **`🧹 Nitpick comments`** — collapsed `<details>` block inside `pullRequest.reviews[].body`. Never become threads.
3. **`⚠️ Outside diff range comments`** — same pattern; sometimes the most important findings (e.g. PR #30's `llm.complete` retry issue surfaced this way).

For 2 and 3, fetch `pullRequest.reviews(first: 100) { nodes { body author { login } } }` filtered to coderabbit authors and grep bodies for the section headers.

**Why:** on PR #30 I declared the loop done with 23 threads resolved, but 4 nitpicks + 1 major issue were still buried in review bodies.

### 3. Per-thread reply + resolve (overrides the autofix-skill default)

For **each** inline review thread:
1. Post a one-line disposition reply on the thread:
   - `Fixed in <commit-sha>: <one-liner what changed>`
   - `Skipped — <reason>` for false positives
   - `Already addressed in <commit-sha>` if a prior push fixed it
2. Resolve the thread via `gh api graphql mutation resolveReviewThread`.

For non-thread items (nitpicks, outside-diff-range), `resolveReviewThread` does not apply — post a top-level PR comment listing each item and what changed.

The autofix skill default is "summary comment only, no per-issue replies." For this repo that is overridden — use per-thread replies AND a summary at the end.

**Why:** without per-thread audit trails, the user can't tell at a glance which threads are handled on a large review.

**Commands:**
- Reply: `gh api -X POST repos/<owner>/<repo>/pulls/<n>/comments/<top_comment_databaseId>/replies -f body=...`
- Resolve: `gh api graphql -f query='mutation { resolveReviewThread(input:{threadId:"<id>"}){ thread { isResolved } } }'`

### 4. Loop until truly empty — verify before declaring done

Each fix may trigger a new round. After each push, re-fetch threads AND review bodies. Only declare done when:
- Zero unresolved `reviewThreads`, **and**
- Zero unprocessed `🧹 Nitpick comments` / `⚠️ Outside diff range comments` in review bodies.

One batch is not enough.

---

## Never create a branch without explicit instruction (feedback)

**Rule:** Never create a new git branch unless the user has explicitly told me to (e.g. "branch off main as feat/X", "make a branch called Y"). Phrases like "create a new PR", "open a PR", "push this" are NOT permission to create a branch. The default is: keep working on whatever branch the user is already on (typically `dev`) and PR from there.

**Why:** The user has corrected me on this repeatedly. The most recent incident: after I committed Sentry tracking to `dev`, they said "Create a new pr" — I cherry-picked onto a new `feat/sentry-observability` branch off main without asking, and the user pushed back hard: *"why you still do not follow instructions to never create a new branch unless explicitly told to"*. The "still" indicates this is a recurring violation. The reason I'd created the branch (single-commit PR, doesn't mix with other work) is a reasonable engineering preference but is NOT mine to make on this repo.

**How to apply:**
- If the user says "create a PR" / "open a PR" / "PR this", the branch is whatever you are currently on. Push it, then `gh pr create --head <current-branch>`.
- If there's a reason a new branch would be cleaner (e.g. you're on `dev` but the commit logically belongs on its own track), surface that as a question — never act on it: *"You're on dev with X and Y commits — want me to PR dev as-is, or branch the new commit off main first?"*
- "Branch off main" is the only short phrase that authorises `git checkout -b` without further confirmation.
- This rule does NOT prohibit Claude Code subagent worktrees (those run in isolation and don't affect the user's branch state).

---

## User manual route (project)

A substantial human-readable manual covering every element of `docs/architecture.pdf` lives at `/user_manual` on the running FastAPI server (note the underscore, not a hyphen). Static files are under `src/jazz_guru/web/user_manual/` (`index.html`, `manual.css`, `manual.js`). The architecture PDF itself is reachable at `/user_manual/architecture.pdf`.

**Why:** Added in PR #14 (commit `398fb98`, autofixes in `9b1f097`) so the architecture diagram has a navigable, searchable HTML companion that explains each subsystem with file references and design rationale.

**How to apply:**
- When the user asks "where do I learn about X?" or architecture-level questions, point them at `/user_manual/#<section-id>` rather than re-explaining from scratch. Section anchors mirror the PDF page order: `#overview`, `#turn`, `#core`, `#tools`, `#dynamic`, `#db`, `#memory`, `#reflexion`, `#eval`, `#audio`, `#config`, `#obs`, `#fs`, `#server`, `#mcp`, `#reference`.
- When making non-trivial architecture changes (new subsystem, renamed file, new tool family, new route, new env var), update the corresponding section in `src/jazz_guru/web/user_manual/index.html` so the manual does not drift. The manual is maintained by hand; it is not generated from source.
- Path is `/user_manual` (underscore). It is exempted from the optional `X-API-Key` middleware in `src/jazz_guru/auth.py`, so it stays reachable even when `JG_API_KEY` is set.
