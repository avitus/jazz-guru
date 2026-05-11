// jazz-guru web client. Vanilla JS, no build step.
(() => {
  const $ = (sel) => document.querySelector(sel);
  const fmt = (b) => (b > 1024 ? (b / 1024).toFixed(1) + 'k' : b + 'B');
  const stamp = () => new Date().toTimeString().slice(0, 8);

  // The API key may arrive once in the page URL (`?key=...`) for the
  // initial hand-off; we move it into sessionStorage immediately and
  // strip it from the URL via history.replaceState so it doesn't leak
  // into browser history, copied links, or Referer headers on outbound
  // fetches.
  const apiKey = (() => {
    const params = new URLSearchParams(location.search);
    const fromUrl = params.get('key') || '';
    let key = fromUrl || sessionStorage.getItem('jg_api_key') || '';
    if (fromUrl) {
      try { sessionStorage.setItem('jg_api_key', fromUrl); } catch {}
      params.delete('key');
      const qs = params.toString();
      history.replaceState(null, '', `${location.pathname}${qs ? `?${qs}` : ''}${location.hash}`);
    }
    return key;
  })();
  const headers = () => apiKey ? { 'x-api-key': apiKey, 'content-type': 'application/json' }
                                : { 'content-type': 'application/json' };

  let sessionId = localStorage.getItem('jg_session') || null;
  let ws = null;
  let osmd = null;
  let mediaRecorder = null;
  let recChunks = [];
  let recording = false;

  const ui = {
    msgs: $('#messages'),
    events: $('#events'),
    artifacts: $('#artifacts'),
    score: $('#score'),
    audio: $('#audio'),
    np: $('#now-playing'),
    label: $('#session-label'),
    status: $('#status'),
    input: $('#input'),
    rec: $('#rec-toggle'),
  };

  function setStatus(text, ok) {
    ui.status.textContent = text;
    ui.status.classList.toggle('ok', !!ok);
  }

  function logMsg(who, body) {
    const div = document.createElement('div');
    div.className = `msg ${who}`;
    div.innerHTML = `<div class="who">${who}</div><div class="body"></div>`;
    div.querySelector('.body').textContent = body;
    ui.msgs.appendChild(div);
    ui.msgs.scrollTop = ui.msgs.scrollHeight;
  }

  function logEvent(tag, body) {
    const div = document.createElement('div');
    div.className = 'e';
    div.innerHTML = `<div class="ts">${stamp()}</div><div class="tag ${tag}">${tag}</div><div class="body"></div>`;
    div.querySelector('.body').textContent = body;
    ui.events.appendChild(div);
    ui.events.scrollTop = ui.events.scrollHeight;
  }

  // ----- artifacts ----------------------------------------------------
  async function refreshArtifacts() {
    if (!sessionId) return;
    try {
      const r = await fetch(`/artifacts/${sessionId}`, { headers: headers() });
      if (!r.ok) return;
      const items = await r.json();
      ui.artifacts.innerHTML = '';
      if (!items.length) {
        ui.artifacts.innerHTML = '<a><span>no artifacts yet</span></a>';
        return;
      }
      for (const a of items) {
        const link = document.createElement('a');
        const size = document.createElement('span');
        size.className = 'size';
        size.textContent = fmt(a.size);
        const path = document.createElement('span');
        path.textContent = a.path;
        link.appendChild(size);
        link.appendChild(path);
        link.onclick = (ev) => { ev.preventDefault(); openArtifact(a); };
        ui.artifacts.appendChild(link);
      }
    } catch (e) { /* ignore */ }
  }

  // Track the last object URL so we can revoke it on next playback —
  // otherwise each artifact tap leaks a Blob URL.
  let lastAudioObjectUrl = null;

  async function fetchArtifactBlob(url) {
    // Authenticated fetch via x-api-key header — never via `?key=` query
    // string, so credentials don't end up in browser history / proxy logs.
    const r = await fetch(url, { headers: headers() });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.blob();
  }

  async function openArtifact(a) {
    const url = a.url; // server-relative; auth comes from headers()
    const ext = (a.path.split('.').pop() || '').toLowerCase();
    if (['wav', 'flac', 'mp3', 'ogg'].includes(ext)) {
      try {
        const blob = await fetchArtifactBlob(url);
        if (lastAudioObjectUrl) URL.revokeObjectURL(lastAudioObjectUrl);
        lastAudioObjectUrl = URL.createObjectURL(blob);
        ui.audio.src = lastAudioObjectUrl;
        ui.np.textContent = `playing ${a.path}`;
        ui.audio.play().catch(() => {});
      } catch (e) {
        logEvent('error', `audio load failed: ${e}`);
      }
    } else if (ext === 'mxl' || ext === 'xml' || ext === 'musicxml') {
      try {
        const buf = await (await fetch(url, { headers: headers() })).arrayBuffer();
        await renderScore(buf, ext);
      } catch (e) {
        logEvent('error', `score render failed: ${e}`);
      }
    } else if (ext === 'mid' || ext === 'midi') {
      logEvent('llm', `MIDI cannot be auto-rendered in browser; render it to .wav via render_midi`);
      try {
        const blob = await fetchArtifactBlob(url);
        window.open(URL.createObjectURL(blob), '_blank');
      } catch (e) {
        logEvent('error', `open failed: ${e}`);
      }
    } else {
      try {
        const blob = await fetchArtifactBlob(url);
        window.open(URL.createObjectURL(blob), '_blank');
      } catch (e) {
        logEvent('error', `open failed: ${e}`);
      }
    }
  }

  async function renderScore(buf, ext) {
    if (!osmd) {
      ui.score.innerHTML = '<div id="osmd"></div>';
      osmd = new opensheetmusicdisplay.OpenSheetMusicDisplay('osmd', {
        autoResize: true, drawTitle: true, backend: 'svg',
      });
    }
    let xml;
    if (ext === 'mxl') {
      // .mxl is a zip; OSMD can't unzip natively. Send back through server endpoint
      // that returns the .xml form, or rely on OSMD's own zip handling via base64.
      // OSMD accepts compressed mxl when passed as binary string starting with PK.
      const u8 = new Uint8Array(buf);
      let bin = '';
      for (let i = 0; i < u8.length; i++) bin += String.fromCharCode(u8[i]);
      xml = bin;
    } else {
      xml = new TextDecoder('utf-8').decode(buf);
    }
    await osmd.load(xml);
    osmd.render();
  }

  // ----- websocket / chat --------------------------------------------
  function connectWs() {
    if (!sessionId) return;
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    // Send the API key via Sec-WebSocket-Protocol (["bearer", "<key>"])
    // instead of a `?key=` query string — keeps the secret out of browser
    // history, proxy logs, and trace captures.
    const url = `${proto}://${location.host}/ws/sessions/${sessionId}/chat`;
    // Detach the previous socket's handlers and close it before installing
    // the new one so late events from the prior socket can't overwrite
    // status or inject stale events into the cleared UI on a fast
    // new-session flow.
    if (ws) {
      ws.onopen = ws.onclose = ws.onerror = ws.onmessage = null;
      try { ws.close(); } catch {}
    }
    const socket = apiKey ? new WebSocket(url, ['bearer', apiKey]) : new WebSocket(url);
    const boundSessionId = sessionId;
    ws = socket;
    // Each handler ignores calls if the global `ws` has moved on or the
    // session was switched out underneath it.
    const isCurrent = () => ws === socket && sessionId === boundSessionId;
    socket.onopen = () => {
      if (!isCurrent()) return;
      setStatus(`connected · session ${boundSessionId.slice(0, 8)}…`, true);
    };
    socket.onclose = () => { if (isCurrent()) setStatus('disconnected'); };
    socket.onerror = () => { if (isCurrent()) setStatus('error'); };
    socket.onmessage = (ev) => {
      if (!isCurrent()) return;
      let evt; try { evt = JSON.parse(ev.data); } catch { return; }
      handleEvent(evt);
    };
  }

  function handleEvent(evt) {
    const t = evt.type;
    const p = evt.payload || {};
    if (t === 'ack') return;
    if (t === 'tool_use') {
      const args = JSON.stringify(p.input || {}).slice(0, 200);
      logEvent('tool_use', `${p.name}  ${args}`);
    } else if (t === 'tool_result') {
      logEvent('tool_result', `${p.name}  ${p.ok === false ? 'error: ' + p.error : 'ok'}`);
    } else if (t === 'llm_request') {
      logEvent('llm', `→ round ${p.round}`);
    } else if (t === 'llm_response') {
      const u = p.usage || {};
      logEvent('llm', `← stop=${p.stop_reason} in=${u.in} out=${u.out}`);
    } else if (t === 'artifacts') {
      refreshArtifacts();
    } else if (t === 'final') {
      if (evt.text) logMsg('agent', evt.text);
      const u = evt.usage || {};
      const cost = (u.cost_usd || 0).toFixed(4);
      logEvent('llm', `final  tools=${evt.tool_calls}  in=${u.input_tokens} out=${u.output_tokens}  $${cost}`);
      // The server now ships non-fatal policy/tool errors that occurred
      // during the turn — surface each so they don't quietly disappear.
      for (const err of evt.errors || []) {
        logEvent('error', typeof err === 'string' ? err : JSON.stringify(err));
      }
      refreshArtifacts();
    } else if (t === 'error') {
      const msg = evt.error || (evt.payload && evt.payload.error) || 'error';
      const phase = evt.payload && evt.payload.phase;
      logEvent('error', phase ? `${phase}: ${msg}` : msg);
    }
  }

  async function sendMessage(text) {
    if (!text.trim()) return;
    if (!sessionId) await ensureSession();
    if (!ws || ws.readyState !== WebSocket.OPEN) connectWs();
    // wait up to ~3s for the connection to actually open before giving up
    for (let i = 0; i < 10 && (!ws || ws.readyState === WebSocket.CONNECTING); i++) {
      await new Promise((r) => setTimeout(r, 300));
    }
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      logEvent('error', 'cannot send: websocket not open');
      return;
    }
    logMsg('user', text);
    ws.send(JSON.stringify({ message: text }));
  }

  async function ensureSession() {
    const r = await fetch('/sessions', {
      method: 'POST',
      headers: headers(),
      body: JSON.stringify({ title: 'web' }),
    });
    if (!r.ok) {
      logEvent('error', `create session: ${r.status} ${await r.text()}`);
      return;
    }
    const out = await r.json();
    sessionId = out.id;
    localStorage.setItem('jg_session', sessionId);
    ui.label.textContent = `session ${sessionId.slice(0, 8)}…`;
    connectWs();
    refreshArtifacts();
  }

  // ----- mic capture (via getUserMedia) ------------------------------
  async function toggleRecord() {
    if (recording) return stopRecord();
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      recChunks = [];
      mediaRecorder = new MediaRecorder(stream, { mimeType: 'audio/webm' });
      mediaRecorder.ondataavailable = (e) => { if (e.data.size) recChunks.push(e.data); };
      mediaRecorder.onstop = () => uploadRecording(stream);
      mediaRecorder.start();
      recording = true;
      ui.rec.classList.add('recording');
      ui.rec.textContent = '■ stop';
      logEvent('llm', 'recording…');
    } catch (e) {
      logEvent('error', `mic access failed: ${e.message || e}`);
    }
  }

  function stopRecord() {
    if (!mediaRecorder) return;
    recording = false;
    ui.rec.classList.remove('recording');
    ui.rec.textContent = '● record';
    mediaRecorder.stop();
  }

  async function uploadRecording(stream) {
    stream.getTracks().forEach((t) => t.stop());
    const blob = new Blob(recChunks, { type: 'audio/webm' });
    if (!sessionId) await ensureSession();
    if (!sessionId) {
      // ensureSession already logged its own error; bail out so we don't
      // turn that failure into a second misleading "/uploads/null" 400.
      logEvent('error', 'upload aborted: no session');
      return;
    }
    const filename = `web_${Date.now()}.webm`;
    const r = await fetch(`/uploads/${sessionId}?name=${encodeURIComponent(filename)}`, {
      method: 'POST',
      headers: { ...headers(), 'content-type': 'application/octet-stream' },
      body: blob,
    });
    if (!r.ok) {
      logEvent('error', `upload failed: ${r.status} ${await r.text()}`);
      return;
    }
    const out = await r.json();
    logEvent('llm', `uploaded ${out.path}`);
    sendMessage(
      `[audio recording at ${out.path}] use audio_analyze on this path and respond about what was played.`
    );
  }

  // ----- wire up ------------------------------------------------------
  $('#composer').addEventListener('submit', (ev) => {
    ev.preventDefault();
    const text = ui.input.value;
    ui.input.value = '';
    sendMessage(text);
  });

  $('#new-session').addEventListener('click', async () => {
    sessionId = null;
    localStorage.removeItem('jg_session');
    ui.msgs.innerHTML = '';
    ui.events.innerHTML = '';
    ui.artifacts.innerHTML = '';
    ui.label.textContent = 'no session';
    if (ws) try { ws.close(); } catch {}
    await ensureSession();
  });

  ui.rec.addEventListener('click', toggleRecord);

  // ----- column resize gutter ---------------------------------------
  // Drag (or arrow-key) the divider between chat and panels. Persists the
  // chat column's px width to localStorage so it survives reloads. Min
  // widths keep both sides usable on resize/zoom; the CSS media query at
  // 1100px collapses to a single column and hides the gutter.
  (() => {
    const main = document.querySelector('main');
    const gutter = document.getElementById('gutter');
    if (!main || !gutter) return;

    const STORE_KEY = 'jg_chat_w_px';
    const MIN_LEFT = 320;
    const MIN_RIGHT = 320;
    const GUTTER_AND_GAPS = 22; // 6px gutter + 2 * 8px gaps

    const clamp = (px) => {
      const total = main.clientWidth - parseFloat(getComputedStyle(main).paddingLeft) * 2;
      const max = Math.max(MIN_LEFT, total - GUTTER_AND_GAPS - MIN_RIGHT);
      return Math.max(MIN_LEFT, Math.min(max, px));
    };
    const apply = (px) => { main.style.setProperty('--chat-w', `${px}px`); };
    const persist = (px) => { try { localStorage.setItem(STORE_KEY, String(px)); } catch {} };

    // Restore prior width if any.
    const saved = parseFloat(localStorage.getItem(STORE_KEY) || '');
    if (Number.isFinite(saved) && saved > 0) apply(clamp(saved));

    let dragging = false;
    let pointerId = null;

    gutter.addEventListener('pointerdown', (ev) => {
      if (ev.button !== 0 && ev.pointerType === 'mouse') return;
      dragging = true;
      pointerId = ev.pointerId;
      gutter.setPointerCapture(pointerId);
      gutter.classList.add('dragging');
      document.body.classList.add('resizing-cols');
      ev.preventDefault();
    });

    gutter.addEventListener('pointermove', (ev) => {
      if (!dragging) return;
      const rect = main.getBoundingClientRect();
      const padLeft = parseFloat(getComputedStyle(main).paddingLeft) || 0;
      const px = clamp(ev.clientX - rect.left - padLeft);
      apply(px);
    });

    const endDrag = () => {
      if (!dragging) return;
      dragging = false;
      gutter.classList.remove('dragging');
      document.body.classList.remove('resizing-cols');
      if (pointerId !== null) { try { gutter.releasePointerCapture(pointerId); } catch {} }
      pointerId = null;
      // Save current rendered width.
      const cur = parseFloat(getComputedStyle(main).getPropertyValue('--chat-w'));
      if (Number.isFinite(cur) && cur > 0) persist(cur);
    };
    gutter.addEventListener('pointerup', endDrag);
    gutter.addEventListener('pointercancel', endDrag);

    // Keyboard nudge for accessibility.
    gutter.addEventListener('keydown', (ev) => {
      const step = ev.shiftKey ? 64 : 16;
      let cur = parseFloat(getComputedStyle(main).getPropertyValue('--chat-w'));
      if (!Number.isFinite(cur) || cur <= 0) {
        // No explicit width yet — read the current rendered chat column.
        const chat = document.querySelector('.chat');
        cur = chat ? chat.getBoundingClientRect().width : 600;
      }
      let next = null;
      if (ev.key === 'ArrowLeft') next = cur - step;
      else if (ev.key === 'ArrowRight') next = cur + step;
      else if (ev.key === 'Home') next = MIN_LEFT;
      else if (ev.key === 'End') next = clamp(1e6);
      if (next !== null) {
        ev.preventDefault();
        const px = clamp(next);
        apply(px);
        persist(px);
      }
    });

    // Re-clamp on viewport resize so a saved width can't exceed the new bounds.
    window.addEventListener('resize', () => {
      const cur = parseFloat(getComputedStyle(main).getPropertyValue('--chat-w'));
      if (Number.isFinite(cur) && cur > 0) apply(clamp(cur));
    });
  })();

  // boot
  (async () => {
    setStatus('connecting…');
    if (sessionId) {
      ui.label.textContent = `session ${sessionId.slice(0, 8)}…`;
      connectWs();
      await refreshArtifacts();
    } else {
      await ensureSession();
    }
  })();
})();
