// jazz-guru web client. Vanilla JS, no build step.
(() => {
  const $ = (sel) => document.querySelector(sel);
  const fmt = (b) => (b > 1024 ? (b / 1024).toFixed(1) + 'k' : b + 'B');
  const stamp = () => new Date().toTimeString().slice(0, 8);

  const apiKey = new URLSearchParams(location.search).get('key') || '';
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
        link.innerHTML = `<span class="size">${fmt(a.size)}</span><span>${a.path}</span>`;
        link.onclick = (ev) => { ev.preventDefault(); openArtifact(a); };
        ui.artifacts.appendChild(link);
      }
    } catch (e) { /* ignore */ }
  }

  async function openArtifact(a) {
    const url = a.url + (apiKey ? `?key=${encodeURIComponent(apiKey)}` : '');
    const ext = (a.path.split('.').pop() || '').toLowerCase();
    if (['wav', 'flac', 'mp3', 'ogg'].includes(ext)) {
      ui.audio.src = url;
      ui.np.textContent = `playing ${a.path}`;
      ui.audio.play().catch(() => {});
    } else if (ext === 'mxl' || ext === 'xml' || ext === 'musicxml') {
      try {
        const buf = await (await fetch(url, { headers: headers() })).arrayBuffer();
        await renderScore(buf, ext);
      } catch (e) {
        logEvent('error', `score render failed: ${e}`);
      }
    } else if (ext === 'mid' || ext === 'midi') {
      logEvent('llm', `MIDI cannot be auto-rendered in browser; render it to .wav via render_midi`);
      window.open(url, '_blank');
    } else {
      window.open(url, '_blank');
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
    const keyq = apiKey ? `?key=${encodeURIComponent(apiKey)}` : '';
    ws = new WebSocket(`${proto}://${location.host}/ws/sessions/${sessionId}/chat${keyq}`);
    ws.onopen = () => setStatus(`connected · session ${sessionId.slice(0, 8)}…`, true);
    ws.onclose = () => setStatus('disconnected');
    ws.onerror = () => setStatus('error');
    ws.onmessage = (ev) => {
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
      refreshArtifacts();
    } else if (t === 'error') {
      logEvent('error', evt.error || 'error');
    }
  }

  async function sendMessage(text) {
    if (!text.trim()) return;
    if (!sessionId) await ensureSession();
    if (!ws || ws.readyState !== 1) connectWs();
    if (!ws || ws.readyState !== 1) {
      // wait briefly for connect
      await new Promise((r) => setTimeout(r, 300));
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
