"""The live loop: audio in, transcript lines out, Claude edits applied to the document.

One active session is shared by every connected browser (e.g. the laptop
capturing a call and a phone you glance at).
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import shutil
import re
import subprocess
import time
import wave
from pathlib import Path

import numpy as np

from . import agenda
from .docops import apply_ops, changed_lines, rename_in_doc
from .engine import SR, AudioStream, Engine
from .research import Researcher, clean, clean_url
from .scribe import Scribe, ScribeError, build_prompt, build_tidy_prompt
from .modes import MODES
from .session import Session, list_sessions

log = logging.getLogger("hark")
SOURCES = {0: "mic", 1: "call", 2: "replay"}
CONTEXT_LINES = 12
ERROR_BACKOFF = 20
MAX_RESEARCH = 2  # lookups running at once; more wait their turn
WAKE = re.compile(r"\b(claude|clawed|claud)\b", re.I)  # speech addressed to Claude skips the wait
WAKE_PAUSE = 1.2  # ...once the sentence is finished
WAKE_WAIT = 4.0  # ...or after this long a pause, punctuation or not
SENTENCE_END = re.compile(r"[.?!][\"')\]]*\s*$")
DONE_KEPT = 6  # how many of Claude's recent "what I did" notes it is reminded of
TIDY_MIN_LINES = 12  # don't bother tidying a document shorter than this on Stop


def respelling(old: str, new: str) -> bool:
    """True if `new` looks like the same name spelled differently (Claire → Clare), not a different person."""
    a, b = old.strip().lower(), new.strip().lower()
    return bool(a and b) and a != b and difflib.SequenceMatcher(None, a, b).ratio() >= 0.7


def speaker_pairs(names) -> list[tuple]:
    """(old label, new name) pairs from a list of {"from", "to"}, or from an older-style mapping."""
    if isinstance(names, dict):
        return list(names.items())
    return [(n.get("from"), n.get("to")) for n in (names or []) if isinstance(n, dict)]


def research_findings(s: Session, jobs: int = 8) -> list[str]:
    """Finished lookups with findings not yet in the document, numbered for the scribe to refer to."""
    out = []
    for j in [j for j in s.research if j["state"] == "done"][-jobs:]:
        items = [(i, it) for i, it in enumerate(j["items"], 1) if not it.get("added")]
        if not items:
            continue
        lines = [f"[R{j['id']}] {clean(j.get('label') or j['q'], 120)}"]
        for i, it in items:
            url = clean_url(it.get("url"))
            note = clean(it.get("note"), 250)
            lines.append(f"  {i}. {clean(it['title'], 250)}" + (f" ({url})" if url else "") + (f": {note}" if note else ""))
        out.append("\n".join(lines))
    return out


class Client:
    def __init__(self, ws):
        self.ws = ws
        self.editing = False
        self.streams: dict[str, AudioStream] = {}


class Hub:
    def __init__(self, engine: Engine, scribe: Scribe, data_dir: Path, threshold: float = 0.40, researcher: Researcher | None = None):
        self.engine = engine
        self.scribe = scribe
        self.researcher = researcher or Researcher(scribe)
        self.research_queue: list[tuple[Session, dict]] = []
        self.research_tasks: dict[tuple[str, int], asyncio.Task] = {}
        self.promote: list[tuple[Session, str]] = []  # research findings waiting to go into the document
        self.data_dir = data_dir
        self.threshold = threshold
        self.session = Session(data_dir, threshold=threshold)
        self.clients: dict[object, Client] = {}
        self.busy = False
        self.force = False
        self.held: tuple | None = None  # Claude result that arrived while someone was editing
        self.tidy_wanted: str | None = None  # "asked", or "stop" when listening stopped in co-writing
        self.tidy_at = 0.0  # earliest time a "stop" tidy may start
        self.held_tidy: tuple | None = None  # tidied document waiting for someone to finish editing
        self.tidied: tuple[str, int] | None = None  # (session id, version) right after the last tidy
        self.prep: tuple[Session, str] | None = None  # (session, agenda) for Claude to lay out with first questions
        self.last_call = 0.0
        self.retry_at = 0.0
        self.claude = {"state": "idle", "detail": "" if scribe.available else getattr(scribe, "problem", "") or "no model available"}
        self._tasks: set[asyncio.Task] = set()

    # -- plumbing -----------------------------------------------------------
    def spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def send(self, ws, msg: dict) -> None:
        try:
            await ws.send_str(json.dumps(msg))
        except Exception:
            pass

    async def broadcast(self, msg: dict) -> None:
        await asyncio.gather(*(self.send(ws, msg) for ws in list(self.clients)))

    @property
    def editing(self) -> bool:
        return any(c.editing for c in self.clients.values())

    def status(self) -> dict:
        s = self.session
        listening = sorted({src for c in self.clients.values() for src in c.streams})
        return {"type": "status", "listening": listening, "backlog": round(self.engine.backlog, 1),
                "claude": self.claude, "editing": self.editing, "pending": len(s.lines) - s.processed,
                "model": self.scribe.model, "asr": self.engine.asr_name, "researching": len(self.research_tasks),
                "research_ok": self.researcher.available}

    async def push_status(self) -> None:
        await self.broadcast(self.status())

    async def push_state(self, ws=None) -> None:
        msg = {"type": "state", "session": self.session.public(), "sessions": list_sessions(self.data_dir)}
        if ws is None:
            await self.broadcast(msg)
        else:
            await self.send(ws, msg)
        await (self.send(ws, self.status()) if ws else self.push_status())

    async def push_doc(self, changed: list[int] | None = None, by: str = "you") -> None:
        s = self.session
        s.save()
        await self.broadcast({"type": "doc", "doc": s.doc, "version": s.version, "changed": changed or [],
                              "by": by, "can_undo": bool(s.history), "title": s.title})

    # -- connections --------------------------------------------------------
    async def connect(self, ws) -> None:
        self.clients[ws] = Client(ws)
        await self.push_state(ws)

    async def disconnect(self, ws) -> None:
        c = self.clients.pop(ws, None)
        if c:
            for st in c.streams.values():
                await self._segments(st, st.flush())
        await self.push_status()
        await self._maybe_release_held()

    # -- audio --------------------------------------------------------------
    async def audio(self, ws, data: bytes) -> None:
        c = self.clients.get(ws)
        if not c or len(data) < 3:
            return
        src = SOURCES.get(data[0], "mic")
        st = c.streams.get(src)
        if st is None:
            return  # source not started (or already stopped)
        pcm = np.frombuffer(data[1:len(data) - (len(data) - 1) % 2], dtype="<i2").astype(np.float32) / 32768.0
        await self._segments(st, st.feed(pcm))

    async def _segments(self, st: AudioStream, segs) -> None:
        for t, samples in segs:
            self.spawn(self._transcribe(st, t, samples))
        if segs:
            await self.push_status()

    async def _transcribe(self, st: AudioStream, t: float, samples: np.ndarray) -> None:
        session = self.session
        try:
            text, emb = await self.engine.process(samples)
        except Exception as e:  # pragma: no cover - model failure
            log.exception("transcription failed: %s", e)
            return
        if not text or session is not self.session:
            return
        merges = len(session.speakers.alias)
        sid = session.speakers.assign(emb, len(samples) / SR, st.last_speaker)
        st.last_speaker = sid
        line = session.add_line(t, st.source, sid, text)
        session.save()
        if len(session.speakers.alias) != merges:
            await self.push_state()  # two speakers were merged: relabel earlier lines everywhere
        else:
            await self.broadcast({"type": "line", "line": session.public_line(line), "speakers": session.speakers.public()})
        await self.push_status()

    async def start_source(self, ws, source: str) -> None:
        c = self.clients.get(ws)
        if c and source not in c.streams:
            c.streams[source] = AudioStream(self.engine, source)
        await self.push_status()

    async def stop_source(self, ws, source: str) -> None:
        c = self.clients.get(ws)
        st = c.streams.pop(source, None) if c else None
        if st:
            await self._segments(st, st.flush())
            if self.session.mode == "outline" and not self.listening and not self.tidy_wanted:
                self.tidy_wanted, self.tidy_at = "stop", time.time() + 3
        await self.push_status()

    async def replay(self, path: str, speed: float = 1.0) -> None:
        """Feed an audio file through the pipeline as if it were live."""
        if shutil.which("ffmpeg"):
            raw = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", path, "-ar", str(SR), "-ac", "1", "-f", "s16le", "-"],
                                 capture_output=True, check=True).stdout
        else:
            with wave.open(path) as w:
                if w.getframerate() != SR or w.getnchannels() != 1:
                    raise SystemExit("Without ffmpeg, --replay needs a 16 kHz mono WAV")
                raw = w.readframes(w.getnframes())
        pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        await asyncio.sleep(2)
        st = AudioStream(self.engine, "replay")
        step = SR // 10
        log.info("replaying %s (%.0fs at %sx)", path, len(pcm) / SR, speed)
        for i in range(0, len(pcm), step):
            await self._segments(st, st.feed(pcm[i:i + step]))
            await asyncio.sleep(0.1 / speed)
        await self._segments(st, st.feed(np.zeros(SR, dtype=np.float32)))
        await self._segments(st, st.flush())
        log.info("replay finished")

    # -- the Claude loop ----------------------------------------------------
    async def updater(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            try:
                if self._prep_due():
                    await self._prepare()
                elif self._due():
                    await self._update()
                elif self._tidy_due():
                    await self._tidy()
            except Exception:  # keep the loop alive whatever happens
                log.exception("update failed")

    def _due(self) -> bool:
        s = self.session
        if self.busy or self.editing or self.held or self.held_tidy or not self.scribe.available:
            return False
        pending = s.lines[s.processed:]
        if not pending and not self.force:
            return False
        now = time.time()
        if now < self.retry_at and not self.force:
            return False
        if self.force or any(l["kind"] == "typed" for l in pending):
            return True
        pause = now - pending[-1]["added"]
        finished = SENTENCE_END.search(pending[-1]["text"]) and pause >= WAKE_PAUSE
        if (finished or pause >= WAKE_WAIT) and any(WAKE.search(l["text"]) for l in pending):
            return True  # someone spoke to Claude: act on it now, not at the next interval
        since = now - self.last_call
        chars = sum(len(l["text"]) for l in pending)
        return since >= s.interval and (pause >= min(2.5, s.interval / 2) or chars >= 400)

    async def _update(self) -> None:
        s = self.session
        self.force = False
        self.busy = True
        upto = len(s.lines)
        pending = s.lines[s.processed:upto]
        adds = [t for (ss, t) in self.promote if ss is s]
        self.promote = [(ss, t) for (ss, t) in self.promote if ss is not s]
        requested = [j["q"] for j in s.research[-15:]]
        findings = research_findings(s)
        earlier = [s.fmt_line(l) for l in s.lines[max(0, s.processed - CONTEXT_LINES):s.processed] if l["kind"] == "speech"]
        new = [s.fmt_line(l) for l in pending if l["kind"] == "speech"]
        typed = [l["text"] for l in pending if l["kind"] == "typed"]
        base = s.doc
        prompt = build_prompt(s.brief, base, s.speaker_notes(), earlier, new, typed, adds, requested, findings, s.done[-DONE_KEPT:])
        detail = f"{len(pending)} new line{'s' * (len(pending) != 1)}" if pending else ""
        if adds:
            detail = (detail + ", " if detail else "") + "adding research"
        self.claude = {"state": "thinking", "detail": detail}
        await self.push_status()
        try:
            data, took = await self.scribe.edit(prompt)
        except ScribeError as e:
            self.promote = [(s, t) for t in adds] + self.promote  # try again with the next update
            self.retry_at = time.time() + ERROR_BACKOFF
            self.claude = {"state": "error", "detail": str(e)}
            log.warning("claude: %s", e)
            return
        finally:
            self.busy = False
            self.last_call = time.time()
        await self._requested_research(s, data.get("research") or [])
        if s is not self.session:
            return  # session switched while Claude was thinking
        if data.get("tidy"):
            self.tidy_wanted = "asked"
        self.held = (s, upto, data, took, base)
        await self._maybe_release_held()

    # -- starting from an agenda ---------------------------------------------
    async def load_agenda(self, text: str, mode: str | None = None) -> bool:
        """Start the document from an agenda. A session that already has content is kept, and a new one started."""
        text = (text or "").strip()[:agenda.MAX_CHARS]
        _title, _context, items = agenda.parse(text)
        if not items:
            return False
        s = self.session
        mode = mode if mode in MODES else s.mode
        if not s.untouched:
            s.save()
            s = self._switch(Session(self.data_dir, mode=mode, threshold=self.threshold))
        else:
            s.set_mode(mode)
        s.set_doc(agenda.layout(text, s.mode), by="agenda")
        if s.mode == "grill" and self.scribe.available:
            self.prep = (s, text)
        s.save()
        await self.push_state()
        return True

    def _prep_due(self) -> bool:
        return bool(self.prep) and not self.busy and not self.held and not self.held_tidy and self.scribe.available

    async def _prepare(self) -> None:
        s, text = self.prep
        self.prep = None
        if s is not self.session:
            return
        self.busy = True
        self.claude = {"state": "preparing", "detail": ""}
        await self.push_status()
        base = s.doc
        try:
            data, took = await self.scribe.prepare(agenda.build_prep_prompt(s.brief, text))
        except ScribeError as e:
            self.claude = {"state": "error", "detail": f"couldn't prepare the questions: {e}"}
            log.warning("prepare: %s", e)
            return
        finally:
            self.busy = False
            self.last_call = time.time()
            await self.push_status()
        doc = str(data.get("document") or "").strip()
        if not doc.startswith("#") or len(doc) < 0.8 * len(base.strip()):
            self.claude = {"state": "error", "detail": "the prepared questions came back incomplete, so the agenda was left as it was"}
            await self.push_status()
            return
        while self.editing and s is self.session:
            await asyncio.sleep(0.5)  # don't swap the document out from under someone's edit
        if s is not self.session:
            return
        if s.doc != base:
            await self.broadcast({"type": "toast", "text": "The agenda changed while Claude was drafting questions, so it kept your version."})
        elif s.set_doc(doc, by="claude"):
            await self.push_doc(changed_lines(base, s.doc), by="claude")
            summary = str(data.get("summary") or "").strip().rstrip(".")[:120]
            await self.broadcast({"type": "toast", "text": f"✓ Ready to grill{': ' + summary if summary else ''}."})
        self.claude = {"state": "idle", "detail": f"prepared in {took:.0f}s"}
        await self._requested_research(s, data.get("research") or [], state="proposed", origin="agenda")
        await self.push_status()

    def _switch(self, s: Session) -> Session:
        self.session = s
        self.held = None
        self.held_tidy, self.tidy_wanted = None, None
        self.prep = None
        return s

    # -- tidying up ---------------------------------------------------------
    @property
    def listening(self) -> bool:
        return any(c.streams for c in self.clients.values())

    def _tidy_due(self) -> bool:
        if not self.tidy_wanted or self.busy or self.editing or self.held or self.held_tidy or not self.scribe.available:
            return False
        if self.tidy_wanted == "stop":
            s = self.session
            if self.listening:
                self.tidy_wanted = None  # listening again: no tidy after all
                return False
            # Let the last things said reach the document first.
            return time.time() >= self.tidy_at and s.processed >= len(s.lines) and self.engine.backlog < 0.5
        return True

    async def _tidy(self) -> None:
        s = self.session
        why, self.tidy_wanted = self.tidy_wanted, None
        if self.tidied == (s.id, s.version):
            if why == "asked":
                await self.broadcast({"type": "toast", "text": "The document is already tidy."})
            return
        if why == "stop" and len([l for l in s.doc.split("\n") if l.strip()]) < TIDY_MIN_LINES:
            return
        self.busy = True
        self.claude = {"state": "tidying", "detail": ""}
        await self.push_status()
        base = s.doc
        try:
            data, took = await self.scribe.rewrite(build_tidy_prompt(s.brief, base, s.speaker_notes()))
        except ScribeError as e:
            self.retry_at = time.time() + ERROR_BACKOFF
            self.claude = {"state": "error", "detail": f"tidy failed: {e}"}
            log.warning("tidy: %s", e)
            await self.push_status()
            return
        finally:
            self.busy = False
            self.last_call = time.time()
        doc = str(data.get("document") or "").strip()
        if len(doc) < 0.25 * len(base.strip()) or not doc.startswith("#"):
            self.claude = {"state": "error", "detail": "tidy came back incomplete, so the document was left as it was"}
            await self.push_status()
            return
        self.held_tidy = (s, base, doc, str(data.get("summary") or "").strip()[:120], took)
        await self._maybe_release_held()

    async def _apply_tidy(self) -> None:
        s, base, doc, summary, took = self.held_tidy
        self.held_tidy = None
        if s is not self.session:
            return
        self.claude = {"state": "idle", "detail": f"tidied in {took:.0f}s"}
        if s.doc != base:
            await self.broadcast({"type": "toast", "text": "The document changed while Claude was tidying, so it left it alone. Press Tidy to try again."})
        elif s.set_doc(doc, by="tidy"):
            self.tidied = (s.id, s.version)
            await self.push_doc(by="tidy")
            await self.broadcast({"type": "toast", "text": f"✓ Tidied{': ' + summary.rstrip('.') if summary else ''}. Undo puts it back."})
        else:
            self.tidied = (s.id, s.version)
            await self.broadcast({"type": "toast", "text": "The document is already tidy."})
        await self.push_status()

    async def _maybe_release_held(self) -> None:
        if self.held_tidy and not self.editing:
            await self._apply_tidy()
        if not (self.held or self.held_tidy):
            return
        if self.editing:
            self.claude = {"state": "paused", "detail": "waiting for you to finish editing"}
            await self.push_status()
            return
        if not self.held:
            return
        s, upto, data, took, base = self.held
        self.held = None
        if s is not self.session:
            return
        ops = [o for o in (data.get("ops") or []) if isinstance(o, dict)]
        old = s.doc
        new, failed = apply_ops(old, ops, base)
        s.processed = max(s.processed, upto)
        renamed = False
        for label, name in speaker_pairs(data.get("speaker_names")):
            label, name = str(label).strip(), str(name).strip()
            for sid, sp in list(s.speakers.items.items()):
                if name and sp["name"].lower() == label.lower() and (sp["auto"] or respelling(sp["name"], name)):
                    was = sp["name"]
                    s.speakers.rename(sid, name)
                    new = rename_in_doc(new, was, s.speakers.name(sid))
                    renamed = True
        n = len(ops) - len(failed)
        if failed:
            log.info("skipped %d edit(s) whose anchors were not found: %s", len(failed), failed)
        ack = str(data.get("ack") or "").strip()[:140]
        if ack:
            s.done = (s.done + [f"[{time.strftime('%H:%M:%S')}] {ack}"])[-DONE_KEPT:]
            detail = f"{ack} ({took:.0f}s)"
            await self.broadcast({"type": "toast", "text": f"✓ {ack}"})
        elif not ops:
            detail = "nothing new to add"
        else:
            detail = f"{n} edit{'s' * (n != 1)} in {took:.0f}s" + (f", {len(failed)} skipped" if failed else "")
        self.claude = {"state": "idle", "detail": detail}
        if s.set_doc(new, by="claude"):
            await self.push_doc(changed_lines(old, s.doc), by="claude")
        else:
            s.save()
        # Findings Claude says it added by voice request. Only mark ones whose link or
        # title really went into the document, so the panel's Add buttons stay honest.
        written = "\n".join(str(o.get("text") or "") for o in ops if isinstance(o, dict))
        for used in (data.get("research_used") or [])[:10]:
            job = s.research_job(used.get("id")) if isinstance(used, dict) else None
            if not job or job["state"] != "done":
                continue
            nums = used.get("items") or range(1, len(job["items"]) + 1)
            touched = False
            for n in list(nums)[:10]:
                if isinstance(n, int) and 1 <= n <= len(job["items"]):
                    it = job["items"][n - 1]
                    if (it.get("url") and it["url"] in written) or it["title"][:40] in written:
                        it["added"] = touched = True
            if touched:
                s.save()
                await self.push_research(s, job)
        if renamed:
            await self.push_state()
        else:
            await self.push_status()

    # -- background research ----------------------------------------------
    async def push_research(self, s: Session, job: dict | None = None, removed: int | None = None) -> None:
        if s is not self.session:
            return
        if removed is not None:
            await self.broadcast({"type": "research_removed", "id": removed})
        elif job is not None:
            await self.broadcast({"type": "research", "job": job})
        await self.push_status()

    async def _requested_research(self, s: Session, reqs: list, state: str | None = None, origin: str = "conversation") -> None:
        """Lookups the scribe heard someone ask for (or, with `state="proposed"`, ones it suggests)."""
        known = {j["q"].strip().lower() for j in s.research}
        for r in reqs[:3]:
            q = str((r or {}).get("question") or "").strip()
            if not q or q.lower() in known:
                continue
            known.add(q.lower())
            job = s.add_research(q, origin, state or ("queued" if s.research_auto else "proposed"), str(r.get("label") or ""))
            if job["state"] == "queued":
                self.research_queue.append((s, job))
            await self.push_research(s, job)
        if reqs:
            s.save()
            self._pump_research()

    def _pump_research(self) -> None:
        while self.research_queue and len(self.research_tasks) < MAX_RESEARCH:
            s, job = self.research_queue.pop(0)
            if job["state"] != "queued":
                continue
            key = (s.id, job["id"])
            task = asyncio.create_task(self._run_research(s, job))
            self.research_tasks[key] = task
            task.add_done_callback(lambda _t, k=key: (self.research_tasks.pop(k, None), self._pump_research()))

    async def _run_research(self, s: Session, job: dict) -> None:
        job.update(state="running", started=time.time(), error="")
        await self.push_research(s, job)
        try:
            result, took = await self.researcher.run(job["q"], s.title)
            job.update(result, state="done", took=round(took))
        except asyncio.CancelledError:
            job.update(state="cancelled")
        except ScribeError as e:
            job.update(state="error", error=str(e))
        except Exception as e:  # keep the hub alive whatever happens
            log.exception("research failed")
            job.update(state="error", error=str(e)[:200])
        job["finished"] = time.time()
        s.save()
        self.research_tasks.pop((s.id, job["id"]), None)
        await self.push_research(s, job)

    def _queue_research(self, s: Session, job: dict) -> None:
        job.update(state="queued", error="")
        self.research_queue.append((s, job))
        self._pump_research()

    async def _research_msg(self, kind: str, msg: dict) -> None:
        s = self.session
        if kind == "research_ask":
            q = (msg.get("q") or "").strip()
            if q:
                job = s.add_research(q, "typed", "queued")
                await self.push_research(s, job)
                self._queue_research(s, job)
                s.save()
            return
        if kind == "research_auto":
            s.research_auto = bool(msg.get("on"))
            s.save()
            await self.push_state()
            return
        job = s.research_job(msg.get("id"))
        if not job:
            return
        if kind == "research_run" and job["state"] in ("proposed", "error", "cancelled"):
            self._queue_research(s, job)
            await self.push_research(s, job)
        elif kind == "research_cancel":
            task = self.research_tasks.get((s.id, job["id"]))
            if task:
                task.cancel()
            elif job["state"] == "queued":
                job.update(state="cancelled")
                await self.push_research(s, job)
        elif kind == "research_dismiss":
            task = self.research_tasks.get((s.id, job["id"]))
            if task:
                task.cancel()
            job["state"] = "dismissed"
            s.research.remove(job)
            await self.push_research(s, removed=job["id"])
        elif kind == "research_add" and job["state"] == "done":
            idx = msg.get("items")
            picks = [it for i, it in enumerate(job["items"]) if (idx is None or i in idx) and not it.get("added")]
            if not picks:
                return
            lines = [f"Request: {job['q']}"]
            if idx is None and job.get("answer"):
                lines.append(f"Answer: {job['answer']}")
            for it in picks:
                it["added"] = True
                title = f"[{it['title']}]({it['url']})" if it.get("url") else it["title"]
                lines.append(f"- {title}" + (f": {it['note']}" if it.get("note") else ""))
            self.promote.append((s, "\n".join(lines)))
            self.force = True
            await self.push_research(s, job)
        s.save()

    # -- messages from the page --------------------------------------------
    async def handle(self, ws, msg: dict) -> None:
        s = self.session
        c = self.clients.get(ws)
        kind = msg.get("type")
        if kind == "start":
            await self.start_source(ws, msg.get("source", "mic"))
        elif kind == "stop":
            await self.stop_source(ws, msg.get("source", "mic"))
        elif kind == "editing":
            if c:
                c.editing = bool(msg.get("on"))
            await self.push_status()
            await self._maybe_release_held()
        elif kind == "set_doc":
            if s.set_doc(msg.get("doc", "")):
                await self.push_doc()
            if c and msg.get("done", True):
                c.editing = False
            await self.push_status()
            await self._maybe_release_held()
        elif kind == "edit_lines":
            await self._edit_lines(ws, msg)
            if c:
                c.editing = False
            await self.push_status()
            await self._maybe_release_held()
        elif kind == "undo":
            if s.undo():
                await self.push_doc()
        elif kind == "say":
            text = (msg.get("text") or "").strip()
            if text:
                line = s.add_line(time.time(), "typed", s.speakers.named("You (typed)"), text, kind="typed")
                s.save()
                await self.broadcast({"type": "line", "line": s.public_line(line), "speakers": s.speakers.public()})
        elif kind == "update_now":
            self.force = True
        elif kind == "tidy":
            self.tidy_wanted = "asked"
            if self.busy:
                await self.send(ws, {"type": "toast", "text": "Claude will tidy up as soon as this update is done."})
        elif isinstance(kind, str) and kind.startswith("research_"):
            await self._research_msg(kind, msg)
        elif kind == "rename":
            was = s.speakers.name(msg.get("id", ""))
            sid = s.speakers.rename(msg.get("id", ""), msg.get("name", ""))
            # The person's old label or spelling goes from the document too.
            s.set_doc(rename_in_doc(s.doc, was, s.speakers.name(sid)))
            s.save()
            await self.push_state()
        elif kind == "reassign":
            target = s.speakers.named(msg.get("name", "").strip() or "Unknown")
            for l in s.lines:
                if l["id"] == msg.get("line"):
                    l["speaker"] = target
            s.save()
            await self.push_state()
        elif kind == "set_mode":
            s.set_mode(msg.get("mode", ""))
            s.save()
            await self.push_state()
        elif kind == "set_brief":
            s.brief = (msg.get("brief") or s.brief)[:6000]
            try:
                s.interval = max(3, min(300, int(msg.get("interval", s.interval))))
            except (TypeError, ValueError):
                pass
            s.save()
            await self.push_state()
        elif kind == "new_session":
            s.save()
            self._switch(Session(self.data_dir, mode=msg.get("mode") or s.mode, threshold=self.threshold))
            await self.push_state()
        elif kind == "agenda":
            if not await self.load_agenda(str(msg.get("text") or ""), msg.get("mode")):
                await self.send(ws, {"type": "toast", "text": "No agenda items found. Put one item per line or bullet."})
        elif kind == "open_session":
            sid = msg.get("id", "")
            if sid and (self.data_dir / sid / "session.json").exists() and "/" not in sid and "\\" not in sid and ".." not in sid:
                s.save()
                self._switch(Session.load(self.data_dir, sid, self.threshold))
                await self.push_state()

    async def _edit_lines(self, ws, msg: dict) -> None:
        """Replace a block of lines the user edited in place."""
        s = self.session
        lines = s.doc.split("\n")
        a, b = int(msg.get("start", 0)), int(msg.get("end", 0))
        old = msg.get("old", "")
        if not (0 <= a <= b <= len(lines) and "\n".join(lines[a:b]) == old):
            # The document moved underneath the editor; find the block by content.
            from .docops import _find_lines
            span = _find_lines(lines, old) if old.strip() else None
            if span is None:
                await self.send(ws, {"type": "toast", "text": "That part of the document changed while you were editing. Try again."})
                await self.push_doc()
                return
            a, b = span
        text = msg.get("text", "")
        new_lines = text.split("\n") if text.strip() else []
        if s.set_doc("\n".join(lines[:a] + new_lines + lines[b:])):
            await self.push_doc()
