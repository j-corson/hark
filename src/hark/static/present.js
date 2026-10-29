// hark room screen: a read-only, big-type view of whatever the group is on right now.
// It follows the conversation by watching which part of the document last changed,
// A section chosen by hand or by voice is held on every screen (hark keeps that choice
// and tells Claude, which then puts new points there). It never sends audio or edits.
(function () {
  const $ = (id) => document.getElementById(id);
  const { blocks, inline } = window.MD;
  const FRESH_MS = 25000;
  const AGREED_MS = 45000; // how long a new decision or action stays in the banner
  const BULK = 3; // an update touching more sections than this (an agenda, a rewrite) doesn't move the focus
  const FIELDS = ["question", "data", "approach", "done when", "track", "depends on", "who cares", "status"];

  const st = {
    sid: null, mode: "meeting", doc: "", P: null,
    touched: new Map(), // section key -> when it last changed
    fresh: new Map(), // line text -> when it appeared
    agreed: [], // new lines in Decisions / Actions: {text, kind, t}
    sections: new Set(), // heading lines of the sections this screen steps through, from hark (same order and numbers everywhere)
    reported: undefined, // what we last told hark we're showing, for "Claude, next section"
    stage: null, // heading line held on screen by choice, kept by hark for every screen; null = follow the talk
    elsewhere: null, // {names, t}: sections Claude changed while another one was held
    ws: null,
    shown: null, nav: [],
    research: new Map(), // id -> look-up job, for quick answers
    popped: new Set(), // quick answers already shown (or done before this screen opened)
    lookup: null, // {id, until}: the quick answer on screen now
    ends: null, // when the meeting is due to end, if set
  };

  // ---------------------------------------------------------------- socket
  let retry = 0;
  function connect() {
    const ws = (st.ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`));
    ws.onopen = () => { retry = 0; st.reported = undefined; $("live").classList.remove("lost"); };
    ws.onmessage = (e) => onMessage(JSON.parse(e.data));
    ws.onclose = () => {
      $("live").hidden = false; $("live").textContent = "Reconnecting…"; $("live").classList.add("lost");
      setTimeout(connect, Math.min(8000, 500 * 2 ** retry++));
    };
  }

  function onMessage(m) {
    if (m.type === "state") {
      if (m.session.id !== st.sid) {
        st.sid = m.session.id;
        st.touched.clear(); st.fresh.clear(); st.agreed = []; st.elsewhere = null;
      }
      st.mode = m.session.mode; st.doc = m.session.doc; st.stage = m.stage || null;
      st.ends = m.session.ends || null;
      renderClock();
      st.sections = new Set(m.sections || []);
      st.research = new Map((m.session.research || []).map((j) => [j.id, j]));
      for (const j of st.research.values()) if (j.state === "done") st.popped.add(`${st.sid}:${j.id}`);
      renderLooking();
      render();
    } else if (m.type === "doc") {
      const old = st.doc;
      st.doc = m.doc;
      if ("stage" in m) st.stage = m.stage || null;
      if (m.sections) st.sections = new Set(m.sections);
      if (m.by !== "tidy") noteChanges(old, m.doc);
      render();
    } else if (m.type === "stage") {
      st.stage = m.heading || null;
      if (!st.stage) st.elsewhere = null;
      render();
    } else if (m.type === "research") {
      const j = m.job;
      st.research.set(j.id, j);
      const key = `${st.sid}:${j.id}`;
      if (onScreen(j) && j.state === "done" && j.answer && !st.popped.has(key)) { st.popped.add(key); showLookup(j); }
      renderLooking();
    } else if (m.type === "research_removed") {
      st.research.delete(m.id);
      if (st.lookup && st.lookup.id === m.id) hideLookup();
      renderLooking();
    } else if (m.type === "status") {
      const on = (m.listening || []).length > 0;
      if (!$("live").classList.contains("lost")) { $("live").hidden = !on; $("live").textContent = "Listening"; }
    }
  }

  // ---------------------------------------------------------------- reading the document
  const norm = (s) => s.toLowerCase().replace(/[*_`]/g, "").replace(/^\d+[.)]\s*/, "").replace(/\s+/g, " ").trim();

  function parse(doc) {
    const lines = doc.replace(/\r\n/g, "\n").split("\n");
    const heads = [];
    lines.forEach((l, i) => {
      const m = l.match(/^(#{1,6})\s+(.*?)\s*#*\s*$/);
      if (m) heads.push({ level: m[1].length, text: m[2].trim(), start: i });
    });
    heads.forEach((h, k) => {
      h.end = lines.length;
      for (let j = k + 1; j < heads.length; j++) if (heads[j].level <= h.level) { h.end = heads[j].start; break; }
      for (let j = k - 1; j >= 0; j--) if (heads[j].level < h.level) { h.parent = heads[j]; break; }
      const p = h.parent && h.parent.level > 1 ? norm(h.parent.text) + "/" : "";
      h.key = p + norm(h.text);
    });
    const h1 = heads.find((h) => h.level === 1);
    return { lines, heads, title: h1 ? h1.text : "" };
  }

  const headLine = (P, h) => P.lines[h.start].trim();
  const heldIn = (P) => (st.stage ? P.heads.find((h) => h.level >= 2 && h.level <= 3 && headLine(P, h) === st.stage) || null : null);

  // Which sections can be the card, per mode.
  // hark decides which sections the screen steps through. The meeting summary is in the
  // list but changes every update, so it never pulls the screen to itself.
  const inList = (P, h) => st.sections.has(headLine(P, h));
  const eligible = (P, h) => inList(P, h) && !(st.mode === "meeting" && norm(h.text) === "summary");

  function sectionAt(P, i) {
    let best = null;
    for (const h of P.heads) if (h.level >= 2 && h.level <= 3 && h.start <= i && i < h.end && (!best || h.level > best.level)) best = h;
    while (best && !eligible(P, best)) best = best.parent && best.parent.level >= 2 ? best.parent : null;
    return best;
  }

  function topSection(P, i) {
    let best = null;
    for (const h of P.heads) if (h.level === 2 && h.start <= i && i < h.end) best = h;
    return best ? norm(best.text) : "";
  }

  // Lines that are new in this version (by text, so moved lines don't count) mark their section.
  function noteChanges(oldDoc, newDoc) {
    const P = parse(newDoc), now = Date.now();
    const seen = new Map();
    for (const l of oldDoc.split("\n")) { const k = l.trim(); if (k) seen.set(k, (seen.get(k) || 0) + 1); }
    const keys = new Set(), agreed = [], away = new Set();
    const held = heldIn(P);
    P.lines.forEach((l, i) => {
      const k = l.trim();
      if (!k) return;
      if (seen.get(k)) { seen.set(k, seen.get(k) - 1); return; }
      st.fresh.set(k, now);
      const h = sectionAt(P, i);
      if (h) keys.add(h.key);
      if (held && h && h !== held && !(held.start <= i && i < held.end)) away.add(h.text);
      const top = topSection(P, i);
      if ((top === "decisions" || top === "actions") && /^\s*[-*+]|^\s*\d+[.)]/.test(l)) agreed.push({ text: l, kind: top, t: now });
    });
    if (keys.size <= BULK) for (const k of keys) st.touched.set(k, now);
    if (away.size && keys.size <= BULK) st.elsewhere = { names: [...away], t: now };
    if (agreed.length <= 4) st.agreed = st.agreed.concat(agreed).slice(-6);
  }

  function itemStatus(P, h) {
    for (let i = h.start + 1; i < h.end; i++) {
      const m = P.lines[i].match(/^\s*[-*]\s+\*\*Status:?\*\*:?\s*(.*)$/i);
      if (m) return norm(m[1]);
    }
    return "";
  }

  // ---------------------------------------------------------------- choosing the card
  const navList = (P) => P.heads.filter((h) => inList(P, h));

  function pick(P, nav) {
    const held = heldIn(P);
    if (held) return held;
    const cands = nav.filter((h) => eligible(P, h));
    const grilling = st.mode === "grill" ? cands.filter((h) => itemStatus(P, h) === "being grilled") : [];
    const pool = grilling.length ? grilling : cands;
    let best = null, bt = 0;
    for (const h of pool) { const t = st.touched.get(h.key) || 0; if (t > bt) { bt = t; best = h; } }
    if (best) return best;
    if (grilling.length) return grilling[0];
    if (st.mode === "grill") {
      const open = cands.filter((h) => !/^(ready|parked)/.test(itemStatus(P, h)));
      return open.find((h) => parseItem(P, h).qs.length) || open[0] || cands[0] || null;
    }
    if (st.mode === "meeting") return cands.filter((h) => h.level === 3).pop() || nav[0] || null;
    return cands.find((h) => P.lines.slice(h.start + 1, h.end).some((l) => l.trim())) || cands[0] || null;
  }

  // ---------------------------------------------------------------- drawing
  function freshAge(texts) {
    const now = Date.now();
    let best = null;
    for (const s of texts) {
      const t = st.fresh.get((s || "").trim());
      if (t && now - t < FRESH_MS && (best === null || t > best)) best = t;
    }
    return best === null ? null : (now - best) / 1000;
  }
  const freshAttr = (texts) => {
    const age = freshAge(texts);
    return age === null ? "" : ` fresh" style="--age:-${age.toFixed(1)}s`;
  };

  function renderLines(lines) {
    const bl = blocks(lines.join("\n"));
    if (!bl.length) return '<p class="nothing">Nothing yet</p>';
    return bl.map((b) => {
      const html = b.html.replace(/<input /g, '<input disabled tabindex="-1" ');
      return `<div class="b${freshAttr(lines.slice(b.start, b.end))}">${html}</div>`;
    }).join("");
  }

  function sectionCard(P, h, nav) {
    const kicker = h.level === 3 && h.parent ? inline(h.parent.text) : (st.mode === "meeting" ? "" : `Section ${nav.indexOf(h) + 1} of ${nav.length}`);
    return (kicker ? `<p class="kicker">${kicker}</p>` : "") +
      `<h1 class="${freshAttr([P.lines[h.start]]).replace(/^ /, "")}">${inline(h.text)}</h1>` +
      `<div class="body">${renderLines(P.lines.slice(h.start + 1, h.end))}</div>`;
  }

  function parseItem(P, h) {
    const out = { fields: [], qs: [], lookups: [], status: "" };
    let inQ = false;
    for (let i = h.start + 1; i < h.end; i++) {
      const l = P.lines[i];
      if (!l.trim()) continue;
      const hm = l.match(/^#{4,6}\s+(.*)/);
      if (hm) { inQ = /open questions/i.test(hm[1]); continue; }
      const lk = l.match(/^\s*[-*+]\s+(?:\*\*)?look up:?(?:\*\*)?:?\s*(.*)$/i);
      if (lk) { out.lookups.push(lk[1]); continue; }
      if (inQ) {
        if (!/^\s*[-*+]\s+/.test(l)) continue;
        const body = l.replace(/^\s*[-*+]\s+/, "");
        const m = body.match(/^\*\*(Q\d+)\.?\s*([^*]*?):?\*\*:?\s*(.*)$/);
        let id = "", title = "", rest = body;
        if (m) { id = m[1]; title = m[2]; rest = m[3]; }
        const disp = rest.match(/\(disputed:\s*([^)]*)\)/i);
        if (disp) rest = rest.replace(disp[0], " ");
        const [q, s] = rest.split(/➡️|=>|→/);
        out.qs.push({ id, title, q: q.trim(), sugg: (s || "").replace(/^\s*suggested:?\s*/i, "").trim(), disputed: disp ? disp[1].trim() : "", line: l });
        continue;
      }
      const f = l.match(/^\s*[-*+]\s+\*\*([^*]+?):?\*\*:?\s*(.*)$/);
      if (f && FIELDS.includes(norm(f[1]))) {
        if (norm(f[1]) === "status") out.status = norm(f[2]);
        else out.fields.push({ name: f[1].trim(), value: f[2].trim(), line: l });
        continue;
      }
    }
    return out;
  }

  function grillCard(P, h, nav) {
    const it = parseItem(P, h);
    const num = (h.text.match(/^(\d+)[.)]/) || [])[1] || String(nav.indexOf(h) + 1);
    const title = h.text.replace(/^\d+[.)]\s*/, "");
    const status = it.status ? ` · <span class="status s-${it.status.split(" ")[0].replace(/[^a-z]/g, "")}">${inline(it.status)}</span>` : "";
    let html = `<p class="kicker">Item ${num} of ${nav.length}${status}</p><h1>${inline(title)}</h1>`;
    const [q, ...more] = it.qs;
    if (q) {
      html += `<div class="q${freshAttr([q.line])}">` +
        `<p class="qlabel">${q.id ? `${q.id}${q.title ? " · " + inline(q.title) : ""}` : "Open question"}` +
        (it.qs.length > 1 ? ` <span>1 of ${it.qs.length} open</span>` : "") + "</p>" +
        `<p class="qtext">${inline(q.q)}</p>` +
        (q.sugg ? `<p class="sugg"><span>Suggested</span> ${inline(q.sugg)}</p>` : "") +
        (q.disputed ? `<p class="disputed"><span>Disputed</span> ${inline(q.disputed)}</p>`
          : q.sugg ? '<p class="hint">Say “yes” to accept, or give another answer.</p>' : "") +
        "</div>";
      if (more.length) html += '<ol class="more">' + more.map((x) => `<li class="${freshAttr([x.line]).replace(/^ /, "")}"><strong>${x.id ? x.id + ". " : ""}${inline(x.title)}</strong> ${inline(x.q)}${x.disputed ? "<em>disputed</em>" : ""}</li>`).join("") + "</ol>";
    } else if (/^ready/.test(it.status)) {
      html += '<p class="verdict">✓ Ready to hand over</p>';
    } else if (/^parked/.test(it.status)) {
      html += '<p class="verdict parked">Parked for now</p>';
    }
    if (it.fields.length) {
      html += '<dl class="fields">' + it.fields.map((f) => {
        const unset = !f.value || f.value === "?";
        return `<div class="${freshAttr([f.line]).replace(/^ /, "")}"><dt>${inline(f.name)}</dt><dd class="${unset ? "unset" : ""}">${unset ? "?" : inline(f.value)}</dd></div>`;
      }).join("") + "</dl>";
    }
    if (it.lookups.length) html += `<p class="lookups"><span>To look up</span> ${it.lookups.map(inline).join(" · ")}</p>`;
    return html;
  }

  function emptyCard() {
    return '<p class="kicker">hark</p><h1>Waiting for the conversation</h1>' +
      '<div class="body"><p class="nothing">Whatever the group is working on will show here, in large type, as it happens.</p></div>';
  }

  function renderRail(P, focus, nav) {
    const rail = $("rail");
    let html = "";
    if (st.mode === "meeting") {
      const ag = P.heads.find((h) => h.level === 2 && norm(h.text) === "agenda");
      const aims = P.heads.find((h) => h.level === 2 && norm(h.text) === "aims");
      if (aims) {
        const list = P.lines.slice(aims.start + 1, aims.end).map((l) => l.match(/^[-*+]\s+\[([ xX])\]\s+(.*)$/)).filter(Boolean);
        if (list.length) html += '<h2>Aims</h2><ol class="aims">' + list.map((m) => `<li class="${/x/i.test(m[1]) ? "done" : ""}">${inline(m[2])}</li>`).join("") + "</ol>";
      }
      if (ag) {
        const items = P.lines.slice(ag.start + 1, ag.end).map((l) => l.match(/^\s*[-*+]\s+\[([ xX])\]\s+(.*)$/)).filter(Boolean);
        let now = false;
        html += "<h2>Agenda</h2><ol>" + items.map((m) => {
          const done = /x/i.test(m[1]);
          const cur = !done && !now; if (cur) now = true;
          return `<li class="${done ? "done" : cur ? "cur" : ""}">${inline(m[2])}</li>`;
        }).join("") + "</ol>";
        if (items.length) html += `<p class="count">${items.filter((m) => /x/i.test(m[1])).length} of ${items.length} done</p>`;
      } else {
        const topics = nav.filter((h) => h.level === 3);
        if (topics.length > 1) html += "<h2>Topics</h2>" + navItems(topics, focus);
      }
    } else if (nav.length > 1) {
      html = `<h2>${st.mode === "grill" ? "Items" : "Outline"}</h2>` + navItems(nav, focus, st.mode !== "grill");
    }
    rail.innerHTML = html;
    rail.hidden = !html;
    document.body.classList.toggle("withrail", !!html);
  }

  function navItems(list, focus, numbered) {
    const P = st.P;
    return "<ol>" + list.map((h, i) => {
      const s = st.mode === "grill" ? itemStatus(P, h) : "";
      const cls = [h === focus ? "cur" : "", /^ready/.test(s) ? "ready" : "", /^parked/.test(s) ? "parked" : ""].filter(Boolean).join(" ");
      return `<li class="${cls}"><button type="button" data-head="${encodeURIComponent(headLine(P, h))}">${numbered ? `<span class="n">${i + 1}</span>` : ""}${inline(h.text)}</button></li>`;
    }).join("") + "</ol>";
  }

  function renderAgreed(P) {
    const now = Date.now(), have = new Set(P.lines.map((l) => l.trim()));
    st.agreed = st.agreed.filter((a) => now - a.t < AGREED_MS && have.has(a.text.trim()));
    const el = $("agreed");
    if (!st.agreed.length) { el.hidden = true; el.innerHTML = ""; return; }
    const html = st.agreed.slice(-2).map((a) => {
      const text = a.text.replace(/^\s*([-*+]|\d+[.)])\s+(\[[ xX]\]\s+)?/, "");
      return `<p><span>${a.kind === "actions" ? "Action" : "Decided"}</span> ${inline(text)}</p>`;
    }).join("");
    if (el.innerHTML !== html) el.innerHTML = html;
    el.hidden = false;
  }

  function render() {
    const P = (st.P = parse(st.doc));
    const t = P.title && !/^(meeting notes|notes|untitled|grilling session)$/i.test(P.title) ? P.title : "";
    $("title").textContent = t;
    document.title = t ? `${t}: room screen` : "hark: room screen";
    const nav = (st.nav = navList(P));
    const focus = pick(P, nav);
    const card = $("card");
    card.innerHTML = !focus ? emptyCard() : st.mode === "grill" && eligible(P, focus) ? grillCard(P, focus, nav) : sectionCard(P, focus, nav);
    const key = focus ? focus.key : null;
    const line = focus ? headLine(P, focus) : null;
    if (line !== st.reported && st.ws && st.ws.readyState === 1) {
      st.reported = line;
      st.ws.send(JSON.stringify({ type: "showing", heading: line }));
    }
    if (key !== st.shown) {
      st.shown = key;
      card.classList.remove("enter"); void card.offsetWidth; card.classList.add("enter");
      card.scrollTop = 0;
    }
    renderRail(P, focus, nav);
    renderAgreed(P);
    const held = !!heldIn(P);
    $("followBtn").textContent = held ? "Follow live" : "Following";
    $("followBtn").classList.toggle("off", held);
    $("held").hidden = !held;
    renderElsewhere();
    $("prevBtn").hidden = $("nextBtn").hidden = nav.length < 2;
    fit();
  }

  // Shrink the type until the card fits; past that, keep the newest lines in view.
  function fit() {
    const c = $("card");
    let s = 1;
    c.style.setProperty("--fit", s);
    while (c.scrollHeight > c.clientHeight + 2 && s > 0.62) { s = +(s - 0.06).toFixed(2); c.style.setProperty("--fit", s); }
    if (c.scrollHeight > c.clientHeight + 2 && !st.stage) {
      const f = c.querySelector(".fresh");
      if (f) c.scrollTop = Math.max(0, f.offsetTop - c.clientHeight / 3);
    }
  }

  // ---------------------------------------------------------------- controls
  function step(d) {
    const nav = st.nav;
    if (!nav.length) return;
    const i = nav.findIndex((h) => h.key === st.shown);
    const j = Math.max(0, Math.min(nav.length - 1, (i < 0 ? 0 : i) + d));
    setStage(headLine(st.P, nav[j]));
  }
  // Every screen shows the same thing, so the choice goes to hark (shown here straight away).
  function setStage(heading) {
    st.stage = heading; st.elsewhere = null;
    if (st.ws && st.ws.readyState === 1) st.ws.send(JSON.stringify({ type: "stage", heading }));
    render();
  }
  function follow() { setStage(null); }

  function renderElsewhere() {
    const el = $("elsewhere"), e = st.elsewhere;
    if (!e || Date.now() - e.t > 12000 || !st.stage) { el.hidden = true; st.elsewhere = null; return; }
    const names = e.names.slice(0, 2).map((n) => inline(n.replace(/^\d+[.)]\s*/, ""))).join(", ");
    const html = `✓ Also changed: ${names}`;
    if (el.innerHTML !== html) el.innerHTML = html;
    el.hidden = false;
  }
  // ---------------------------------------------------------------- quick look-ups
  // A quick answer pops up over the notes for long enough to read it aloud, then goes.
  // Requests for sources stay in the Research panel: a reading list doesn't read across a room.
  // Answers from the group's own documents are short, so they pop up too.
  // So do Claude's answers about the meeting itself ("what's left?") and the reminder near the end.
  const onScreen = (j) => j.kind === "quick" || j.kind === "corpus" || j.kind === "meeting";
  function renderLooking() {
    const busy = [...st.research.values()].filter((j) => onScreen(j) && (j.state === "queued" || j.state === "running"));
    const el = $("looking");
    el.hidden = !busy.length;
    const j = busy[busy.length - 1];
    if (j) el.textContent = `${j.kind === "corpus" ? `Searching ${j.corpus}` : "Looking up"}: ${j.label || j.q}`;
  }
  let lookupTimer;
  function showLookup(j) {
    const it = j.items && j.items[0];
    let host = "";
    try { host = it && it.url ? new URL(it.url).hostname.replace(/^www\./, "") : ""; } catch (_) { /* not a URL */ }
    const corpus = j.kind === "corpus", meeting = j.kind === "meeting";
    const src = meeting ? (j.origin === "reminder" ? "" : "From the notes and discussion so far") : corpus ? (j.items.length ? `From: ${j.items.slice(0, 3).map((i) => esc(i.title)).join("; ")}` : `Nothing found in ${esc(j.corpus)}`)
      : it ? `Source: ${esc(it.title)}${host ? ` <span class="host">${esc(host)}</span>` : ""}`
      : "From Claude's own knowledge, not checked against a source";
    const el = $("lookup");
    const answer = clip(j.answer, 700);
    const more = answer !== j.answer ? '<span class="more"> The full answer is in the Research panel.</span>' : "";
    const kicker = meeting ? (j.origin === "reminder" ? "Time check" : "Claude") : corpus ? `From ${esc(j.corpus)}` : "Quick look-up";
    el.innerHTML = `<p class="kicker">${kicker}</p><h2>${esc(j.label || j.q)}</h2><p class="answer">${esc(answer)}${more}</p>` +
      (src ? `<p class="src">${src}</p>` : "");
    el.hidden = false;
    // A long answer gets smaller type rather than being cut off.
    let s = 1;
    el.style.setProperty("--lfit", s);
    while (el.scrollHeight > el.clientHeight + 2 && s > 0.55) { s = +(s - 0.05).toFixed(2); el.style.setProperty("--lfit", s); }
    el.classList.remove("enter"); void el.offsetWidth; el.classList.add("enter");
    const words = answer.split(/\s+/).length;
    const ms = Math.min(90000, Math.max(40000, 25000 + words * 400));
    st.lookup = { id: j.id };
    clearTimeout(lookupTimer);
    lookupTimer = setTimeout(hideLookup, ms);
  }
  // Shorten to whole sentences (or whole words), as hark does for Claude.
  function clip(text, limit) {
    if (text.length <= limit) return text;
    const cut = text.slice(0, limit);
    const end = Math.max(cut.lastIndexOf(". "), cut.lastIndexOf("? "), cut.lastIndexOf("! "));
    if (end > limit / 2) return cut.slice(0, end + 1) + " …";
    return cut.slice(0, cut.lastIndexOf(" ")).replace(/[,;:]$/, "") + " …";
  }
  function hideLookup() {
    clearTimeout(lookupTimer);
    st.lookup = null;
    $("lookup").hidden = true;
  }
  const esc = (s) => String(s || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

  function fullscreen() {
    if (document.fullscreenElement) document.exitFullscreen();
    else if (document.documentElement.requestFullscreen) document.documentElement.requestFullscreen().catch(() => {});
  }

  $("prevBtn").onclick = () => step(-1);
  $("nextBtn").onclick = () => step(1);
  $("followBtn").onclick = follow;
  $("fsBtn").onclick = fullscreen;
  $("lookup").onclick = hideLookup;
  $("rail").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-head]");
    if (b) setStage(decodeURIComponent(b.dataset.head));
  });
  document.addEventListener("fullscreenchange", () => { $("fsBtn").textContent = document.fullscreenElement ? "Exit full screen" : "Full screen"; });
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    // Presentation clickers send PageUp / PageDown.
    if (e.key === "ArrowLeft" || e.key === "PageUp") { e.preventDefault(); step(-1); }
    else if (e.key === "ArrowRight" || e.key === "PageDown") { e.preventDefault(); step(1); }
    else if (e.key === "l" || e.key === "L" || e.key === "Home") { e.preventDefault(); follow(); }
    else if (e.key === "f" || e.key === "F") { e.preventDefault(); fullscreen(); }
    else if (e.key === "Escape" && st.lookup) { e.preventDefault(); hideLookup(); }
  });

  // Controls fade out when the mouse is still, so the screen stays clean.
  let idle;
  const wake = () => {
    document.body.classList.remove("idle");
    clearTimeout(idle);
    idle = setTimeout(() => document.body.classList.add("idle"), 3000);
  };
  ["mousemove", "pointerdown", "keydown"].forEach((ev) => document.addEventListener(ev, wake, { passive: true }));
  wake();

  window.addEventListener("resize", fit);
  $("elsewhere").onclick = () => {
    const n = st.elsewhere && st.elsewhere.names[0], P = st.P;
    const h = n && P && P.heads.find((x) => x.text === n);
    if (h) setStage(headLine(P, h));
  };
  // "14 min left" in the top bar, once an end time is set.
  function renderClock() {
    const el = $("clock");
    el.hidden = !st.ends;
    if (!st.ends) return;
    const left = Math.round((st.ends - Date.now() / 1000) / 60);
    const at = new Date(st.ends * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
    const span = (n) => (n < 90 ? `${n} min` : `${Math.floor(n / 60)} h${n % 60 ? ` ${n % 60} min` : ""}`);
    el.textContent = left >= 0 ? `${span(left)} left · ends ${at}` : `${span(-left)} over · was due ${at}`;
    el.classList.toggle("soon", left >= 0 && left <= 10);
    el.classList.toggle("over", left < 0);
  }
  setInterval(() => {
    renderClock();
    if (st.agreed.length) renderAgreed(st.P || parse(st.doc));
    if (st.elsewhere) renderElsewhere();
  }, 3000);
  connect();
})();
