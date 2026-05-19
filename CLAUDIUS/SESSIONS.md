# Sessions log

A rolling record of what we worked on, when. Entries are short. Detail belongs in `notes/` or in the commit/PR history; this file is just an index of work-sessions.

Format per entry: `## YYYY-MM-DD — short title` then 1–5 bullets. Cite commits, PRs, or files where useful. Add a `Carry-forward:` line if there is unfinished work to pick up next session.

---

## 2026-05-16 — Memory & CLAUDIUS bootstrap

- User defined a new collaboration protocol (6 points): in-project `MEMORY.md`, a `CLAUDIUS/` folder for sessions + independent notes, a session-opening review ritual, and a session-closing update ritual. The stub at `~/.claude/.../memory/MEMORY.md` is reduced to those 6 points only; the substantive memories live here in the project.
- Restructured memory storage: consolidated the 4 separate CodeRabbit feedback memories into one workflow entry. Migrated the four standing rules (testing, sidecar validation, PR thread resolution, no branch creation) and the Tier-2 project context into `MEMORY.md`, plus the user-manual route. Removed the now-redundant per-topic stub files from `~/.claude/...`.
- Created `CLAUDIUS/SESSIONS.md` (this file) and `CLAUDIUS/notes/2026-05-16-first-impressions.md` capturing what struck me on a fresh read of the codebase.
- Repo state on entry: branch `dev-macbook`, clean. Recent work was CodeRabbit autofix passes and a docs commit (`afa77af`) covering tier-2 testing/improver, auto-distillation, blocks, lick_match. Sentry observability work shipped in `9fa25ec` / `a4abbe1` / `c966c48`.
- Merged main into `dev` and resolved the parallel-write conflict on `MEMORY.md` + `SESSIONS.md`: kept main's organisational scaffold, folded in `dev`'s more detailed CodeRabbit workflow and the Sentry-incident "no new branches" example, kept main's R1/R2/P1 and `dev`'s user-manual entry.

Carry-forward: user will direct next work.

---

## 2026-05-17 — PR 34 + PR 33 merged; absorbed Sentry observability

- Opened PR 34 (`chore/claudius-memory-bootstrap` → `main`) for the memory bootstrap. Both PRs (33 + 34) had no unresolved CodeRabbit threads — 33's were already cleared from prior autofix rounds, 34 came back clean ("No actionable comments were generated 🎉").
- Merged PR 34 (`b55f168`) then PR 33 (`fad0d0a`). Fast-forwarded `dev-macbook` to `origin/main`. `origin/dev-macbook` is now behind by 10 commits (push pending operator approval).
- Fast-forward also pulled in PR #32 (`feat/sentry-observability`, merged on the user's other machine 2026-05-16): new `src/jazz_guru/observability.py` with `init_sentry()`, `sentry-sdk>=2.30` dep, seven new `sentry_*` settings, init wired into CLI / server / worker. Plus a 370-line `.claude/skills/blocks-network/SKILL.md` from commit `6176a81`.
- Updated `CLAUDE.md` with the `observability.py` architecture bullet. Added `CLAUDIUS/notes/2026-05-16-sentry-observability.md` capturing the design choices worth holding onto (no-op patterns, committed-DSN-but-conservative-PII split, sample-rate defaults, `Literal` typing for pydantic env vars).

Carry-forward: local `dev-macbook` is 10 commits ahead of `origin/dev-macbook`; need user nod to push. The CLAUDE.md / CLAUDIUS notes updates above are uncommitted — separate commit when the user is ready.
