from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from jazz_guru.cli_tool import tool_app
from jazz_guru.config import get_goal, get_policy, get_settings
from jazz_guru.distillation import enqueue_reflexion, run_reflexion
from jazz_guru.eval import run_all
from jazz_guru.harness import AgentLoop, SessionManager
from jazz_guru.llm import health_check_detailed

app = typer.Typer(add_completion=False, no_args_is_help=True, help="jazz-guru agent harness CLI")
app.add_typer(tool_app, name="tool")
console = Console()


@app.command()
def info() -> None:
    """Show resolved configuration."""
    s = get_settings()
    g = get_goal()
    p = get_policy()
    out = {
        "model": s.anthropic_model,
        "embedding": {"provider": s.embedding_provider, "model": s.embedding_model, "dim": s.embedding_dim},
        "database_url": s.database_url,
        "redis_url": s.redis_url,
        "workspace": str(s.jg_workspace_dir.resolve()),
        "goal_profile": g.profile,
        "objectives": [o.id for o in g.objectives],
        "policy_tools": list(p.tools.keys()),
        "feature_tts": bool(s.feature_tts),
    }
    console.print(Panel.fit(json.dumps(out, indent=2), title="jazz-guru info"))


@app.command()
def ping() -> None:
    """Verify Anthropic credentials and connectivity."""
    s = get_settings()
    ok, detail = asyncio.run(health_check_detailed())
    masked = (s.anthropic_api_key[:10] + "..." + s.anthropic_api_key[-4:]) if s.anthropic_api_key else "(unset)"
    console.print(f"[dim]model={s.anthropic_model}  key={masked}[/dim]")
    if ok:
        console.print(f"[green]anthropic: ok[/green]  -> {detail}")
        raise typer.Exit(code=0)
    console.print(f"[red]anthropic: failed[/red]\n[red]{detail}[/red]")
    raise typer.Exit(code=1)


@app.command()
def goal() -> None:
    """Print the rendered goal block that goes into the system prompt."""
    g = get_goal()
    console.print(Panel(g.render_system_block(), title=f"goal[{g.profile}]"))


@app.command()
def tools() -> None:
    """List registered tools and their schemas."""
    from jazz_guru.actions import register_all

    r = register_all()
    table = Table(title="registered tools")
    table.add_column("name")
    table.add_column("tags")
    table.add_column("description")
    for spec in r.all_specs():
        table.add_row(spec.name, ",".join(spec.tags), spec.description[:80])
    console.print(table)


@app.command("new-session")
def new_session(title: str = typer.Option(None, help="Optional title for the session.")) -> None:
    """Create a new session row and print its ID."""

    async def _run() -> uuid.UUID:
        sm = SessionManager()
        h = await sm.create(title=title)
        return h.id

    sid = asyncio.run(_run())
    console.print(str(sid))


@app.command()
def chat(
    message: str = typer.Argument(..., help="User message."),
    session: str = typer.Option(None, "--session", "-s", help="Existing session UUID."),
) -> None:
    """Send a single message through the agent loop."""

    async def _run() -> dict[str, object]:
        sm = SessionManager()
        if session:
            handle = await sm.load(uuid.UUID(session))
        else:
            handle = await sm.create()
        loop = AgentLoop(handle)
        result = await loop.step(message)
        return {
            "session": str(handle.id),
            "text": result.text,
            "tool_calls": result.tool_calls,
            "usage": {
                "input": result.usage.input_tokens,
                "output": result.usage.output_tokens,
                "usd": round(result.usage.cost_usd, 4),
            },
        }

    out = asyncio.run(_run())
    console.print_json(json.dumps(out))


@app.command()
def trace(session: str = typer.Argument(..., help="Session UUID")) -> None:
    """Print the JSONL trace file for a session."""
    s = get_settings()
    path = Path(s.jg_trace_dir) / f"{session}.jsonl"
    if not path.exists():
        console.print(f"[yellow]no trace at {path}[/yellow]")
        raise typer.Exit(code=1)
    console.print(path.read_text(encoding="utf-8"))


@app.command()
def distill(
    session: str = typer.Argument(..., help="Session UUID"),
    sync: bool = typer.Option(False, "--sync", help="Run inline instead of enqueuing."),
) -> None:
    """Run the reflexion distillation loop on a session."""

    async def _sync() -> dict[str, object]:
        r = await run_reflexion(uuid.UUID(session))
        return {"score": r.score, "critique": r.critique, "playbook_entries": len(r.playbook_entries)}

    if sync:
        console.print_json(json.dumps(asyncio.run(_sync())))
    else:
        try:
            job_id = enqueue_reflexion(uuid.UUID(session))
            console.print(f"enqueued: {job_id}")
        except Exception as e:
            console.print(f"[red]could not enqueue: {e}[/red] (try --sync)")
            raise typer.Exit(code=1) from e


@app.command()
def evalrun(only: str = typer.Option(None, "--only", help="Run a single task by id.")) -> None:
    """Run the regression suite."""

    res = asyncio.run(run_all(only=only))
    console.print_json(json.dumps(res, default=str))


@app.command()
def viewer(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Run the local trace viewer."""
    import uvicorn

    uvicorn.run("jazz_guru.logging.viewer.app:app", host=host, port=port)


@app.command()
def tui(
    server: str = typer.Option("http://127.0.0.1:8000", "--server", "-S", help="Server base URL."),
    session: str = typer.Option(None, "--session", "-s", help="Existing session UUID."),
    api_key: str = typer.Option(None, "--api-key", help="X-API-Key (or set JG_API_KEY)."),
) -> None:
    """Launch the Textual TUI client (mic capture, live tool events)."""
    from jazz_guru.client.tui import run as tui_run

    tui_run(server=server, session=session, api_key=api_key)


@app.command("mic-devices")
def mic_devices() -> None:
    """List input audio devices (PortAudio)."""
    from jazz_guru.client.audio import list_input_devices

    devs = list_input_devices()
    if not devs:
        console.print("[yellow]no input devices found[/yellow]")
        return
    table = Table(title="input devices")
    table.add_column("idx")
    table.add_column("name")
    table.add_column("ch")
    table.add_column("sr")
    for d in devs:
        table.add_row(str(d["index"]), d["name"], str(d["channels"]), str(d["samplerate"]))
    console.print(table)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
