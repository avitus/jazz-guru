from __future__ import annotations

import html as html_lib
import json
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from jazz_guru.config import get_settings


def _esc(s: object) -> str:
    """HTML-escape a value for safe inline rendering."""
    return html_lib.escape(str(s), quote=True)


def create_app() -> FastAPI:
    app = FastAPI(title="jazz-guru trace viewer")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        s = get_settings()
        files = sorted(Path(s.jg_trace_dir).glob("*.jsonl"))
        items = "".join(
            f'<li><a href="/sessions/{_esc(p.stem)}">{_esc(p.stem)}</a> '
            f'<span class="dim">({p.stat().st_size}B)</span></li>'
            for p in files
        )
        return f"""<!doctype html><meta charset=utf-8>
<title>jazz-guru traces</title>
<style>
body{{font:14px/1.4 system-ui;margin:24px;max-width:900px}}
.dim{{color:#888}}
li{{margin:4px 0}}
</style>
<h1>jazz-guru traces</h1>
<ul>{items or '<i>no traces</i>'}</ul>"""

    @app.get("/sessions/{sid}", response_class=HTMLResponse)
    async def session(sid: str) -> str:
        try:
            uuid.UUID(sid)
        except ValueError as e:
            raise HTTPException(400, "bad uuid") from e
        path = Path(get_settings().jg_trace_dir) / f"{sid}.jsonl"
        if not path.exists():
            raise HTTPException(404, "not found")
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rows.append(
                f'<tr><td class="dim">{_esc(rec.get("ts",""))}</td>'
                f'<td><b>{_esc(rec.get("type",""))}</b></td>'
                f'<td><pre>{_esc(json.dumps(rec.get("payload"), indent=2)[:1000])}</pre></td></tr>'
            )
        return f"""<!doctype html><meta charset=utf-8>
<title>{_esc(sid)}</title>
<style>
body{{font:13px/1.3 system-ui;margin:16px}}
table{{border-collapse:collapse;width:100%}}
td{{border-bottom:1px solid #eee;padding:6px;vertical-align:top}}
.dim{{color:#888;font-size:11px;white-space:nowrap}}
pre{{margin:0;white-space:pre-wrap;font-size:12px}}
</style>
<h1>{_esc(sid)}</h1>
<p><a href="/">&larr; back</a></p>
<table>{''.join(rows)}</table>"""

    return app


app = create_app()
