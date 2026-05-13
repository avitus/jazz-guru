"""``jazz-guru tool ...`` subcommands.

Read/inspect operations against published Tier-2 tools: ``list``, ``show``,
``test``, ``diff``, ``rollback``. Wired into the main ``cli.app`` via
``app.add_typer(tool_app, name="tool")``.
"""
from __future__ import annotations

import asyncio
import difflib
import json

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from jazz_guru.actions import store
from jazz_guru.actions.tools.tool_test_meta import tool_test_run as _run

tool_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect, test, and roll back Tier-2 dynamic tools.",
)
console = Console()


def _short(s: str, n: int = 80) -> str:
    s = s.replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


@tool_app.command("list")
def cmd_list() -> None:
    """All published Tier-2 tools with version, test count, and last status."""

    async def _run_async() -> None:
        tools = await store.list_all(scope=None, include_deprecated=False)
        if not tools:
            console.print("[dim]no published tools[/dim]")
            return
        table = Table(title="Tier-2 tools")
        table.add_column("name")
        table.add_column("v", justify="right")
        table.add_column("tests", justify="right")
        table.add_column("locked", justify="center")
        table.add_column("description")
        for t in tools:
            tests = await store.list_tests(t.name)
            locked = "🔒" if (t.meta or {}).get("improve_locked") else ""
            table.add_row(
                t.name,
                str(t.version),
                str(len(tests)),
                locked,
                _short(t.description),
            )
        console.print(table)

    asyncio.run(_run_async())


@tool_app.command("show")
def cmd_show(name: str = typer.Argument(..., help="Tool name to inspect.")) -> None:
    """Print the current source, schema, tests, and version history."""

    async def _run_async() -> int:
        tool = next(
            (t for t in await store.list_all() if t.name == name),
            None,
        )
        if tool is None:
            console.print(f"[red]unknown tool {name!r}[/red]")
            return 1
        console.print(
            Panel.fit(
                f"name: {tool.name}\n"
                f"version: {tool.version}\n"
                f"scope: {tool.scope}\n"
                f"sha256: {tool.sha256[:12]}…\n"
                f"deprecated: {tool.deprecated}\n"
                f"meta: {json.dumps(tool.meta or {})}",
                title="metadata",
            )
        )
        console.print(Panel(tool.description, title="description"))
        console.print(
            Panel(json.dumps(tool.input_schema or {}, indent=2), title="input schema")
        )
        console.print(Panel(tool.source, title="source"))

        tests = await store.list_tests(name)
        if tests:
            t = Table(title=f"tests ({len(tests)})")
            t.add_column("name")
            t.add_column("origin")
            t.add_column("enabled")
            for c in tests:
                t.add_row(c.name, c.origin, "✓" if c.enabled else "✗")
            console.print(t)
        else:
            console.print(
                "[yellow]no tests attached — improvement loop will skip this tool[/yellow]"
            )

        versions = await store.list_versions(name)
        if versions:
            v = Table(title="version history")
            v.add_column("v", justify="right")
            v.add_column("origin")
            v.add_column("sha256")
            v.add_column("superseded_by", justify="right")
            v.add_column("rationale")
            for ver in versions:
                v.add_row(
                    str(ver.version),
                    ver.origin,
                    ver.sha256[:12] + "…",
                    str(ver.superseded_by) if ver.superseded_by else "",
                    _short(ver.rationale or ""),
                )
            console.print(v)
        return 0

    rc = asyncio.run(_run_async())
    if rc != 0:
        raise typer.Exit(code=rc)


@tool_app.command("test")
def cmd_test(
    name: str = typer.Argument(..., help="Tool to run tests on."),
    case: str = typer.Option(None, "--case", help="Run only this case."),
    judge: bool = typer.Option(
        False, "--judge", help="Evaluate rubric cases via the LLM judge."
    ),
) -> None:
    """Execute the tool's test suite and print pass/fail per case."""

    async def _run_async() -> int:
        r = await _run(name=name, case_name=case, use_judge=judge)
        if not r.get("ok"):
            console.print(f"[red]{r.get('error', 'failed')}[/red]")
            return 1
        if r.get("note"):
            console.print(f"[yellow]{r['note']}[/yellow]")
        passed, failed = r.get("passed", 0), r.get("failed", 0)
        header = (
            f"{name} (v{r.get('version')}): "
            f"[green]{passed} passed[/green], "
            f"[red]{failed} failed[/red]"
        )
        console.print(header)
        table = Table(show_lines=False)
        table.add_column("case")
        table.add_column("ok", justify="center")
        table.add_column("ms", justify="right")
        table.add_column("judge", justify="right")
        table.add_column("failures")
        for c in r.get("cases", []):
            table.add_row(
                c["name"],
                "✓" if c["passed"] else "✗",
                str(c.get("ms", "")),
                f"{c['judge_score']:.2f}" if c.get("judge_score") is not None else "",
                _short("; ".join(c.get("failures") or [])) or (c.get("error") or ""),
            )
        console.print(table)
        return 0 if failed == 0 else 2

    rc = asyncio.run(_run_async())
    if rc != 0:
        raise typer.Exit(code=rc)


@tool_app.command("diff")
def cmd_diff(
    name: str = typer.Argument(..., help="Tool name."),
    v1: int = typer.Argument(..., help="Older version number."),
    v2: int = typer.Argument(
        0,
        help="Newer version number. Omit (or pass 0) to diff against the current live version.",
    ),
) -> None:
    """Unified diff of source between two versions.

    With ``v2`` omitted, the second side is the current live source from
    ``generated_tools`` rather than a snapshot.
    """

    async def _run_async() -> int:
        if v2 not in (0, v1):
            row_b = await store.get_version(name, v2)
            if row_b is None:
                console.print(f"[red]no version {v2} for {name!r}[/red]")
                return 1
            b_label = f"v{v2}"
            b_source = row_b.source
        elif v2 == v1:
            console.print("[yellow]v1 == v2; nothing to diff[/yellow]")
            return 0
        else:
            tool = next(
                (t for t in await store.list_all() if t.name == name),
                None,
            )
            if tool is None:
                console.print(f"[red]unknown tool {name!r}[/red]")
                return 1
            b_label = f"v{tool.version} (current)"
            b_source = tool.source

        row_a = await store.get_version(name, v1)
        if row_a is None:
            console.print(f"[red]no version {v1} for {name!r}[/red]")
            return 1
        diff = "".join(
            difflib.unified_diff(
                row_a.source.splitlines(keepends=True),
                b_source.splitlines(keepends=True),
                fromfile=f"v{v1}",
                tofile=b_label,
                n=3,
            )
        )
        if not diff:
            console.print("[dim]no source-level changes[/dim]")
            return 0
        console.print(diff, end="")
        return 0

    rc = asyncio.run(_run_async())
    if rc != 0:
        raise typer.Exit(code=rc)


@tool_app.command("rollback")
def cmd_rollback(
    name: str = typer.Argument(..., help="Tool name."),
    to: int = typer.Option(..., "--to", help="Target version to restore."),
) -> None:
    """Restore a historical version. The current source is snapshotted first."""

    async def _run_async() -> int:
        r = await store.rollback(name, to_version=to)
        if not r.ok:
            console.print(f"[red]{r.error}[/red]")
            return 1
        console.print(
            f"[green]rolled back[/green] {name}: "
            f"v{r.from_version} → v{r.to_version} (now live as v{r.new_version})"
        )
        return 0

    rc = asyncio.run(_run_async())
    if rc != 0:
        raise typer.Exit(code=rc)


@tool_app.command("unlock")
def cmd_unlock(name: str = typer.Argument(..., help="Tool name.")) -> None:
    """Clear ``improve_locked`` so the improver can try the tool again.

    Set by the improver after ``consecutive_failures`` reaches
    ``MAX_ATTEMPTS``. The agent cannot self-clear this; an operator does
    after reviewing and (presumably) fixing what's wrong.
    """

    async def _run_async() -> int:
        from sqlalchemy import select

        from jazz_guru.db import session_scope
        from jazz_guru.state import GeneratedTool

        async with session_scope() as s:
            tool = (
                await s.execute(select(GeneratedTool).where(GeneratedTool.name == name))
            ).scalar_one_or_none()
            if tool is None:
                console.print(f"[red]unknown tool {name!r}[/red]")
                return 1
            meta = dict(tool.meta or {})
            was_locked = bool(meta.pop("improve_locked", False))
            meta.pop("improve_lock_reason", None)
            meta["consecutive_failures"] = 0
            tool.meta = meta
        if was_locked:
            console.print(
                f"[green]unlocked[/green] {name}; consecutive_failures reset to 0"
            )
        else:
            console.print(
                f"[yellow]{name} was not locked; consecutive_failures reset to 0[/yellow]"
            )
        return 0

    rc = asyncio.run(_run_async())
    if rc != 0:
        raise typer.Exit(code=rc)
