"""Textual TUI client for jazz-guru.

Layout:
  +------------------------+--------------------------+
  |  chat / transcript     |  live tool events        |
  |                        |  artifacts (auto-refresh)|
  |                        |                          |
  +------------------------+--------------------------+
  | input                                              |
  +----------------------------------------------------+

Keybinds:
  Enter         send message
  Space         hold to push-to-talk; release to send the recording
  ctrl+r        toggle continuous VAD recording
  ctrl+l        clear chat
  ctrl+c        quit
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid as uuid_mod
from pathlib import Path
from typing import Any, ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Input, Log, RichLog, Static

from jazz_guru.client.audio import PushToTalk
from jazz_guru.client.sdk import JazzGuruClient


def _stamp() -> str:
    return time.strftime("%H:%M:%S")


class JazzGuruTui(App[None]):
    CSS = """
    Screen { layout: vertical; }
    #main { height: 1fr; }
    #chat-pane { width: 2fr; border: round $primary; padding: 1; }
    #side-pane { width: 1fr; border: round $accent; padding: 1; }
    #events { height: 2fr; border-bottom: solid $surface; }
    #artifacts { height: 1fr; }
    #streaming { height: auto; max-height: 8; padding: 0 1; color: $text-muted; }
    #status-bar { height: 1; background: $boost; padding: 0 1; }
    Input { dock: bottom; }
    """

    BINDINGS: ClassVar[list[Any]] = [
        Binding("space", "ptt_start", "PTT (hold)", priority=True, show=True, key_display="space"),
        Binding("space-released", "ptt_stop", show=False),
        Binding("ctrl+r", "toggle_vad", "VAD on/off"),
        Binding("ctrl+l", "clear_chat", "clear"),
        Binding("ctrl+c", "quit", "quit"),
    ]

    def __init__(
        self,
        server_url: str = "http://127.0.0.1:8000",
        session_id: str | None = None,
        api_key: str | None = None,
    ) -> None:
        super().__init__()
        self._server = server_url
        self._session_id = session_id
        self._api_key = api_key
        self._client: JazzGuruClient | None = None
        self._ptt = PushToTalk()
        self._ptt_path: Path | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._chat_buf: list[str] = []

    # -- layout ---------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="chat-pane"):
                yield RichLog(id="chat", wrap=True, highlight=False, markup=True)
            with Vertical(id="side-pane"):
                yield RichLog(id="events", wrap=False, highlight=True, markup=True)
                yield Log(id="artifacts", auto_scroll=False)
        yield Static("", id="streaming")
        yield Static("ready", id="status-bar")
        yield Input(placeholder="type and press Enter, or hold Space to talk", id="prompt")
        yield Footer()

    # -- lifecycle ------------------------------------------------------
    async def on_mount(self) -> None:
        self._client = JazzGuruClient(self._server, api_key=self._api_key)
        await self._client.open()
        try:
            health = await self._client.health()
            self._status(f"connected to {self._server}  (status={health.get('status')})")
        except Exception as e:
            self._status(f"[red]connection failed: {e}[/red]")
            return
        if not self._session_id:
            try:
                self._session_id = await self._client.create_session(title="tui")
            except Exception as e:
                self._status(f"[red]create_session failed: {e}[/red]")
                return
        self._chat(f"[dim]session {self._session_id} ready[/dim]")
        await self._refresh_artifacts()

    async def on_unmount(self) -> None:
        if self._client is not None:
            await self._client.close()

    # -- handlers -------------------------------------------------------
    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = (event.value or "").strip()
        if not text:
            return
        event.input.value = ""
        await self._send(text)

    async def _send(self, text: str) -> None:
        if not self._client or not self._session_id:
            self._status("[red]not connected[/red]")
            return
        self._chat(f"[bold cyan]you[/bold cyan] {text}")
        self._reset_streaming()
        try:
            stream = await self._client.stream_chat(self._session_id, text)
            async for evt in stream:
                self._handle_event(evt)
            await self._refresh_artifacts()
        except Exception as e:
            self._chat(f"[red]error: {e}[/red]")
            self._reset_streaming()

    def _handle_event(self, evt: dict[str, Any]) -> None:
        t = evt.get("type", "?")
        p = evt.get("payload") or {}
        if t == "ack":
            return
        if t == "tool_use":
            name = p.get("name", "?")
            args = json.dumps(p.get("input") or {})[:120]
            self._event(f"[yellow]use[/yellow] [bold]{name}[/bold] {args}")
        elif t == "tool_result":
            ok = "[green]ok[/green]" if p.get("ok", True) else "[red]err[/red]"
            manifest = p.get("manifest")
            suffix = f"  [dim]({manifest.get('size_bytes')}B → disk)[/dim]" if manifest else ""
            self._event(f"     {ok} {p.get('name','?')}{suffix}")
        elif t == "text_delta":
            delta = p.get("delta", "")
            if delta:
                self._chat_buf.append(delta)
                self._update_streaming("".join(self._chat_buf))
        elif t == "llm_request":
            self._event(f"[dim]→ llm round {p.get('round')}[/dim]")
        elif t == "llm_response":
            usage = p.get("usage", {})
            self._event(
                f"[dim]← stop={p.get('stop_reason')} in={usage.get('in')} out={usage.get('out')}[/dim]"
            )
        elif t == "artifacts":
            return  # handled via refresh
        elif t == "final":
            text = evt.get("text", "")
            if text:
                self._chat(f"[bold magenta]agent[/bold magenta] {text}")
            self._reset_streaming()
            usage = evt.get("usage", {})
            cost = usage.get("cost_usd", 0.0)
            self._status(
                f"final  tool_calls={evt.get('tool_calls', 0)}  "
                f"in={usage.get('input_tokens',0)} out={usage.get('output_tokens',0)}  ${cost:.4f}"
            )
        elif t == "error":
            self._event(f"[red]{evt.get('error','error')}[/red]")
            self._reset_streaming()
        else:
            self._event(f"[dim]{t}[/dim] {json.dumps(p)[:120]}")

    # -- artifacts ------------------------------------------------------
    async def _refresh_artifacts(self) -> None:
        if not self._client or not self._session_id:
            return
        try:
            arts = await self._client.list_artifacts(self._session_id)
        except Exception:
            return
        log = self.query_one("#artifacts", Log)
        log.clear()
        if not arts:
            log.write_line("(no artifacts yet)")
            return
        for a in arts:
            log.write_line(f"{a['size']:>9}  {a['path']}")

    # -- mic capture ----------------------------------------------------
    def action_ptt_start(self) -> None:
        if self._ptt.is_recording:
            return
        if not self._session_id:
            self._status("[red]no session yet[/red]")
            return
        out_dir = (
            Path(os.environ.get("JG_WORKSPACE_DIR", "./workspace"))
            / "sessions" / self._session_id / "in"
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        self._ptt_path = out_dir / f"ptt_{int(time.time()*1000)}.wav"
        try:
            self._ptt.start()
            self._status("[red]● recording[/red]  release space to send")
        except Exception as e:
            self._status(f"[red]mic error: {e}[/red]")

    def action_ptt_stop(self) -> None:
        if not self._ptt.is_recording or self._ptt_path is None:
            return
        try:
            path = self._ptt.stop_and_save(self._ptt_path)
            self._status(f"recorded {path.name}  ({path.stat().st_size}B)")
            t = asyncio.create_task(self._send(
                f"[audio recording at {path}] use audio_analyze on this path "
                "and respond about what was played."
            ))
            self._tasks.add(t)
            t.add_done_callback(self._tasks.discard)
        except Exception as e:
            self._status(f"[red]mic stop error: {e}[/red]")
        finally:
            self._ptt_path = None

    # -- VAD ------------------------------------------------------------
    def action_toggle_vad(self) -> None:
        # placeholder for parity; full VAD wiring requires loop integration
        self._status("VAD: not enabled in v1; use space (PTT) for now")

    def action_clear_chat(self) -> None:
        self.query_one("#chat", RichLog).clear()
        self.query_one("#events", RichLog).clear()

    # -- helpers --------------------------------------------------------
    def _chat(self, line: str) -> None:
        log = self.query_one("#chat", RichLog)
        log.write(f"[dim]{_stamp()}[/dim] {line}")

    def _event(self, line: str) -> None:
        log = self.query_one("#events", RichLog)
        log.write(f"[dim]{_stamp()}[/dim] {line}")

    def _status(self, line: str) -> None:
        self.query_one("#status-bar", Static).update(line)

    def _update_streaming(self, text: str) -> None:
        # Show the trailing portion so a long generation doesn't push the
        # status bar / input off-screen. The full text lands in the chat
        # pane on `final`.
        tail = text[-800:]
        self.query_one("#streaming", Static).update(f"[italic dim]{tail}[/italic dim]")

    def _reset_streaming(self) -> None:
        self._chat_buf.clear()
        self.query_one("#streaming", Static).update("")


def run(server: str = "http://127.0.0.1:8000", session: str | None = None, api_key: str | None = None) -> None:
    if session:
        # validate
        uuid_mod.UUID(session)
    JazzGuruTui(server, session, api_key).run()


if __name__ == "__main__":
    run()
