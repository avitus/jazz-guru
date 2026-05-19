# /autofix must poll itself — don't make the user babysit

**Date:** 2026-05-19
**Source:** Direct feedback while running `/autofix 38` immediately after `gh pr create` — CodeRabbit was still queued (`"Currently processing new changes in this PR..."`) and the default `/autofix` workflow exits in that state with a "try again in a few minutes" message.

## The rule

When `/autofix` finds CodeRabbit's review is **in progress**, do not exit and leave the user to re-run. Instead, set up an autonomous poll so the next iteration fires without user intervention.

## Why

- The user is explicit: "I'm getting tired of babysitting /autofix."
- CodeRabbit's review-in-progress state is *transient* (typically resolves in 2–5 min) and *predictable* (the polling cost is small, the user-attention cost of remembering to re-run is high).
- Same logic applies to **other** transient states `/autofix` might encounter (rate limits, partial review fetches). Default to "poll yourself" rather than "ask the user."

## How to apply

After `/autofix` detects review-in-progress (or any retryable transient state), instead of just exiting:

1. **Start a `/loop`** on `/autofix <PR#>` via the Skill tool, omitting the interval so the harness self-paces. This puts the runtime into `/loop` dynamic mode, which is the mode `ScheduleWakeup` is designed for.
2. **Call `ScheduleWakeup`** at the end of that turn with `prompt: "/loop /autofix <PR#>"` and a delay tuned to the wait:
   - **CodeRabbit review pending:** 270s — stays within the 5-minute prompt cache window, and CodeRabbit usually finishes inside that span.
   - **Still pending after one round:** another 270s. If it's still pending after ~3 rounds (~15 min), surface it to the user — something is wrong with the review pipeline.
   - **Other transient states** (rate limit, etc.): match the delay to the actual wait the API tells you about.
3. **`reason` on ScheduleWakeup should be specific** — e.g. `"waiting on CodeRabbit review for PR #38"` — so telemetry/user can see what's being polled. (Per `ScheduleWakeup` docs: "watching CI run" beats "waiting".)
4. **Once CodeRabbit posts a real review**, the next `/autofix` iteration picks up the unresolved threads and runs the normal fix loop — no extra wakeup needed.
5. **When the loop is done** (fixes applied + summary posted, or no unresolved threads found), do **not** schedule another wakeup. Omitting `ScheduleWakeup` ends the loop.

## Don't

- Don't fall back to "tell the user to re-run in a few minutes." The whole point of this rule is to remove that step.
- Don't schedule wakeups for `/autofix` when CodeRabbit has actually finished reviewing — only when the workflow hit a *transient retryable* state.
- Don't loop forever — cap at ~3 review-in-progress rounds (~15 min total) before surfacing to the user.
- Don't use a wakeup interval shorter than 270s — the prompt cache TTL is 5 minutes, and dropping below 270s burns cache without buying meaningfully faster polling.

## Edge cases worth watching

- If `/autofix` was invoked fresh in this turn (not from a `/loop`), I need to invoke `/loop /autofix <PR#>` first (via Skill) before `ScheduleWakeup` will fire correctly. `ScheduleWakeup` is documented as "for /loop dynamic mode."
- If the user manually re-runs `/autofix` while a loop is already polling, the second invocation should detect that and either join the existing loop or no-op. The `/loop` skill should handle this — if it doesn't, fall back to noticing the loop's recent wakeup metadata.

## Related

- [[autofix]] — the skill being polled (`.claude/skills/autofix/SKILL.md`).
- [[loop]] — the skill driving polling (`.claude/skills/loop/SKILL.md`).
- `ScheduleWakeup` tool docs — picking delays around the 5-minute cache TTL.
