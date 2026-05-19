# Sessions log

A rolling record of what we worked on, when. Entries are short. Detail belongs in `notes/` or in the commit/PR history; this file is just an index of work-sessions.

Format per entry: `## YYYY-MM-DD — short title` then 1–5 bullets. Cite commits, PRs, or files where useful. Add a `Carry-forward:` line if there is unfinished work to pick up next session.

---

## 2026-05-16 — Memory & CLAUDIUS bootstrap

- User defined a new collaboration protocol (6 points): in-project `MEMORY.md`, a `CLAUDIUS/` folder for sessions + independent notes, a session-opening review ritual, and a session-closing update ritual. The stub at `~/.claude/.../memory/MEMORY.md` is reduced to those 6 points only; the substantive memories live here in the project.
- Migrated the four standing rules (testing, sidecar validation, PR thread resolution, no branch creation) and the Tier-2 project context into `MEMORY.md`, then removed the now-redundant per-topic stub files (`feedback_*.md`, `project_tier2_plan.md`). The stub area now contains only the 6-point `MEMORY.md`.
- Created `CLAUDIUS/SESSIONS.md` (this file) and `CLAUDIUS/notes/2026-05-16-first-impressions.md` capturing what struck me on a fresh read of the codebase.
- Repo state on entry: branch `dev-macbook`, clean. Recent work was CodeRabbit autofix passes and a docs commit (`afa77af`) covering tier-2 testing/improver, auto-distillation, blocks, lick_match.

Carry-forward: user will direct next work.

---

## 2026-05-17 — PR 34 + PR 33 merged; absorbed Sentry observability

- Opened PR 34 (`chore/claudius-memory-bootstrap` → `main`) for the memory bootstrap. Both PRs (33 + 34) had no unresolved CodeRabbit threads — 33's were already cleared from prior autofix rounds, 34 came back clean ("No actionable comments were generated 🎉").
- Merged PR 34 (`b55f168`) then PR 33 (`fad0d0a`). Fast-forwarded `dev-macbook` to `origin/main`. `origin/dev-macbook` is now behind by 10 commits (push pending operator approval).
- Fast-forward also pulled in PR #32 (`feat/sentry-observability`, merged on the user's other machine 2026-05-16): new `src/jazz_guru/observability.py` with `init_sentry()`, `sentry-sdk>=2.30` dep, seven new `sentry_*` settings, init wired into CLI / server / worker. Plus a 370-line `.claude/skills/blocks-network/SKILL.md` from commit `6176a81`.
- Updated `CLAUDE.md` with the `observability.py` architecture bullet. Added `CLAUDIUS/notes/2026-05-16-sentry-observability.md` capturing the design choices worth holding onto (no-op patterns, committed-DSN-but-conservative-PII split, sample-rate defaults, `Literal` typing for pydantic env vars).

Carry-forward: local `dev-macbook` is 10 commits ahead of `origin/dev-macbook`; need user nod to push. The CLAUDE.md / CLAUDIUS notes updates above are uncommitted — separate commit when the user is ready.

---

## 2026-05-17 — DTL1000 augmented-data investigation

- User asked whether `ppquadrat/DigThatLick` (or anything else) bundles note-level musical content for DTL1000, parallel to WJazzD's SQLite of full transcriptions.
- Surveyed the repo, the full Dig That Lick OSF tree (`buxvr` → Metadata `rqk7z` → 6 sub-components incl. DTL1000 `bwg42`), the Jazzomat downloads page, the QMUL deliverables page, the UK Data Service ReShare entry, and the project's Pattern Similarity Search interface.
- Conclusion: the ~1,736 monophonic CRNN-extracted solos exist but are **not publicly downloadable** — only exposed via the web UI at `dig-that-lick.hfm-weimar.de/similarity_search/`. The 4 files already in `data/DTL1000/` are the complete public DTL1000 release. Detailed write-up + suggested next moves saved to `CLAUDIUS/notes/2026-05-17-dtl1000-transcriptions-not-public.md`.

Carry-forward: user decided no implementation yet; if/when we proceed, the cheap first step is a `dtl_lookup` metadata-only typed surface (mirroring `lick_match_info`'s shape). Note: `data/DTL1000.zip` (2.2 MB) and `data/DTL1000/` are still untracked in git.

---

## 2026-05-19 — Blocks agent-card validation fix; autofix made autonomous

- `blocks publish` was failing schema validation on `blocks/jazz_guru/agent-card.json`: the IO entries used `contentTypes` (plural) but the Blocks validator only accepts a single `contentType` per input/output. Two passes needed — first to switch to `contentType` + add `accept[]` arrays for file-class inputs, then to discover MusicXML media types aren't in the file-class catalog at all (and `+xml` classifies as form-class, which forbids `accept`). Score input/output ended up collapsed to `application/octet-stream` with MIME guidance moved into the descriptions.
- Opened PR #38 (`fix/blocks-agent-card-validation` → `main`) with just the single-file fix. CodeRabbit review kicked off; `/autofix 38` will run when it lands.
- User feedback: don't make them babysit `/autofix` — when CodeRabbit's review is still in progress, poll autonomously instead of exiting with "try again in a few minutes." Wrote up the rule in `CLAUDIUS/notes/2026-05-19-autofix-must-poll-itself.md` and armed a `/loop /autofix 38` with `ScheduleWakeup` at 270s (within the prompt-cache TTL, matches CodeRabbit's typical 2–5 min review). Loop will self-cycle until CodeRabbit posts a review, then run the fix loop and post the summary.

Carry-forward: PR #38 is awaiting CodeRabbit. Autofix loop is polling on its own. The DTL1000 files remain untracked — separate decision when/if the user wants them in git.
