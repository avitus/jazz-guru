# Sentry observability — design notes

Date: 2026-05-16 (absorbed into `dev-macbook` on 2026-05-17 via fast-forward after PRs #34 + #33 merged). Shipped via PR #32 (`feat/sentry-observability`), commits `a4abbe1` / `9fa25ec` / `e605a98`.

## What landed

A single 64-line `src/jazz_guru/observability.py` with one entrypoint, `init_sentry()`. CLI, server, and worker each call it once at process startup (CLI at import time, server/worker inside `run()`). New dep: `sentry-sdk>=2.30`.

Settings additions (in `jazz_guru.config.Settings`):
- `sentry_dsn` — committed default. Set empty to disable.
- `sentry_environment` ("dev")
- `sentry_send_default_pii` (False in code, True in `.env.example`)
- `sentry_enable_logs` (False in code, True in `.env.example`)
- `sentry_traces_sample_rate` (1.0)
- `sentry_profile_session_sample_rate` (1.0)
- `sentry_profile_lifecycle` (`Literal["manual","trace"]`, default `"trace"`)

## Design choices worth holding onto

**No-op when DSN is empty + skipped under pytest.** Tests don't ship events to production Sentry; operators who clear the DSN don't either. The pytest detection uses `"pytest" in sys.modules` — broader than checking for `PYTEST_CURRENT_TEST`, which is only set per test. This catches collection-time imports too. Same pattern is worth reusing for any other "this should never fire under test" wiring.

**Committed default DSN, but conservative PII defaults.** DSNs are public-facing tokens (the docstring explicitly notes "also embedded in browser JS"), so checking one in is fine — they're not secrets in Sentry's threat model. But `sentry_send_default_pii` and `sentry_enable_logs` default to **False in code** and only flip to True via this project's own `.env.example`. The split means: anyone forking jazz-guru gets the DSN out of the box (good for "it just works") but does NOT silently ship request headers, user IP, and LLM I/O to a third party until they opt in. This is exactly the right asymmetry for a tool with a checked-in default.

**Single entrypoint, idempotent via module-level flag.** `_initialized = False` at module scope; `init_sentry()` short-circuits after the first successful init. The CLI module calls it at import time (above the typer setup); server/worker call it inside their `run()` functions. So you get init both on `from jazz_guru.cli import app` (CLI consumers) and on `python -m jazz_guru.server` (server entry). Notable: there's no init in the Blocks handler or in test fixtures — the former because Blocks runs out-of-process and the latter intentionally.

**Sample rates default to 1.0.** Every transaction, every profile session. That's appropriate for a personal dev tool with no scaling concerns, but it's worth knowing where the knob is if usage ever ramps. The docstring on `traces_sample_rate` already points at the env var as the tuning surface.

**`profile_lifecycle="trace"` (the aggressive setting).** The profiler runs while any transaction is active rather than waiting for explicit `start`/`stop` calls. Fine for a personal harness; could be expensive in a hosted multi-tenant context.

## What this means for future work

- If a tool/feature ever needs to *intentionally* avoid being captured (e.g. handling secrets), wrap the relevant span with `sentry_sdk.scope.set_tag("ignore", True)` or use a `BeforeSendCallback`. There's no jazz-guru-specific filter yet — if one becomes necessary, it should live in `observability.py` near `init_sentry()`, not be scattered through call sites.
- The `Literal["manual","trace"]` typing on `sentry_profile_lifecycle` was added in commit `9fa25ec` after an autofix round — worth remembering as a pattern: pydantic-settings env vars that take a fixed string set should be typed as `Literal[...]` so invalid env values fail at startup, not silently inside Sentry's SDK.
- The Blocks handler does NOT currently call `init_sentry()`. If Blocks ever needs error reporting, the per-request `asyncio.run` pattern means init would have to happen inside `handler` — but with the `_initialized` guard, that's still cheap on the hot path. Worth flagging if Blocks usage grows.

## Tying back to the architecture I noted earlier

This integration is a textbook example of two patterns from my first-impressions note:

- **"Typed surfaces over scattered calls."** All Sentry config flows through `Settings`; nothing reaches into `os.environ` directly. Adding a new knob is a one-place change.
- **"Degraded-but-honest fallbacks."** Empty DSN → silent no-op. `ImportError` on `sentry_sdk` → silent no-op. Neither breaks the process. Same shape as the Voyage → hash-stub embedding fallback.
