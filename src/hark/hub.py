"""The live loop: audio in, transcript lines out, Claude edits applied to the document.

One active session is shared by every connected browser (e.g. the laptop
capturing a call and a phone you glance at).
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import time
import wave
from pathlib import Path

import numpy as np

from .docops import apply_ops, changed_lines
from .engine import SR, AudioStream, Engine
from .scribe import Scribe, ScribeError, build_prompt
from .session import Session, list_sessions

log = logging.getLogger("hark")
SOURCES = {0: "mic", 1: "call", 2: "replay"}
CONTEXT_LINES = 12
ERROR_BACKOFF = 20


class Client:
    def __init__(self, ws):
        self.ws = ws
        self.editing = False
        self.streams: dict[str, AudioStream] = {}


class Hub:
    def __init__(self, engine: Engine, scribe: Scribe, data_dir: Path, threshold: float = 0.40):
        self.engine = engine
        self.scribe = scribe
        self.data_dir = data_dir
        self.threshold = threshold
        self.session = Session(data_dir, threshold=threshold)
        self.clients: dict[object, Client] = {}
        self.busy = False
        self.force = False
        self.held: tuple | None = None  # Claude result that arrived while someone was editing
        self.last_call = 0.0
        self.retry_at = 0.0
        self.claude = {"state": "idle", "detail": "" if scribe.available else "Claude CLI not found"}
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
                "model": self.scribe.model, "asr": self.engine.asr_name}

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
                if self._due():
                    await self._update()
            except Exception:  # keep the loop alive whatever happens
                log.exception("update failed")

    def _due(self) -> bool:
        s = self.session
        if self.busy or self.editing or self.held or not self.scribe.available:
            return False
        pending = s.lines[s.processed:]
        if not pending and not self.force:
            return False
        now = time.time()
        if now < self.retry_at and not self.force:
            return False
        if self.force or any(l["kind"] == "typed" for l in pending):
            return True
        since = now - self.last_call
        pause = now - pending[-1]["added"]
        chars = sum(len(l["text"]) for l in pending)
        return since >= s.interval and (pause >= min(2.5, s.interval / 2) or chars >= 400)

    async def _update(self) -> None:
        s = self.session
        self.force = False
        self.busy = True
        upto = len(s.lines)
        pending = s.lines[s.processed:upto]
        earlier = [s.fmt_line(l) for l in s.lines[max(0, s.processed - CONTEXT_LINES):s.processed] if l["kind"] == "speech"]
        new = [s.fmt_line(l) for l in pending if l["kind"] == "speech"]
        typed = [l["text"] for l in pending if l["kind"] == "typed"]
        prompt = build_prompt(s.brief, s.doc, s.speaker_notes(), earlier, new, typed)
        self.claude = {"state": "thinking", "detail": f"{len(pending)} new line{'s' * (len(pending) != 1)}"}
        await self.push_status()
        try:
            data, took = await self.scribe.edit(prompt)
        except ScribeError as e:
            self.retry_at = time.time() + ERROR_BACKOFF
            self.claude = {"state": "error", "detail": str(e)}
            log.warning("claude: %s", e)
            return
        finally:
            self.busy = False
            self.last_call = time.time()
        if s is not self.session:
            return  # session switched while Claude was thinking
        self.held = (s, upto, data, took)
        await self._maybe_release_held()

    async def _maybe_release_held(self) -> None:
        if not self.held:
            return
        if self.editing:
            self.claude = {"state": "paused", "detail": "waiting for you to finish editing"}
            await self.push_status()
            return
        s, upto, data, took = self.held
        self.held = None
        if s is not self.session:
            return
        ops = data.get("ops") or []
        old = s.doc
        new, failed = apply_ops(old, ops)
        s.processed = max(s.processed, upto)
        renamed = False
        for label, name in (data.get("speaker_names") or {}).items():
            for sid, sp in list(s.speakers.items.items()):
                if sp["auto"] and sp["name"].lower() == str(label).lower() and name.strip():
                    s.speakers.rename(sid, name)
                    renamed = True
        n = len(ops) - len(failed)
        if failed:
            log.info("skipped %d edit(s) whose anchors were not found: %s", len(failed), failed)
        self.claude = {"state": "idle", "detail": f"{n} edit{'s' * (n != 1)} in {took:.0f}s" + (f", {len(failed)} skipped" if failed else "")}
        if s.set_doc(new):
            await self.push_doc(changed_lines(old, s.doc), by="claude")
        else:
            s.save()
        if renamed:
            await self.push_state()
        else:
            await self.push_status()

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
        elif kind == "rename":
            s.speakers.rename(msg.get("id", ""), msg.get("name", ""))
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
            self.session = Session(self.data_dir, mode=msg.get("mode") or s.mode, threshold=self.threshold)
            self.held = None
            await self.push_state()
        elif kind == "open_session":
            sid = msg.get("id", "")
            if sid and (self.data_dir / sid / "session.json").exists() and "/" not in sid and "\\" not in sid and ".." not in sid:
                s.save()
                self.session = Session.load(self.data_dir, sid, self.threshold)
                self.held = None
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
