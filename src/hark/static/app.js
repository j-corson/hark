// hark page: capture audio, show the live document and transcript, send edits.
(function () {
  const $ = (id) => document.getElementById(id);
  const { blocks, esc } = window.MD;
  const FRESH_MS = 25000;
  const SOURCE_ID = { mic: 0, call: 1 };

  const st = {
    ws: null, session: null, status: {}, doc: "", version: 0, lines: [], speakers: {}, sessions: [],
    modes: {}, fresh: new Map(), editing: null, rawOpen: false, pendingRender: false,
    caps: {}, ctx: null, workletReady: null, wake: null, lastUserScroll: 0,
  };

  // ---------------------------------------------------------------- socket
  let retry = 0;
  function connect() {
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
    ws.binaryType = "arraybuffer";
    st.ws = ws;
    ws.onopen = () => {
      retry = 0;
      for (const src of Object.keys(st.caps)) send({ type: "start", source: src });
    };
    ws.onmessage = (e) => onMessage(JSON.parse(e.data));
    ws.onclose = () => {
      setStatusText("Reconnecting to hark…", "error");
      setTimeout(connect, Math.min(8000, 500 * 2 ** retry++));
    };
  }
  const send = (m) => st.ws && st.ws.readyState === 1 && st.ws.send(JSON.stringify(m));

  function onMessage(m) {
    if (m.type === "state") {
      st.session = m.session; st.sessions = m.sessions;
      st.doc = m.session.doc; st.version = m.session.version;
      st.lines = m.session.lines; st.speakers = m.session.speakers;
      $("modeSel").value = m.session.mode;
      $("undoBtn").disabled = !m.session.can_undo;
      renderDoc(); renderTranscript(); renderSpeakers(); renderTitle(m.session.title);
    } else if (m.type === "doc") {
      if (m.by === "claude" && m.changed.length) {
        const ls = m.doc.split("\n"), now = Date.now();
        for (const i of m.changed) if (ls[i] && ls[i].trim()) st.fresh.set(ls[i].trim(), now);
      }
      st.doc = m.doc; st.version = m.version;
      $("undoBtn").disabled = !m.can_undo;
      renderTitle(m.title);
      renderDoc(m.by === "claude");
    } else if (m.type === "line") {
      st.speakers = m.speakers;
      st.lines.push(m.line);
      appendLine(m.line);
      renderSpeakers();
    } else if (m.type === "status") {
      st.status = m; renderStatus(); markPending();
    } else if (m.type === "toast") {
      toast(m.text);
    }
  }

  // ---------------------------------------------------------------- document
  function renderTitle(t) { document.title = t ? `${t} – hark` : "hark"; }

  function freshAge(start, end, lines) {
    const now = Date.now();
    let best = null;
    for (let i = start; i < end; i++) {
      const t = st.fresh.get((lines[i] || "").trim());
      if (t && now - t < FRESH_MS && (best === null || t > best)) best = t;
    }
    return best === null ? null : (now - best) / 1000;
  }

  function renderDoc(scrollToFresh) {
    if (st.editing || st.rawOpen) { st.pendingRender = true; return; }
    st.pendingRender = false;
    const now = Date.now();
    for (const [k, t] of st.fresh) if (now - t > FRESH_MS) st.fresh.delete(k);
    const lines = st.doc.split("\n");
    const bl = blocks(st.doc);
    const el = $("doc");
    el.innerHTML = "";
    let firstFresh = null;
    bl.forEach((b, idx) => {
      const tmp = document.createElement("div");
      tmp.innerHTML = b.html;
      const node = tmp.firstElementChild;
      node.dataset.start = b.start; node.dataset.end = b.end;
      const age = freshAge(b.start, b.end, lines);
      if (age !== null) {
        node.classList.add("fresh");
        node.style.setProperty("--age", `-${age.toFixed(1)}s`);
        if (!firstFresh) firstFresh = node;
      }
      el.appendChild(node);
      // An empty section gets a placeholder you can click to start writing in it.
      const next = bl[idx + 1];
      if (b.kind === "h" && b.level >= 2 && (!next || (next.kind === "h" && next.level <= b.level))) {
        const ph = document.createElement("p");
        ph.className = "emptysec";
        ph.textContent = "Nothing yet";
        ph.dataset.start = b.end; ph.dataset.end = b.end; ph.dataset.insert = "1";
        el.appendChild(ph);
      }
    });
    const add = document.createElement("button");
    add.type = "button"; add.className = "quiet add"; add.textContent = "Add a line";
    add.dataset.start = lines.length; add.dataset.end = lines.length; add.dataset.insert = "1";
    el.appendChild(add);
    if (scrollToFresh && firstFresh && Date.now() - st.lastUserScroll > 10000) {
      const r = firstFresh.getBoundingClientRect(), pane = $("docPane").getBoundingClientRect();
      if (r.top < pane.top || r.bottom > pane.bottom) firstFresh.scrollIntoView({ block: "center" });
    }
  }
  $("docPane").addEventListener("wheel", () => (st.lastUserScroll = Date.now()), { passive: true });
  $("docPane").addEventListener("touchmove", () => (st.lastUserScroll = Date.now()), { passive: true });
  setInterval(() => { if (st.fresh.size && !st.editing && !st.rawOpen) renderDoc(false); }, 15000);

  $("doc").addEventListener("click", (e) => {
    if (e.target.closest("a")) return;
    const box = e.target.closest("input[type=checkbox]");
    const blk = e.target.closest("[data-start]");
    if (!blk || st.editing) return;
    const start = +blk.dataset.start, end = +blk.dataset.end;
    const lines = st.doc.split("\n");
    if (box) {
      e.preventDefault();
      const old = lines[start];
      const text = old.replace(/\[([ xX])\]/, (_, c) => (c === " " ? "[x]" : "[ ]"));
      send({ type: "edit_lines", start, end, old, text });
      return;
    }
    openInline(blk, start, end, blk.dataset.insert === "1");
  });

  function openInline(node, start, end, insert) {
    const lines = st.doc.split("\n");
    const old = lines.slice(start, end).join("\n");
    const ta = document.createElement("textarea");
    ta.className = "inline";
    ta.value = insert ? "- " : old;
    ta.setAttribute("aria-label", "Edit this part of the document");
    const size = () => { ta.style.height = "auto"; ta.style.height = ta.scrollHeight + 2 + "px"; };
    node.replaceWith(ta);
    st.editing = { start, end, old, insert };
    send({ type: "editing", on: true });
    size(); ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
    let done = false;
    const finish = (save) => {
      if (done) return; done = true;
      const ed = st.editing; st.editing = null;
      const text = ta.value.replace(/\s+$/, "");
      if (save && !(ed.insert && (text === "-" || text === "")) && text !== ed.old) {
        send({ type: "edit_lines", start: ed.start, end: ed.end, old: ed.old, text });
      } else {
        send({ type: "editing", on: false });
      }
      renderDoc(false);
    };
    ta.addEventListener("input", size);
    ta.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); finish(true); }
      else if (e.key === "Escape") { e.preventDefault(); finish(false); }
    });
    ta.addEventListener("blur", () => finish(true));
  }

  // Full-text editing
  function openRaw() {
    st.rawOpen = true;
    $("raw").value = st.doc;
    $("doc").hidden = true; $("rawWrap").hidden = false;
    send({ type: "editing", on: true });
    $("raw").focus();
  }
  function closeRaw(save) {
    if (save) send({ type: "set_doc", doc: $("raw").value, done: true });
    else send({ type: "editing", on: false });
    st.rawOpen = false;
    $("doc").hidden = false; $("rawWrap").hidden = true;
    renderDoc(false);
  }
  $("rawSave").onclick = () => closeRaw(true);
  $("rawCancel").onclick = () => closeRaw(false);
  $("raw").addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); closeRaw(true); }
  });

  // ---------------------------------------------------------------- transcript
  const fmtTime = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const spk = (id) => st.speakers[id] || { name: "Unknown", color: "#888", auto: true };

  function lineNode(l) {
    const li = document.createElement("li");
    const s = spk(l.speaker);
    li.dataset.id = l.id;
    li.className = l.kind === "typed" ? "typed" : "";
    li.style.setProperty("--c", l.kind === "typed" ? "var(--act)" : s.color);
    const src = l.source === "call" ? '<span class="src">call</span>' : "";
    li.innerHTML = `<div><button type="button" class="who" data-line="${l.id}">${esc(s.name)}</button><time>${fmtTime(l.t)}</time>${src}</div><div>${esc(l.text)}</div>`;
    return li;
  }
  function renderTranscript() {
    const ol = $("transcript");
    ol.innerHTML = "";
    st.lines.forEach((l) => ol.appendChild(lineNode(l)));
    $("trEmpty").hidden = st.lines.length > 0;
    markPending();
    $("sidePane").scrollTop = $("sidePane").scrollHeight;
  }
  function appendLine(l) {
    const pane = $("sidePane");
    const atBottom = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 80;
    $("transcript").appendChild(lineNode(l));
    $("trEmpty").hidden = true;
    markPending();
    if (atBottom) pane.scrollTop = pane.scrollHeight;
  }
  // Lines Claude has already worked into the document are dimmed.
  function markPending() {
    const items = $("transcript").children;
    const pending = st.status.pending || 0;
    for (let i = 0; i < items.length; i++) items[i].classList.toggle("done", i < items.length - pending);
  }
  function renderSpeakers() {
    const box = $("speakers");
    box.innerHTML = "";
    const used = new Set(st.lines.filter((l) => l.kind === "speech").map((l) => l.speaker));
    for (const [id, s] of Object.entries(st.speakers)) {
      if (!used.has(id)) continue;
      const b = document.createElement("button");
      b.type = "button"; b.className = "chip" + (s.auto ? " auto" : "");
      b.style.setProperty("--c", s.color);
      b.textContent = s.name; b.title = "Rename this speaker";
      b.onclick = () => {
        const name = prompt(`Rename "${s.name}" to… (use an existing name to merge them)`, s.auto ? "" : s.name);
        if (name && name.trim()) send({ type: "rename", id, name: name.trim() });
      };
      box.appendChild(b);
    }
    // Refresh names/colours on existing lines.
    for (const li of $("transcript").children) {
      const l = st.lines.find((x) => x.id === +li.dataset.id);
      if (!l || l.kind === "typed") continue;
      const s = spk(l.speaker);
      li.style.setProperty("--c", s.color);
      li.querySelector(".who").textContent = s.name;
    }
  }
  $("transcript").addEventListener("click", (e) => {
    const b = e.target.closest(".who");
    if (!b) return;
    const name = prompt("Who said this?");
    if (name && name.trim()) send({ type: "reassign", line: +b.dataset.line, name: name.trim() });
  });

  // ---------------------------------------------------------------- status
  function setStatusText(text, cls) {
    const el = $("status");
    el.textContent = text; el.className = "status " + (cls || "");
  }
  function renderStatus() {
    const s = st.status, c = s.claude || {};
    const listening = Object.keys(st.caps).length > 0;
    $("startBtn").classList.toggle("on", listening);
    $("startBtn").textContent = listening ? "Stop" : "Start listening";
    let text = "", cls = "";
    if (c.state === "thinking") { text = `Claude is updating the document (${c.detail})`; cls = "thinking"; }
    else if (s.editing) { text = "Claude waits while you edit"; cls = "paused"; }
    else if (c.state === "error") { text = `Claude couldn't update: ${c.detail}`; cls = "error"; }
    else if (listening) text = s.pending ? `Listening. ${s.pending} new line${s.pending === 1 ? "" : "s"} for the next update.` : (c.detail ? `Listening. Last update: ${c.detail}.` : "Listening.");
    else text = c.detail ? `Last update: ${c.detail}.` : "Not listening.";
    if (s.backlog > 4) text += ` Transcription is ${Math.round(s.backlog)}s behind.`;
    setStatusText(text, cls);
  }

  // ---------------------------------------------------------------- audio
  // Ask the browser for a 16 kHz context so it does the (high-quality) resampling.
  // Browsers that can't mix sample rates get a default context; the worklet resamples instead.
  async function ensureCtx(native) {
    if (!st.ctx) {
      try { st.ctx = native ? new AudioContext() : new AudioContext({ sampleRate: 16000 }); }
      catch (_) { st.ctx = new AudioContext(); }
      st.workletReady = st.ctx.audioWorklet.addModule("/static/worklet.js");
    }
    if (st.ctx.state === "suspended") await st.ctx.resume();
    await st.workletReady;
    return st.ctx;
  }

  function addMeter(source) {
    const m = document.createElement("span");
    m.className = "meter"; m.id = "meter-" + source;
    m.innerHTML = `<i></i>${source === "call" ? "call" : "mic"}`;
    $("meters").appendChild(m);
    return m.querySelector("i");
  }

  async function capture(source, stream) {
    let ctx = await ensureCtx(), input;
    try { input = ctx.createMediaStreamSource(stream); }
    catch (_) {
      if (ctx.sampleRate === 16000 && !Object.keys(st.caps).length) {
        await ctx.close(); st.ctx = null;
        ctx = await ensureCtx(true);
        input = ctx.createMediaStreamSource(stream);
      } else throw _;
    }
    const node = new AudioWorkletNode(ctx, "hark-capture");
    const sink = ctx.createGain(); sink.gain.value = 0;
    input.connect(node); node.connect(sink); sink.connect(ctx.destination);
    const bar = addMeter(source);
    const id = SOURCE_ID[source];
    node.port.onmessage = (e) => {
      const pcm = new Uint8Array(e.data.pcm);
      const buf = new Uint8Array(pcm.length + 1);
      buf[0] = id; buf.set(pcm, 1);
      if (st.ws && st.ws.readyState === 1 && st.ws.bufferedAmount < 2e6) st.ws.send(buf);
      bar.style.setProperty("--lv", Math.min(100, Math.sqrt(e.data.peak) * 130) + "%");
    };
    st.caps[source] = { stream, node, input, sink };
    send({ type: "start", source });
    for (const t of stream.getTracks()) t.addEventListener("ended", () => stopSource(source));
  }

  function stopSource(source) {
    const c = st.caps[source];
    if (!c) return;
    delete st.caps[source];
    c.stream.getTracks().forEach((t) => t.stop());
    try { c.input.disconnect(); c.node.disconnect(); c.sink.disconnect(); } catch (_) {}
    const m = $("meter-" + source); if (m) m.remove();
    send({ type: "stop", source });
    if (!Object.keys(st.caps).length) releaseWake();
    renderStatus();
  }

  async function start(kind) {
    hidePopovers();
    if (!navigator.mediaDevices || !window.isSecureContext) {
      toast("This browser blocks the microphone on insecure pages. Open the https:// link hark printed (run hark --lan).");
      return;
    }
    try {
      if (kind === "call") {
        const disp = await navigator.mediaDevices.getDisplayMedia({
          video: true, audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
          systemAudio: "include", selfBrowserSurface: "exclude", surfaceSwitching: "include",
        });
        const at = disp.getAudioTracks();
        if (!at.length) {
          disp.getTracks().forEach((t) => t.stop());
          toast("No call audio was shared. Choose the Teams tab (or your whole screen on Windows) and turn on “Share audio”.");
          return;
        }
        // Only the audio is used; when sharing stops, the call source stops too.
        const v = disp.getVideoTracks()[0];
        if (v) v.addEventListener("ended", () => stopSource("call"));
        await capture("call", new MediaStream(at));
        st.caps.call.stream = disp;
      }
      const mic = await navigator.mediaDevices.getUserMedia({
        // Noise suppression muddies the voice features used to tell speakers apart; the speech model copes with noise.
        audio: { echoCancellation: true, noiseSuppression: false, autoGainControl: true, channelCount: 1 },
      });
      await capture("mic", mic);
      holdWake();
      renderStatus();
    } catch (err) {
      Object.keys(st.caps).forEach(stopSource);
      toast(err && err.name === "NotAllowedError" ? "Microphone access was blocked. Allow it in the browser's site settings and try again." : `Couldn't start: ${err.message || err}`);
    }
  }

  async function holdWake() {
    try { if ("wakeLock" in navigator && !st.wake) st.wake = await navigator.wakeLock.request("screen"); } catch (_) {}
  }
  function releaseWake() { if (st.wake) { st.wake.release().catch(() => {}); st.wake = null; } }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && Object.keys(st.caps).length) { st.wake = null; holdWake(); }
  });

  const canShare = !!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia) && !/Android|iPhone|iPad/i.test(navigator.userAgent);
  $("startBtn").onclick = (e) => {
    e.stopPropagation();
    if (Object.keys(st.caps).length) { Object.keys(st.caps).forEach(stopSource); return; }
    if (!canShare) { start("mic"); return; }
    togglePopover("sourceMenu");
  };
  $("sourceMenu").addEventListener("click", (e) => {
    const b = e.target.closest("[data-start]");
    if (b) start(b.dataset.start);
  });

  // ---------------------------------------------------------------- menus & dialogs
  function hidePopovers() { document.querySelectorAll(".popover").forEach((p) => (p.hidden = true)); }
  function togglePopover(id) {
    const p = $(id), was = p.hidden;
    hidePopovers(); p.hidden = !was;
    if (!p.hidden) p.querySelector("button").focus();
  }
  document.addEventListener("click", (e) => { if (!e.target.closest(".popover")) hidePopovers(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") hidePopovers(); });
  $("menuBtn").onclick = (e) => { e.stopPropagation(); togglePopover("moreMenu"); };
  $("undoBtn").onclick = () => send({ type: "undo" });

  $("moreMenu").addEventListener("click", async (e) => {
    const b = e.target.closest("[data-act]");
    if (!b) return;
    hidePopovers();
    const act = b.dataset.act;
    if (act === "edit") openRaw();
    else if (act === "update") { send({ type: "update_now" }); toast("Asked Claude to update now."); }
    else if (act === "copy") {
      try { await navigator.clipboard.writeText(st.doc); toast("Copied the document as Markdown."); }
      catch (_) { toast("This browser won't allow copying here. Use Download .md instead."); }
    } else if (act === "download") {
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([st.doc], { type: "text/markdown" }));
      a.download = ((st.session && st.session.title) || "notes").replace(/[^\w\- ]+/g, "").trim().replace(/\s+/g, "-") + ".md";
      a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    } else if (act === "brief") {
      $("briefText").value = st.session.brief;
      $("intervalIn").value = st.session.interval;
      $("briefDlg").showModal();
    } else if (act === "sessions") {
      renderSessions(); $("sessDlg").showModal();
    } else if (act === "new") {
      if (Object.keys(st.caps).length && !confirm("Start a new document? Listening continues into the new one.")) return;
      send({ type: "new_session", mode: $("modeSel").value });
      st.fresh.clear();
    }
  });
  $("briefDlg").addEventListener("close", () => {
    if ($("briefDlg").returnValue === "ok") send({ type: "set_brief", brief: $("briefText").value, interval: +$("intervalIn").value });
  });

  function renderSessions() {
    $("sessDir").textContent = st.session ? `Saved as Markdown in ${st.session.dir.replace(/[\\/][^\\/]+$/, "")}` : "";
    const ul = $("sessList"); ul.innerHTML = "";
    if (!st.sessions.length) { ul.innerHTML = '<li class="empty">No saved sessions yet.</li>'; return; }
    for (const s of st.sessions) {
      const li = document.createElement("li");
      const when = new Date(s.updated * 1000).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
      li.innerHTML = `<button type="button" value="cancel" class="${st.session && s.id === st.session.id ? "cur" : ""}"><span>${esc(s.title)}</span><time>${when}</time></button>`;
      li.firstChild.onclick = () => { send({ type: "open_session", id: s.id }); st.fresh.clear(); $("sessDlg").close(); };
      ul.appendChild(li);
    }
  }

  $("modeSel").onchange = () => send({ type: "set_mode", mode: $("modeSel").value });

  $("sayForm").onsubmit = (e) => {
    e.preventDefault();
    const v = $("sayInput").value.trim();
    if (!v) return;
    send({ type: "say", text: v });
    $("sayInput").value = "";
  };

  $("tabBtn").onclick = () => {
    const on = document.body.classList.toggle("show-tr");
    $("tabBtn").textContent = on ? "Document" : "Transcript";
    if (on) $("sidePane").scrollTop = $("sidePane").scrollHeight;
  };

  let toastTimer;
  function toast(text) {
    const t = $("toast");
    t.textContent = text; t.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), 5000);
  }

  // ---------------------------------------------------------------- boot
  fetch("/modes").then((r) => r.json()).then((modes) => {
    st.modes = modes;
    const sel = $("modeSel");
    for (const [k, v] of Object.entries(modes)) sel.add(new Option(v.label, k));
    if (st.session) sel.value = st.session.mode;
  });
  if (!canShare) $("callOpt").hidden = true;
  connect();
})();
