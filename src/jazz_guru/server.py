from __future__ import annotations

import asyncio
import contextlib
import json
import mimetypes
import time
import uuid
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from jazz_guru import auth
from jazz_guru.config import get_goal, get_settings
from jazz_guru.distillation import enqueue_reflexion, run_reflexion
from jazz_guru.eval import run_all
from jazz_guru.harness import AgentLoop, SessionManager
from jazz_guru.logging import get_logger
from jazz_guru.memory import get_memory
from jazz_guru.state import list_session_artifacts

log = get_logger(__name__)

WEB_STATIC = Path(__file__).resolve().parent / "web" / "static"


class CreateSessionBody(BaseModel):
    title: str | None = None
    goal_profile: str = "default"


class ChatBody(BaseModel):
    message: str


class MemorySearchBody(BaseModel):
    query: str
    k: int = 5
    session_id: str | None = None


def _session_dir(session_id: uuid.UUID) -> Path:
    return get_settings().jg_workspace_dir / "sessions" / str(session_id)


def create_app() -> FastAPI:
    app = FastAPI(title="jazz-guru", version="0.1.0")
    sm = SessionManager()
    auth.install(app)

    if WEB_STATIC.exists():
        app.mount("/ui", StaticFiles(directory=str(WEB_STATIC), html=True), name="ui")

    # ---------- index ----------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        g = get_goal()
        rows = [
            ("GET",  "/ui/",                               "graphical web UI"),
            ("GET",  "/health",                            "liveness check"),
            ("GET",  "/goal",                              "rendered goal block"),
            ("GET",  "/docs",                              "Swagger UI"),
            ("POST", "/sessions",                          "create a new session"),
            ("POST", "/sessions/{id}/chat",                "send a message"),
            ("POST", "/sessions/{id}/distill?sync=true",   "run reflexion distillation"),
            ("POST", "/eval/run",                          "run the regression suite"),
            ("POST", "/memory/search",                     "vector search over memory"),
            ("GET",  "/artifacts/{id}",                    "list session artifacts (JSON)"),
            ("GET",  "/artifacts/{id}/{path}",             "download a session artifact"),
            ("WS",   "/ws/sessions/{id}/chat",             "streaming tool events"),
        ]
        body = "\n".join(
            f'<tr><td class="m m-{m.lower()}">{m}</td>'
            f'<td><a href="{p if "{" not in p and m=="GET" else "#"}"><code>{p}</code></a></td>'
            f"<td>{d}</td></tr>"
            for m, p, d in rows
        )
        return f"""<!doctype html><meta charset=utf-8>
<title>jazz-guru</title>
<style>
body{{font:14px/1.5 system-ui,-apple-system,sans-serif;margin:32px;max-width:860px;color:#222}}
h1{{margin:0 0 4px}} .sub{{color:#666;margin-bottom:24px}}
table{{border-collapse:collapse;width:100%;margin-top:8px}}
td{{padding:6px 10px;border-bottom:1px solid #eee;vertical-align:top}}
.m{{font-weight:600;font-size:11px;width:48px;text-align:center;border-radius:3px;color:#fff}}
.m-get{{background:#3b82f6}} .m-post{{background:#10b981}} .m-ws{{background:#8b5cf6}}
code{{background:#f3f4f6;padding:1px 6px;border-radius:3px;font-size:12px}}
a{{color:#1d4ed8;text-decoration:none}} a:hover{{text-decoration:underline}}
.tag{{display:inline-block;background:#f3f4f6;padding:2px 8px;border-radius:10px;font-size:11px;margin-right:6px;color:#444}}
</style>
<h1>jazz-guru</h1>
<div class="sub">
  <span class="tag">profile: {g.profile}</span>
  <span class="tag">objectives: {len(g.objectives)}</span>
  <a href="/ui/">graphical UI &rarr;</a> &nbsp;
  <a href="/docs">interactive API docs &rarr;</a>
</div>
<table>{body}</table>"""

    @app.get("/favicon.ico")
    async def _favicon() -> RedirectResponse:
        return RedirectResponse(url="/ui/favicon.svg")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/goal")
    async def goal() -> dict[str, object]:
        g = get_goal()
        return {
            "profile": g.profile,
            "rendered": g.render_system_block(),
            "objectives": [o.model_dump() for o in g.objectives],
            "constraints": g.constraints,
            "success_criteria": g.success_criteria,
        }

    # ---------- sessions / chat -----------------------------------------
    @app.post("/sessions")
    async def create_session(body: CreateSessionBody) -> dict[str, str]:
        h = await sm.create(title=body.title, goal_profile=body.goal_profile)
        return {"id": str(h.id)}

    @app.post("/sessions/{session_id}/chat")
    async def chat(session_id: str, body: ChatBody) -> dict[str, object]:
        try:
            sid = uuid.UUID(session_id)
        except ValueError as e:
            raise HTTPException(400, f"invalid session id: {e}") from e
        h = await sm.load(sid)
        loop = AgentLoop(h)
        res = await loop.step(body.message)
        return {
            "text": res.text,
            "tool_calls": res.tool_calls,
            "rounds": res.rounds,
            "usage": {
                "input_tokens": res.usage.input_tokens,
                "output_tokens": res.usage.output_tokens,
                "cost_usd": res.usage.cost_usd,
            },
            "errors": res.errors,
        }

    @app.post("/sessions/{session_id}/distill")
    async def distill(session_id: str, sync: bool = False) -> dict[str, object]:
        try:
            sid = uuid.UUID(session_id)
        except ValueError as e:
            raise HTTPException(400, f"invalid session id: {e}") from e
        if sync:
            r = await run_reflexion(sid)
            return {"mode": "sync", "score": r.score, "critique": r.critique}
        try:
            job_id = enqueue_reflexion(sid)
            return {"mode": "async", "job_id": job_id}
        except Exception as e:
            raise HTTPException(500, f"could not enqueue: {e}") from e

    @app.post("/eval/run")
    async def eval_run(only: str | None = None) -> dict[str, object]:
        return await run_all(only=only)

    @app.post("/memory/search")
    async def memory_search(body: MemorySearchBody) -> dict[str, object]:
        try:
            sid = uuid.UUID(body.session_id) if body.session_id else None
        except ValueError as e:
            raise HTTPException(400, f"invalid session_id: {e}") from e
        recs = await get_memory().search(body.query, k=body.k, session_id=sid)
        return {"results": [{"id": str(r.id), "kind": r.kind, "text": r.text, "score": r.score} for r in recs]}

    # ---------- uploads --------------------------------------------------
    MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MiB; tune if you need more

    @app.post("/uploads/{session_id}")
    async def upload(session_id: str, request: Request, name: str | None = None) -> dict[str, Any]:
        # Streams the request body to disk in chunks so a single large upload
        # doesn't buffer in worker memory; enforces a hard size cap and
        # derives the stored filename from a basename-only resolver instead
        # of brittle string replacement.
        try:
            sid = uuid.UUID(session_id)
        except ValueError as e:
            raise HTTPException(400, f"invalid session id: {e}") from e

        in_dir = _session_dir(sid) / "in"
        in_dir.mkdir(parents=True, exist_ok=True)

        # Path(...).name strips any directory components, so even an input
        # like "../../etc/passwd" becomes "passwd"; reject empty / dotfile /
        # already-traversal-like residues.
        if name:
            safe = Path(name).name.lstrip(".") or f"upload_{int(time.time())}.bin"
        else:
            safe = f"upload_{int(time.time())}.bin"
        target = in_dir / safe
        try:
            target.relative_to(in_dir.resolve())
        except ValueError as e:
            raise HTTPException(400, "filename escapes upload dir") from e

        total = 0
        try:
            with target.open("wb") as fh:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        fh.close()
                        target.unlink(missing_ok=True)
                        raise HTTPException(
                            413,
                            f"upload exceeds {MAX_UPLOAD_BYTES} bytes",
                        )
                    fh.write(chunk)
        except HTTPException:
            raise
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return {"path": str(target), "size": total, "session_id": str(sid)}

    # ---------- artifacts ------------------------------------------------
    @app.get("/artifacts/{session_id}")
    async def list_artifacts(session_id: str) -> list[dict[str, Any]]:
        try:
            sid = uuid.UUID(session_id)
        except ValueError as e:
            raise HTTPException(400, f"invalid session id: {e}") from e
        base = _session_dir(sid)
        out: list[dict[str, Any]] = []
        for rel in list_session_artifacts(sid):
            p = base / rel
            mt = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
            out.append({
                "path": rel,
                "size": p.stat().st_size if p.exists() else 0,
                "mime": mt,
                "url": f"/artifacts/{session_id}/{rel}",
            })
        return out

    @app.get("/artifacts/{session_id}/{path:path}")
    async def get_artifact(session_id: str, path: str) -> FileResponse:
        try:
            sid = uuid.UUID(session_id)
        except ValueError as e:
            raise HTTPException(400, f"invalid session id: {e}") from e
        base = _session_dir(sid).resolve()
        target = (base / path).resolve()
        try:
            target.relative_to(base)
        except ValueError as e:
            raise HTTPException(400, "path escapes session dir") from e
        if not target.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(str(target))

    # ---------- websocket -----------------------------------------------
    @app.websocket("/ws/sessions/{session_id}/chat")
    async def ws_chat(ws: WebSocket, session_id: str) -> None:
        await ws.accept()
        try:
            auth.require_ws(ws.query_params.get("key"))
        except HTTPException:
            await ws.send_json({"type": "error", "error": "invalid key"})
            await ws.close(code=4401)
            return
        try:
            sid = uuid.UUID(session_id)
        except ValueError:
            await ws.send_json({"type": "error", "error": "invalid session id"})
            await ws.close()
            return
        handle = await sm.load(sid)
        loop_ = asyncio.get_running_loop()

        def ws_send(name: str, payload: dict[str, Any]) -> None:
            # The controller emits events from inside this same event loop.
            # Using run_coroutine_threadsafe(...).result() would deadlock the
            # loop on its own future, so schedule the send and return
            # immediately. call_soon_threadsafe also keeps this safe if the
            # caller ever ends up on a different thread.
            async def _send() -> None:
                with contextlib.suppress(Exception):
                    await ws.send_json({"type": name, "payload": payload})

            loop_.call_soon_threadsafe(lambda: loop_.create_task(_send()))

        # Fan out controller events to BOTH the trace writer (default sink)
        # and the WS so workspace/traces/<sid>.jsonl is populated for
        # web-driven sessions, not only CLI-driven ones.
        agent = AgentLoop(handle)
        trace_sink = agent._on_event

        def fanout(name: str, payload: dict[str, Any]) -> None:
            with contextlib.suppress(Exception):
                trace_sink(name, payload)
            ws_send(name, payload)

        agent._on_event = fanout  # type: ignore[method-assign]
        agent.controller.on_event = fanout
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                    user_text = msg.get("message", "")
                except Exception:
                    user_text = raw
                if not user_text:
                    continue
                await ws.send_json({"type": "ack", "message": user_text})
                res = await agent.step(user_text)
                # publish artifact list after the turn so UIs can refresh
                arts = list_session_artifacts(sid)
                await ws.send_json({"type": "artifacts", "payload": {"items": arts}})
                await ws.send_json({
                    "type": "final",
                    "text": res.text,
                    "tool_calls": res.tool_calls,
                    "usage": {
                        "input_tokens": res.usage.input_tokens,
                        "output_tokens": res.usage.output_tokens,
                        "cost_usd": res.usage.cost_usd,
                    },
                })
        except WebSocketDisconnect:
            log.info("ws.disconnect", session_id=session_id)
        except Exception as e:
            log.warning("ws.error", err=str(e))
            with contextlib.suppress(Exception):
                await ws.send_json({"type": "error", "error": str(e)})

    return app


app = create_app()


def run() -> None:
    s = get_settings()
    uvicorn.run("jazz_guru.server:app", host=s.jg_host, port=s.jg_port, reload=False)


if __name__ == "__main__":
    run()
