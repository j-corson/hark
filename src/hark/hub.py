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
from datetime import datetime
from pathlib import Path

import numpy as np

from . import agenda, issues, skill, slides, timing
from .docops import apply_ops, changed_lines, find_heading, rename_in_doc
from .engine import SR, AudioStream, Engine
from .research import KINDS, Researcher, clean, clean_url, clip, guess_kind
from .screen import WAKE, addressed, screen_request, screen_sections  # noqa: F401 (WAKE is used by tests)
from .scribe import Scribe, ScribeError, build_close_prompt, build_prompt, build_tidy_prompt
from .modes import MODES
from .session import Session, list_sessions, read_versions
from .speakers import UNKNOWN

log = logging.getLogger("hark")
SOURCES = {0: "mic", 1: "call", 2: "replay"}
CONTEXT_LINES = 12
ERROR_BACKOFF = 20
MAX_RESEARCH = 2  # lookups running at once; more wait their turn
WAKE_PAUSE = 1.2  # ...once the sentence is finished
WAKE_WAIT = 4.0  # ...or after this long a pause, punctuation or not
HEADING_TEXT = re.compile(r"^#+\s+(.*)$")
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
        quick = j.get("kind") == "quick" and j.get("answer") and not j.get("added")
        if not items and not quick:
            continue
        where = f" (from the {clean(j['corpus'], 60)} collection)" if j.get("corpus") else ""
        lines = [f"[R{j['id']}] {clean(j.get('label') or j['q'], 120)}{where}"]
        if quick or (j.get("kind") == "corpus" and j.get("answer")):
            lines.append(f"  Answer: {clip(clean(j['answer'], 4000), 1500)}")
        for i, it in items:
            url = clean_url(it.get("url"))
            note = clean(it.get("note"), 250)
            where = url or clean(it.get("path"), 200)
            lines.append(f"  {i}. {clean(it['title'], 250)}" + (f" ({where})" if where else "") + (f": {note}" if note else ""))
        out.append("\n".join(lines))
    return out


class Client:
    def __init__(self, ws, local: bool = False):
        self.ws = ws
        self.local = local  # a browser on this computer, which may open file dialogs here
        self.editing = False
        self.streams: dict[str, AudioStream] = {}


def said(text: str, line: dict) -> str:
    """A transcript line for Claude, marked if hark already switched the screen for it."""
    return text + "  [screen request: done by hark]" if line.get("screen") else text


class Hub:
    def __init__(self, engine: Engine, scribe: Scribe, data_dir: Path, threshold: float = 0.40, researcher: Researcher | None = None,
                 gh: issues.GhRunner | None = None, deck: slides.Deck | None = None):
        self.engine = engine
        self.scribe = scribe
        self.researcher = researcher or Researcher(scribe)
        self._corpora_checked, self._corpora_seen = 0.0, None
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
        self.stage_moves = 0  # room screen switches by hand or by hark itself, not by Claude
        self.tidy_wanted: str | None = None  # "asked", or "stop" when listening stopped in co-writing
        self.tidy_at = 0.0  # earliest time a "stop" tidy may start
        self.held_tidy: tuple | None = None  # tidied document waiting for someone to finish editing
        self.tidied: tuple[str, int] | None = None  # (session id, version) right after the last tidy
        self.prep: tuple | None = None  # (session, agenda, aims, issues) for Claude to lay out with first questions
        self.prep_items: list[tuple[Session, list]] = []  # issues added mid-session: (session, [(section as added, issue)])
        self.gh = gh  # the GitHub CLI, for issues in grilling
        self.gh_ok = False  # ...installed and logged in (see check_issues)
        self.issues_repo: str | None = None  # the last repo issues were picked from (config.json), read once
        self.last_call = 0.0
        self.retry_at = 0.0
        self.claude = {"state": "idle", "detail": "" if scribe.available else getattr(scribe, "problem", "") or "no model available"}
        self._tasks: set[asyncio.Task] = set()
        self.deck = deck or slides.Deck()
        self.popup: tuple[str, int, float] | None = None  # (session id, job id, until): the answer popped up on the room screen
        self.linking = False  # a file dialog is open to link a deck

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
                "research_ok": self.researcher.available, "corpora": self.researcher.collections()}

    async def push_status(self) -> None:
        await self.broadcast(self.status())

    async def push_state(self, ws=None) -> None:
        msg = {"type": "state", "session": self.session.public(), "sessions": list_sessions(self.data_dir), "stage": self.session.stage,
               "sections": screen_sections(self.session.doc, self.session.mode), "issues": self.issues_public(self.session),
               "slides_available": self.deck.available}
        # Only a browser on this computer may link a deck: the file dialog opens here.
        local = lambda w: self.deck.available and bool(getattr(self.clients.get(w), "local", False))  # noqa: E731
        await asyncio.gather(*(self.send(w, {**msg, "can_link": local(w)}) for w in ([ws] if ws else list(self.clients))))
        await (self.send(ws, self.status()) if ws else self.push_status())

    async def push_doc(self, changed: list[int] | None = None, by: str = "you") -> None:
        s = self.session
        s.save()
        # The held section follows renumbering; if it's gone, the screen follows the talk again.
        s.stage = find_heading(s.doc, s.stage)
        await self.broadcast({"type": "doc", "doc": s.doc, "version": s.version, "changed": changed or [],
                              "by": by, "can_undo": bool(s.history), "title": s.title, "stage": s.stage,
                              "sections": screen_sections(s.doc, s.mode), "issues": self.issues_public(s)})

    async def _screen_request(self, s: Session, line: dict) -> None:
        """Switch the room screen straight away for a clear "Claude, show the methods", with no Claude call.
        Claude still sees the line later, with a note that it was done, so it doesn't do it again."""
        if s is not self.session:
            return
        tries = [[line]]
        prev = s.lines[-2] if len(s.lines) > 1 else None
        if (line["kind"] == "speech" and prev and prev["kind"] == "speech" and not prev.get("screen")
                and line["added"] - prev["added"] < 8 and addressed(prev["text"])):
            tries.append([prev, line])  # "Claude," and the request can land in separate lines
        for lines in tries:
            req = screen_request(" ".join(l["text"] for l in lines), s.doc, need_wake=line["kind"] == "speech",
                                 mode=s.mode, current=s.stage or s.showing, slides=self.deck.available)
            if req:
                break
        else:
            return
        kind, heading = req
        if kind == "slide":
            for l in lines:
                l["screen"] = True
            m = HEADING_TEXT.match(heading or "")
            note = f"Put {m.group(1) if m else 'what the room screen shows'} on a slide"
            s.done = (s.done + [f"[{time.strftime('%H:%M:%S')}] {note} (hark did this for you)"])[-DONE_KEPT:]
            self.spawn(self.slide_this(s, heading, "voice"))
            return
        if kind == "follow":
            note = "The room screen follows the conversation again"
        else:
            m = HEADING_TEXT.match(heading or "")
            note = f"Showing {m.group(1) if m else heading} on the room screen"
        for l in lines:
            l["screen"] = True
        if heading != s.stage:
            await self.set_stage(heading, "voice")
            await self.broadcast({"type": "toast", "text": f"✓ {note}"})
        s.done = (s.done + [f"[{time.strftime('%H:%M:%S')}] {note} (hark did this for you)"])[-DONE_KEPT:]

    async def set_stage(self, heading: str | None, by: str) -> bool:
        """Hold a section on the room screen (None: follow the conversation). False if no such section."""
        s = self.session
        found = find_heading(s.doc, heading)
        if heading and not found:
            return False
        s.stage = found
        if by != "claude":
            self.stage_moves += 1
        await self.broadcast({"type": "stage", "heading": found, "by": by})
        return True

    # -- slides ---------------------------------------------------------------
    def _can_slide(self, s: Session) -> bool:
        """PowerPoint can be reached here, and this session has a deck linked."""
        return self.deck.available and bool(s.pptx)

    def _popped(self, s: Session, job: dict) -> None:
        """Note an answer the room screen pops up, for "put that on a slide"."""
        if job.get("kind") in slides.POPUP_KINDS and job["state"] == "done" and job.get("answer"):
            self.popup = (s.id, job["id"], time.time() + slides.popup_seconds(job["answer"]))

    def _popup_job(self, s: Session) -> dict | None:
        """The answer popped up on the room screen now, or until a little while ago."""
        if not self.popup or self.popup[0] != s.id or time.time() > self.popup[2] + slides.POPUP_GRACE:
            return None
        job = s.research_job(self.popup[1])
        return job if job and job["state"] == "done" else None

    async def slide_job(self, s: Session, job: dict) -> str | None:
        """Add a finished card to the linked deck. None if added, otherwise why not; the card says which."""
        note = await self.deck.add(s.pptx, slides.job_slide(job))
        if note is None:
            job["slide"] = time.time()
            job.pop("slides_note", None)
        else:
            job["slides_note"] = note
        s.save()
        await self.push_research(s, job)
        return note

    async def slide_this(self, s: Session, heading: str | None = None, by: str = "you", ws=None, question: str | None = None) -> None:
        """Put a named section on a slide, or else what the room screen shows: an answer that popped up, or the section.
        A grilling item gives a slide per open question, or just `question` (e.g. "Q2")."""
        say = (lambda text: self.send(ws, {"type": "toast", "text": text})) if ws else (lambda text: self.broadcast({"type": "toast", "text": text}))
        if not self.deck.available:
            return
        if not s.pptx:
            await say("No PowerPoint deck is linked. Link one on this computer: ☰ → Link a PowerPoint file…")
            return
        job = None if heading or question else self._popup_job(s)
        if job:
            what = job.get("label") or job["q"]
            note = await self.slide_job(s, job)
        else:
            found = find_heading(s.doc, heading or s.stage or s.showing)
            payloads = slides.section_slides(s.doc, found, s.mode, question) if found else []
            m = HEADING_TEXT.match(found or "")
            what = m.group(1) if m else found
            if not payloads:
                await say(f"{what} has no open question {question} to put on a slide." if found and question
                          else f"There's no section called {heading} to put on a slide." if heading
                          else "There's nothing on the room screen to put on a slide yet.")
                return
            if question:
                what = f"{question} of {what}"
            elif len(payloads) > 1:
                what = f"{what} ({len(payloads)} questions, a slide each)"
            note = None
            for payload in payloads:  # in order; stop at the first that doesn't go in
                note = await self.deck.add(s.pptx, payload)
                if note is not None:
                    break
        await say(f"✓ Added {what} to slides" if note is None else f"{what} wasn't added to slides: {note}")

    async def _link(self, ws) -> None:
        """Ask for a deck with a file dialog on this computer. Cancelling changes nothing. The deck is linked
        to the session open when the dialog closes, which may not be the one open when it opened."""
        self.linking = True
        try:
            path = await self.deck.pick(self.session.pptx)
        except slides.SlidesError as e:
            await self.send(ws, {"type": "toast", "text": f"Couldn't open the file dialog: {e}"})
            return
        finally:
            self.linking = False
        if not path:
            return
        s = self.session
        s.pptx = path
        s.save()
        await self.push_state()
        await self.send(ws, {"type": "toast", "text": f"✓ Linked {s.public()['deck']}. Slides are added to it while it's open in PowerPoint."})

    async def _slides_msg(self, ws, kind: str, msg: dict) -> None:
        s = self.session
        if not self.deck.available:
            return
        if kind == "slides_auto":
            s.slides_auto = bool(msg.get("on")) and bool(s.pptx)
            s.save()
            await self.push_state()
        elif kind == "slides_add":
            job = s.research_job(msg.get("id"))
            if job and job["state"] == "done" and self._can_slide(s):
                self.spawn(self.slide_job(s, job))
        elif kind == "slides_this":
            heading = msg.get("heading")
            self.spawn(self.slide_this(s, heading.strip() if isinstance(heading, str) and heading.strip() else None, "you", ws))
        elif kind == "slides_link" and getattr(self.clients.get(ws), "local", False) and not self.linking:
            self.spawn(self._link(ws))

    # -- connections --------------------------------------------------------
    async def connect(self, ws, local: bool = False) -> None:
        self.clients[ws] = Client(ws, local)
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
        relabels = session.speakers.relabels
        sid = session.speakers.assign(emb, len(samples) / SR, st.last_speaker)
        st.last_speaker = sid
        line = session.add_line(t, st.source, sid, text)
        await self._screen_request(session, line)
        session.save()
        if session.speakers.relabels != relabels:
            await self.push_state()  # speakers were merged or promoted: relabel earlier lines everywhere
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
            if not self.listening:
                await self._offer_updates(ws, self.session)
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
                await self._watch_corpora()
                await self._time_check()
                if self._prep_due():
                    await (self._prepare() if self.prep else self._prepare_items())
                elif self._due():
                    await self._update()
                elif self._tidy_due():
                    await self._tidy()
            except Exception:  # keep the loop alive whatever happens
                log.exception("update failed")

    async def _watch_corpora(self) -> None:
        """Every few seconds, tell the page if collections were added or removed elsewhere (e.g. `hark --add-corpus`)."""
        if time.time() - self._corpora_checked < 5:
            return
        self._corpora_checked = time.time()
        names = [c["name"] for c in self.researcher.collections()]
        if names != self._corpora_seen:
            self._corpora_seen = names
            await self.push_status()

    def active_corpus(self, s: Session) -> str | None:
        """The collection chosen for this session, if it still exists. Claude searches no other."""
        return self.researcher.find(s.corpus) if s.corpus else None

    def _collections_for(self, s: Session) -> list[dict]:
        # Claude is only told about the session's own collection, so it can't pick another one.
        active = self.active_corpus(s)
        return [c for c in self.researcher.collections() if c["name"] == active]

    # -- meeting time and answers ----------------------------------------------
    async def _answered(self, s: Session, data: dict) -> None:
        """Claude's answer to a question about the session ("what's left?"), and an end time someone said."""
        ends = str(data.get("ends") or "").strip()
        at = timing.parse_clock(ends, exact=True) if ends else None
        if at and at > time.time() - 60 and at != s.ends:
            s.set_ends(at)
            s.save()
            if s is self.session:
                await self.push_state()
                await self.broadcast({"type": "toast", "text": f"✓ This session ends at {timing.hhmm(at)}"})
        a = data.get("answer")
        text = clip(clean(a.get("text"), 1200), 600) if isinstance(a, dict) else ""
        if not text:
            return
        label = clean(a.get("question"), 120) or "Your question"
        job = s.add_research(label, "conversation", "done", label, kind="meeting")
        job.update(answer=text, finished=time.time())
        self._popped(s, job)
        s.save()
        await self.push_research(s, job)

    async def _time_check(self) -> None:
        """Near the end, if asked to, remind the room what's still open. Only while someone is listening."""
        s = self.session
        if not (s.ends and s.nudge and not s.nudged and self.listening):
            return
        now = time.time()
        if not s.ends - timing.NUDGE_BEFORE <= now < s.ends:
            return
        s.nudged = True
        text = timing.reminder(s.doc, now, s.ends)
        if text:
            label = f"{timing.minutes(s.ends - now).capitalize()} left"
            job = s.add_research(label, "reminder", "done", label, kind="meeting")
            job.update(answer=text, finished=now)
            self._popped(s, job)
            await self.push_research(s, job)
        s.save()

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
        if self.force or any(l["kind"] == "typed" and not l.get("screen") for l in pending):
            return True
        pause = now - pending[-1]["added"]
        finished = SENTENCE_END.search(pending[-1]["text"]) and pause >= WAKE_PAUSE
        if (finished or pause >= WAKE_WAIT) and any(addressed(l["text"]) and not l.get("screen") for l in pending):
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
        requested = [j["q"] for j in s.research[-15:] if j.get("kind") != "meeting"]
        findings = research_findings(s)
        earlier = [said(s.fmt_line(l), l) for l in s.lines[max(0, s.processed - CONTEXT_LINES):s.processed] if l["kind"] == "speech"]
        new = [said(s.fmt_line(l), l) for l in pending if l["kind"] == "speech"]
        typed = [said(l["text"], l) for l in pending if l["kind"] == "typed"]
        # A screen request hark already carried out. "Next section" is relative, so if Claude
        # also acted on it the screen would move twice; nor should Claude's older view of the
        # talk undo a switch someone made while it was thinking.
        no_show = any(l.get("screen") for l in pending)
        moves = self.stage_moves
        base = s.doc
        numbered = screen_sections(base, s.mode) if s.mode in ("outline", "notes") else None
        # Grilling keeps one conversation open (live.py): once it's under way, Claude remembers
        # the brief, the skill and the earlier talk, so only what's new is sent.
        live = await self.scribe.live_for(s.id, s.mode) if getattr(self.scribe, "live", None) else None
        going = bool(live and live.continues(s.brief))
        lead, method = live.opening() if live and not going else ("", "")
        if s.mode == "grill" and not live:
            method = skill.text()
        prompt = lead + build_prompt("(as before)" if going else s.brief, base, s.speaker_notes(), [] if going else earlier, new, typed,
                                     adds, requested, findings, s.done[-DONE_KEPT:],
                                     on_screen=s.stage, sections=numbered, collections=self._collections_for(s),
                                     collections_unpicked=not self.active_corpus(s) and bool(self.researcher.collections()),
                                     clock=timing.clock_note(time.time(), s.ends),
                                     slide_deck=("linked" if s.pptx else "unlinked") if self.deck.available else "", skill=method)
        detail = f"{len(pending)} new line{'s' * (len(pending) != 1)}" if pending else ""
        if adds:
            detail = (detail + ", " if detail else "") + "adding research"
        self.claude = {"state": "thinking", "detail": detail}
        await self.push_status()
        try:
            data, took = await (self.scribe.edit_live(live, prompt, s.brief) if live else self.scribe.edit(prompt))
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
        await self._answered(s, data)
        if s is not self.session:
            return  # session switched while Claude was thinking
        if data.get("tidy"):
            self.tidy_wanted = "asked"
        data["_no_show"] = no_show or moves != self.stage_moves
        data["_no_slide"] = no_show
        self.held = (s, upto, data, took, base)
        await self._maybe_release_held()

    # -- starting from an agenda ---------------------------------------------
    async def load_agenda(self, text: str, mode: str | None = None, aims: list | None = None, ends: str = "",
                          nudge: bool | None = None, issues: list[dict] | None = None) -> bool:
        """Start the document from an agenda. A session that already has content is kept, and a new one started.
        Aims and an end time can come in the agenda text ("Aims: ...", "Ends: 11:30") or separately (the dialog).
        In grilling, GitHub issues (from fetch_issues) join the agenda as items."""
        text, more_aims, said_ends = agenda.plan((text or "").strip()[:agenda.MAX_CHARS])
        aims = [str(a).strip()[:300] for a in (aims or []) if str(a).strip()][:10] + more_aims
        _title, _context, items = agenda.parse(text)
        if not items and not aims and not issues:
            return False
        at = timing.parse_clock(ends, exact=True) if ends else timing.parse_clock(said_ends) if said_ends else None
        s = self.session
        mode = mode if mode in MODES else s.mode
        if not s.untouched:
            s.save()
            s = self._switch(Session(self.data_dir, mode=mode, threshold=self.threshold))
        else:
            s.set_mode(mode)
        issues = (issues or []) if s.mode == "grill" else []
        s.set_doc(agenda.layout(text, s.mode, aims, issues), by="agenda")
        if at or nudge is not None:
            s.set_ends(at or s.ends, nudge)
        if s.mode == "grill" and self.scribe.available and (items or issues):
            self.prep = (s, text, aims, issues)
        s.save()
        await self.push_state()
        return True

    def _prep_due(self) -> bool:
        return bool(self.prep or self.prep_items) and not self.busy and not self.held and not self.held_tidy and self.scribe.available

    async def _prepare(self) -> None:
        s, text, aims, new_issues = self.prep
        self.prep = None
        if s is not self.session:
            return
        self.busy = True
        self.claude = {"state": "preparing", "detail": ""}
        await self.push_status()
        base = s.doc
        try:
            data, took = await self.scribe.prepare(agenda.build_prep_prompt(s.brief, text, aims, new_issues, s.doc, skill.text()))
        except ScribeError as e:
            self.claude = {"state": "error", "detail": f"couldn't prepare the questions: {e}"}
            log.warning("prepare: %s", e)
            return
        finally:
            self.busy = False
            self.last_call = time.time()
            await self.push_status()
        doc = issues.keep_issue_fields(base, str(data.get("document") or "").strip())
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

    async def _prepare_items(self) -> None:
        """Prep items added mid-session (GitHub issues). Only those items change, and only if nobody has touched them since."""
        s, new = self.prep_items.pop(0)
        if s is not self.session:
            return
        self.busy = True
        self.claude = {"state": "preparing", "detail": "new items"}
        await self.push_status()
        try:
            data, took = await self.scribe.prepare_items(agenda.build_item_prep_prompt(s.brief, s.doc, new, skill.text()))
        except ScribeError as e:
            self.claude = {"state": "error", "detail": f"couldn't prepare the new items: {e}"}
            log.warning("prepare items: %s", e)
            return
        finally:
            self.busy = False
            self.last_call = time.time()
            await self.push_status()
        while self.editing and s is self.session:
            await asyncio.sleep(0.5)
        if s is not self.session:
            return
        added = {issue["ref"]: section for section, issue in new}
        base = doc = s.doc
        kept = 0
        for it in data.get("items") or []:
            ref = str(it.get("ref") or "").strip() if isinstance(it, dict) else ""
            section = str(it.get("section") or "").strip() if isinstance(it, dict) else ""
            head = issues.issue_items(doc).get(ref)
            if ref not in added or not head or not section.startswith("###"):
                continue
            now = issues.item_section(doc, head)
            if issues.changed(added[ref], now):
                kept += 1  # changed by hand or by Claude meanwhile: keep that
                continue
            lines = section.split("\n")
            num = re.match(r"^###\s+(\d+[.)])\s*", head)
            title = re.sub(r"^#+\s+(\d+[.)]\s*)?", "", lines[0]).strip()
            lines[0] = f"### {num.group(1)} {title}" if num and title else head  # keep the number it was given
            section = "\n".join(lines)
            if ref not in issues.issue_items(section):
                section = issues.keep_issue_fields(now, section)
            doc = doc.replace(now, section, 1)
        if s.set_doc(doc, by="claude"):
            await self.push_doc(changed_lines(base, s.doc), by="claude")
            summary = str(data.get("summary") or "").strip().rstrip(".")[:120]
            await self.broadcast({"type": "toast", "text": f"✓ Ready to grill{': ' + summary if summary else ''}."})
        elif kept:
            await self.broadcast({"type": "toast", "text": "The new items changed while Claude was drafting questions, so it kept your version."})
        self.claude = {"state": "idle", "detail": f"prepared in {took:.0f}s"}
        await self._requested_research(s, data.get("research") or [], state="proposed", origin="agenda")
        await self.push_status()

    def _switch(self, s: Session) -> Session:
        self.session = s
        self.held = None
        self.held_tidy, self.tidy_wanted = None, None
        self.prep = None
        self.prep_items = []
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
        elif s.set_doc(issues.keep_issue_fields(base, doc), by="tidy"):
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
        new = issues.keep_issue_fields(old, new)  # an issue item stays linked to its issue
        s.processed = max(s.processed, upto)
        renamed = False
        for label, name in speaker_pairs(data.get("speaker_names")):
            label, name = str(label).strip(), str(name).strip()
            for sid, sp in list(s.speakers.items.items()):
                if name and sid != UNKNOWN and sp["name"].lower() == label.lower() and (sp["auto"] or respelling(sp["name"], name)):
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
        show = data.get("show")
        if isinstance(show, str) and data.get("_no_show"):
            log.info("ignored show %r: hark already switched the screen", show)
        elif isinstance(show, str) and not await self.set_stage(show.strip() or None, "claude"):
            log.info("asked to show %r, which isn't a section", show)
        slide = data.get("slide")
        if isinstance(slide, str) and data.get("_no_slide"):
            log.info("ignored slide %r: hark already did the request", slide)
        elif isinstance(slide, str) and self._can_slide(s):
            q = data.get("slide_question")
            self.spawn(self.slide_this(s, slide.strip() or None, "claude", question=q.strip() if isinstance(q, str) and q.strip() else None))
        # Findings Claude says it added by voice request. Only mark ones whose link or
        # title really went into the document, so the panel's Add buttons stay honest.
        written = "\n".join(str(o.get("text") or "") for o in ops if isinstance(o, dict))
        for used in (data.get("research_used") or [])[:10]:
            job = s.research_job(used.get("id")) if isinstance(used, dict) else None
            if not job or job["state"] != "done":
                continue
            nums = used.get("items") or range(1, len(job["items"]) + 1)
            touched = False
            if job.get("kind") == "quick" and not job.get("added"):
                job["added"] = touched = True  # the explanation itself has no link to check for
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
        known = {(j["q"].strip().lower(), j.get("corpus")) for j in s.research}
        for r in reqs[:3]:
            if not isinstance(r, dict):
                continue
            q = str(r.get("question") or "").strip()
            kind = r.get("kind") if r.get("kind") in KINDS else guess_kind(q)
            # Whatever collection was named, only the session's own one is ever searched.
            corpus = self.active_corpus(s) if kind == "corpus" else None
            if kind == "corpus" and not corpus:
                named = self.researcher.find(r.get("corpus"))
                await self.broadcast({"type": "toast", "text": f"To let Claude search {named or 'your collections'}, pick "
                                                               f"{'it' if named else 'one'} in the Research panel, in the menu next to "
                                                               "Look something up (it says Web now), then ask again."})
                continue
            if not q or (q.lower(), corpus) in known:
                continue
            known.add((q.lower(), corpus))
            job = s.add_research(q, origin, state or ("queued" if s.research_auto else "proposed"), str(r.get("label") or ""), kind, corpus)
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
            result, took = await self.researcher.run(job["q"], s.title, job.get("kind", "sources"), job.get("corpus"))
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
        if job["state"] == "done":
            self._popped(s, job)
            # The card shows first; the slide follows if the deck is open. Only results finished from now on.
            if s.slides_auto and self._can_slide(s) and job.get("kind") in slides.AUTO_KINDS:
                self.spawn(self.slide_job(s, job))

    def _queue_research(self, s: Session, job: dict) -> None:
        job.update(state="queued", error="")
        self.research_queue.append((s, job))
        self._pump_research()

    async def _research_msg(self, kind: str, msg: dict) -> None:
        s = self.session
        if kind == "research_ask":
            q = (msg.get("q") or "").strip()
            if q:
                corpus = self.active_corpus(s)
                job = s.add_research(q, "typed", "queued", kind="corpus" if corpus else guess_kind(q), corpus=corpus)
                await self.push_research(s, job)
                self._queue_research(s, job)
                s.save()
            return
        if kind == "research_corpus":
            s.corpus = self.researcher.find(msg.get("name"))
            s.save()
            await self.push_state()
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
        elif kind == "research_sources" and job["state"] == "done" and job.get("kind") == "quick" and not job.get("deeper"):
            deeper = s.add_research(job["q"], job["origin"], "queued", job.get("label") or "", "sources")
            job["deeper"] = deeper["id"]
            await self.push_research(s, job)
            await self.push_research(s, deeper)
            self._queue_research(s, deeper)
        elif kind == "research_add" and job["state"] == "done":
            idx = msg.get("items")
            quick = job.get("kind") == "quick"
            if quick and job.get("added"):
                return
            picks = [it for i, it in enumerate(job["items"]) if (idx is None or i in idx) and not it.get("added")]
            if not picks and not (quick and job.get("answer")):
                return
            lines = [f"Request: {job['q']}"]
            if (idx is None or quick) and job.get("answer"):
                lines.append(f"Answer: {job['answer']}")
            if quick:
                job["added"] = True
                lines.append("(A quick explanation: add it as a short point, with the source link if there is one.)")
            for it in picks:
                it["added"] = True
                title = f"[{it['title']}]({it['url']})" if it.get("url") else it["title"] + (f" ({it['path']})" if it.get("path") else "")
                lines.append(f"- {title}" + (f": {it['note']}" if it.get("note") else ""))
            self.promote.append((s, "\n".join(lines)))
            self.force = True
            await self.push_research(s, job)
        s.save()

    # -- collections ----------------------------------------------------------
    async def _corpus_msg(self, ws, kind: str, msg: dict) -> None:
        """Add a GitHub repo, update a collection now, or forget one. Folders on this computer can only be
        added from the command line, so a page (or a leaked link) can't point Claude at any folder it likes."""
        from . import config

        say = lambda text: self.send(ws, {"type": "toast", "text": text})
        name = str(msg.get("name") or "")
        try:
            if kind == "corpus_add":
                spec = str(msg.get("spec") or "").strip()
                if not spec:
                    return
                await say(f"Fetching {spec} from GitHub…")
                c = await asyncio.to_thread(config.add_corpus, spec, str(msg.get("about") or "")[:300], False)
                corpus = self.researcher.corpora.get(c["name"])
                n = await asyncio.to_thread(corpus.sync) if corpus else {"files": 0}
                await say(f"Added {c['name']}: {n['files']} files Claude can search.")
            elif kind == "corpus_update":
                corpus = self.researcher.corpora.get(name)
                if corpus:
                    n = await asyncio.to_thread(corpus.sync, True)
                    await say(f"{name} is up to date: {n['files']} files" + (f". {corpus.problem}" if corpus.problem else "."))
            elif kind == "corpus_remove":
                if await asyncio.to_thread(config.remove_corpus, name):
                    await say(f"Forgot {name}.")
        except (OSError, ValueError) as e:
            await say(str(e)[:300])
        await self.push_status()

    # -- GitHub issues ------------------------------------------------------
    async def check_issues(self, announce: bool = False) -> bool:
        """Whether GitHub issues can be used: `gh` is installed and logged in. Checked once, at start."""
        self.gh_ok = bool(self.gh) and await self.gh.available()
        if announce and self.gh_ok:
            await self.push_state()
        return self.gh_ok

    def issues_public(self, s: Session) -> dict:
        """What the page needs: whether issues work, the last repo, and each issue item's post state."""
        from . import config

        items = {}
        for ref, head in issues.issue_items(s.doc).items():
            posted = s.issues.get(ref) or {}
            posts = posted.get("posts") or []
            items[ref] = {"heading": head, "changed": issues.changed(posted.get("snapshot"), issues.item_section(s.doc, head)),
                          "url": posts[-1]["url"] if posts else "", "posts": len(posts), "closed": bool(posted.get("closed"))}
        if self.issues_repo is None:
            self.issues_repo = config.issue_settings()["repo"]
        return {"available": self.gh_ok, "repo": self.issues_repo, "items": items}

    async def fetch_issues(self, refs: list[str], default_repo: str = "") -> tuple[list[dict], list[str]]:
        """Look issues up with `gh`: ([{"ref", "title", "body", "url", "state"}], problems to show). Each issue once."""
        from . import config

        found, errors, seen = [], [], set()
        for text in refs:
            parsed = issues.parse_ref(str(text), default_repo or None)
            if not parsed:
                errors.append(f"{str(text)[:80]!r} isn't a GitHub issue. Write it as #12, owner/repo#12 or its link.")
                continue
            ref = issues.ref_str(*parsed)
            if ref in seen:
                continue
            seen.add(ref)
            try:
                found.append({"ref": ref, **await self.gh.view(*parsed)})
            except issues.GhError as e:
                errors.append(f"Couldn't get {ref}: {e}")
        if default_repo and issues.REPO.match(default_repo) and found:
            self.issues_repo = default_repo
            await asyncio.to_thread(config.save_issues_repo, default_repo)
        return found, errors

    async def add_issues(self, ws, refs: list[str], default_repo: str = "") -> None:
        """Add issues to a grilling session under way: new items at the end of Items, prepped on their own."""
        s = self.session
        say = lambda text: self.send(ws, {"type": "toast", "text": text})
        if s.mode != "grill":
            await say("Issues can be added in Grilling mode only.")
            return
        have = issues.issue_items(s.doc)
        fresh, dupes = [], []
        for text in refs:
            parsed = issues.parse_ref(str(text), default_repo or None)
            ref = issues.ref_str(*parsed) if parsed else str(text)
            (dupes if ref in have else fresh).append(ref)
        if dupes:
            await say(f"{', '.join(dupes)} {'is' if len(dupes) == 1 else 'are'} already in this session.")
        found, errors = await self.fetch_issues(fresh, default_repo)
        for e in errors:
            await say(e)
        if not found or s is not self.session:
            return
        n = agenda.next_item_number(s.doc)
        new = [(agenda.issue_item(n + i, issue), issue) for i, issue in enumerate(found)]
        if s.set_doc(agenda.append_items(s.doc, [section for section, _ in new]), by="issues"):
            await self.push_doc()
            await say(f"Added {len(new)} issue{'s' * (len(new) != 1)}" + (": Claude is drafting first questions." if self.scribe.available else "."))
        if self.scribe.available:
            self.prep_items.append((s, new))

    def _session_by_id(self, sid) -> Session | None:
        """The session to post from: the current one, or one just left (it was saved when it was left)."""
        if not sid or sid == self.session.id:
            return self.session
        if not self._saved_session(sid):
            return None
        return Session.load(self.data_dir, sid, self.threshold)

    def _comment(self, s: Session, ref: str) -> dict | None:
        from . import config

        head = issues.issue_items(s.doc).get(ref)
        if not head:
            return None
        posted = s.issues.get(ref) or {}
        section = issues.item_section(s.doc, head)
        d = datetime.fromtimestamp(s.created)
        body = issues.comment_body(s.doc, head, s.title, f"{d.day} {d:%b %Y}", posted.get("snapshot"), config.issue_settings()["disclaimer"])
        return {"ref": ref, "heading": head, "body": body, "note": issues.readiness(section) or "",
                "changed": issues.changed(posted.get("snapshot"), section), "snapshot": section,
                "blockers": issues.close_blockers(section), "closed": bool(posted.get("closed"))}

    async def _post(self, s: Session, ref: str, body: str, snapshot=None) -> dict:
        """Post one comment. {"ref", "url"} or {"ref", "error"}; nothing is recorded unless gh says it worked.
        `snapshot` is the item as it was when its preview was made, so later edits still count as new."""
        head = issues.issue_items(s.doc).get(ref)
        parsed = issues.parse_ref(ref)
        if not head or not parsed:
            return {"ref": ref, "error": "That item no longer has an Issue field, so it isn't linked to an issue."}
        if not self.gh_ok:
            return {"ref": ref, "error": issues.GH_NEEDED + "."}
        if not body.strip():
            return {"ref": ref, "error": "The comment is empty."}
        try:
            url = await self.gh.comment(*parsed, body)
        except issues.GhError as e:
            return {"ref": ref, "error": str(e)}
        s.record_post(ref, url, snapshot if isinstance(snapshot, str) and snapshot.strip() else issues.item_section(s.doc, head))
        s.save()
        return {"ref": ref, "url": url}

    async def _close(self, ws, s: Session, ref: str, body: str) -> dict:
        """Post the item's last comment and close its issue, once Claude has checked every question it was asked
        was answered and the frontier is empty, as at the end of a grilling round. A next round Claude finds goes
        under the item's Open questions instead. {"ref", "url", "closed"}, {"ref", "gaps", "added"} (not ready:
        nothing posted; `added` is how many new questions) or {"ref", "error"}."""
        head = issues.issue_items(s.doc).get(ref)
        parsed = issues.parse_ref(ref)
        if not head or not parsed:
            return {"ref": ref, "error": "That item no longer has an Issue field, so it isn't linked to an issue."}
        if not self.gh_ok:
            return {"ref": ref, "error": issues.GH_NEEDED + "."}
        if not body.strip():
            return {"ref": ref, "error": "The comment is empty."}
        section = issues.item_section(s.doc, head)
        if blockers := issues.close_blockers(section):
            return {"ref": ref, "gaps": blockers}
        if not self.scribe.available:
            return {"ref": ref, "error": "Claude has to check the item before it is closed, and Claude isn't available."}
        await self.send(ws, {"type": "issue_checking", "ref": ref})
        try:
            issue = await self.gh.view(*parsed)
        except issues.GhError as e:
            return {"ref": ref, "error": str(e)}
        s.save()  # so the versions log has every edit, for the questions asked along the way
        docs = [e["doc"] for e in read_versions(s.dir / "versions.jsonl") if isinstance(e.get("doc"), str)] + [s.doc]
        asked = issues.asked_questions(docs, ref)
        prompt = build_close_prompt(f"{issue['title']}\n\n{issue['body']}", section, asked, issues.parked_for(s.doc, head, ref),
                                    issues.next_label(asked), skill.text())
        try:
            data, _ = await self.scribe.check_close(prompt)
        except ScribeError as e:
            return {"ref": ref, "error": f"Claude couldn't check the item: {e}"}
        gaps = [str(g).strip() for g in data.get("gaps") or [] if str(g).strip()]
        more = [q for q in data.get("next_round") or [] if isinstance(q, str) and re.match(r"^\s*[-*]\s+\*\*Q\d+", q)]
        head = issues.issue_items(s.doc).get(ref) or head  # the document may have moved on while Claude checked
        old = s.doc
        if more:  # the next round: on the item for the room, and nothing posted
            now = issues.item_section(old, head)
            if s.set_doc(old.replace(now, issues.add_questions(now, more), 1), by="claude") and s is self.session:
                await self.push_doc(changed_lines(old, s.doc), by="claude")
            return {"ref": ref, "gaps": gaps, "added": len(more)}
        if not data.get("ready") or gaps:
            return {"ref": ref, "gaps": gaps or ["Claude found the item isn't finished yet."]}
        ready = issues.item_section(old, head)
        if s.set_doc(old.replace(ready, issues.with_status(ready, "ready"), 1), by="claude") and s is self.session:
            await self.push_doc(changed_lines(old, s.doc), by="claude")
        r = await self._post(s, ref, issues.closing_body(body, str(data.get("resolution") or "")))
        if "error" in r:
            return r
        try:
            await self.gh.close(*parsed)
        except issues.GhError as e:
            return {**r, "error": f"The comment was posted, but the issue wasn't closed: {e}"}
        s.issues[ref]["closed"] = time.time()
        s.save()
        return {**r, "closed": True}

    async def _offer_updates(self, ws, s: Session) -> None:
        """On Stop, or on leaving a session: offer to post the issue items that changed since their last post."""
        if not self.gh_ok:
            return
        items = [c for ref in issues.offer(s.doc, s.issues) if (c := self._comment(s, ref))]
        if items:
            await self.send(ws, {"type": "issues_offer", "session": s.id, "title": s.title, "items": items})

    async def _issues_msg(self, ws, kind: str, msg: dict) -> None:
        if not self.gh_ok:
            await self.send(ws, {"type": "toast", "text": issues.GH_NEEDED + "."})
            return
        if kind == "issues_list":
            repo = str(msg.get("repo") or "").strip()
            if not issues.REPO.match(repo):
                await self.send(ws, {"type": "issues_list", "repo": repo, "items": [], "error": "Write the repo as owner/repo."})
                return
            try:
                rows = await self.gh.list_open(repo)
                await self.send(ws, {"type": "issues_list", "repo": repo, "items": rows})
            except issues.GhError as e:
                await self.send(ws, {"type": "issues_list", "repo": repo, "items": [], "error": str(e)})
        elif kind == "issues_add":
            refs = msg.get("refs") if isinstance(msg.get("refs"), list) else []
            await self.add_issues(ws, [str(r) for r in refs[:50]], str(msg.get("repo") or "").strip())
        elif kind == "issue_preview":
            c = self._comment(self.session, str(msg.get("ref") or ""))
            await self.send(ws, {"type": "issue_preview", **c} if c else
                            {"type": "issue_error", "ref": msg.get("ref"), "error": "That item isn't linked to an issue any more."})
        elif kind == "issue_post":
            s = self._session_by_id(msg.get("session"))
            r = await self._post(s, str(msg.get("ref") or ""), str(msg.get("body") or ""), msg.get("snapshot")) if s else {"ref": msg.get("ref"), "error": "That session wasn't found."}
            await self.send(ws, {"type": "issue_error" if "error" in r else "issue_posted", **r})
            if s is self.session and "url" in r:
                await self.broadcast({"type": "issues", "issues": self.issues_public(s)})
        elif kind == "issue_close":
            s = self._session_by_id(msg.get("session"))
            r = await self._close(ws, s, str(msg.get("ref") or ""), str(msg.get("body") or "")) if s else {"ref": msg.get("ref"), "error": "That session wasn't found."}
            await self.send(ws, {"type": "issue_error" if "error" in r and "url" not in r else
                                 "issue_unready" if "gaps" in r else "issue_posted", **r})
            if s is self.session and "url" in r:
                await self.broadcast({"type": "issues", "issues": self.issues_public(s)})
        elif kind == "issues_post_many":
            s = self._session_by_id(msg.get("session"))
            posts = [p for p in msg.get("posts") or [] if isinstance(p, dict)] if isinstance(msg.get("posts"), list) else []
            results = [await self._post(s, str(p.get("ref") or ""), str(p.get("body") or ""), p.get("snapshot")) for p in posts] if s else []
            if posts:
                await self.send(ws, {"type": "issues_posted", "results": results})
            if s is self.session and any("url" in r for r in results):
                await self.broadcast({"type": "issues", "issues": self.issues_public(s)})

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
                await self._screen_request(s, line)
                s.save()
                await self.broadcast({"type": "line", "line": s.public_line(line), "speakers": s.speakers.public()})
        elif kind == "stage":
            heading = msg.get("heading")
            if not await self.set_stage(heading if isinstance(heading, str) else None, "you"):
                await self.send(ws, {"type": "stage", "heading": s.stage, "by": "you"})
        elif kind == "showing":
            heading = msg.get("heading")
            s.showing = heading[:300] if isinstance(heading, str) else None
        elif kind == "update_now":
            self.force = True
        elif kind == "tidy":
            self.tidy_wanted = "asked"
            if self.busy:
                await self.send(ws, {"type": "toast", "text": "Claude will tidy up as soon as this update is done."})
        elif isinstance(kind, str) and kind.startswith("research_"):
            await self._research_msg(kind, msg)
        elif isinstance(kind, str) and kind.startswith("corpus_"):
            await self._corpus_msg(ws, kind, msg)
        elif isinstance(kind, str) and kind.startswith("issue"):
            await self._issues_msg(ws, kind, msg)
        elif isinstance(kind, str) and kind.startswith("slides_"):
            await self._slides_msg(ws, kind, msg)
        elif kind == "rename":
            was = s.speakers.name(msg.get("id", ""))
            sid = s.speakers.rename(msg.get("id", ""), msg.get("name", ""))
            # The person's old label or spelling goes from the document too.
            s.set_doc(rename_in_doc(s.doc, was, s.speakers.name(sid)))
            s.save()
            await self.push_state()
        elif kind == "reassign":
            name = msg.get("name", "").strip()
            target = s.speakers.named(name) if name else UNKNOWN
            for l in s.lines:
                if l["id"] == msg.get("line"):
                    # A line given by hand is that speaker's for good, so claim them either way;
                    # only a line out of Unknown teaches them a voice, the one Unknown held for it.
                    held = s.speakers.resolve(l["speaker"]) == UNKNOWN
                    if held and target == UNKNOWN:
                        break  # already Unknown and left blank: nothing was said, so nothing changes
                    s.speakers.claim(target, l["speaker"] if held else None)
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
            await self._offer_updates(ws, s)
        elif kind == "agenda":
            aims = msg.get("aims") if isinstance(msg.get("aims"), list) else None
            nudge = msg.get("nudge") if isinstance(msg.get("nudge"), bool) else None
            found = []
            refs = [str(r) for r in msg.get("issues") or [] if isinstance(r, (str, int))] if isinstance(msg.get("issues"), list) else []
            if refs and msg.get("mode") == "grill" and self.gh_ok:
                found, errors = await self.fetch_issues(refs, str(msg.get("repo") or ""))
                for e in errors:
                    await self.send(ws, {"type": "toast", "text": e})
            if not await self.load_agenda(str(msg.get("text") or ""), msg.get("mode"), aims, str(msg.get("ends") or ""), nudge, found):
                await self.send(ws, {"type": "toast", "text": "No agenda items or aims found. Put one item per line or bullet."})
            elif self.session is not s:
                await self._offer_updates(ws, s)
        elif kind == "timing":
            ends = str(msg.get("ends") or "").strip()
            at = timing.parse_clock(ends, exact=True) if ends else None
            if ends and not at:
                await self.send(ws, {"type": "toast", "text": "That end time wasn't a time of day, such as 11:30."})
                return
            s.set_ends(at, bool(msg.get("nudge")))
            s.save()
            await self.push_state()
        elif kind == "open_session":
            sid = msg.get("id", "")
            if self._saved_session(sid):
                s.save()
                self._switch(Session.load(self.data_dir, sid, self.threshold))
                await self.push_state()
                await self._offer_updates(ws, s)

    def _saved_session(self, sid) -> bool:
        """`sid` names a saved session in the data folder (and nothing outside it)."""
        return (isinstance(sid, str) and bool(sid) and "/" not in sid and "\\" not in sid and ".." not in sid
                and (self.data_dir / sid / "session.json").exists())

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
