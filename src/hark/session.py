"""A session: one live document, its transcript, speakers and undo history.

Saved as plain files in a folder per session:
  doc.md          the document
  transcript.md   who said what, when
  session.json    everything needed to reopen it
  versions.jsonl  every version of the document, one per line, so a lost or
                  ruined document can be recovered and Undo survives reopening
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from .docops import tidy
from .modes import DEFAULT_MODE, MODES
from .speakers import Speakers

HISTORY = 100


def default_data_dir() -> Path:
    return Path(os.environ.get("HARK_DATA", Path.home() / "Documents" / "Hark"))


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


class Session:
    def __init__(self, root: Path, mode: str = DEFAULT_MODE, threshold: float = 0.40):
        now = datetime.now()
        self.root = root
        self.id = now.strftime("%Y-%m-%d_%H%M%S")
        self.created = time.time()
        self.mode = mode
        self.brief = MODES[mode]["brief"]
        self.interval = MODES[mode]["interval"]
        self.doc = MODES[mode]["template"]
        self.version = 0
        self.history: list[str] = []
        self._log: list[dict] = []  # document versions not yet appended to versions.jsonl
        self._log_started = False
        self.lines: list[dict] = []
        self.processed = 0  # lines[:processed] are reflected in the document
        self.speakers = Speakers(threshold)
        self._next_line = 1
        self.research: list[dict] = []  # background lookups, newest last
        self.research_auto = True  # start lookups people ask for out loud without asking first
        self._next_research = 1
        self.done: list[str] = []  # Claude's recent "what I did" notes, so it doesn't redo an instruction

    # -- paths / naming -----------------------------------------------------
    @property
    def dir(self) -> Path:
        return self.root / self.id

    @property
    def title(self) -> str:
        m = re.search(r"^#\s+(.+)$", self.doc, re.M)
        return m.group(1).strip() if m else "Untitled"

    @property
    def untouched(self) -> bool:
        return not self.lines and not self.research and self.doc.strip() == MODES[self.mode]["template"].strip()

    # -- mutations ----------------------------------------------------------
    def _record(self, by: str) -> None:
        self._log.append({"v": self.version, "t": round(time.time(), 1), "by": by, "doc": self.doc})

    def set_doc(self, markdown: str, by: str = "you") -> bool:
        markdown = tidy(markdown)
        if markdown == self.doc:
            return False
        if not self._log_started:
            self._record("start")  # the version this log starts from, so Undo can reach it after reopening
            self._log_started = True
        self.history.append(self.doc)
        del self.history[:-HISTORY]
        self.doc = markdown
        self.version += 1
        self._record(by)
        return True

    def undo(self) -> bool:
        if not self.history:
            return False
        self.doc = self.history.pop()
        self.version += 1
        self._record("undo")
        return True

    def add_line(self, t: float, source: str, speaker: str, text: str, kind: str = "speech") -> dict:
        line = {"id": self._next_line, "t": t, "added": time.time(), "source": source, "speaker": speaker, "text": text, "kind": kind}
        self._next_line += 1
        self.lines.append(line)
        return line

    def add_research(self, question: str, origin: str, state: str, label: str = "") -> dict:
        job = {"id": self._next_research, "q": question.strip()[:500], "label": label.strip()[:120] or question.strip()[:120],
               "origin": origin, "state": state,
               "created": time.time(), "answer": "", "items": [], "error": "", "took": 0}
        self._next_research += 1
        self.research.append(job)
        return job

    def research_job(self, jid) -> dict | None:
        return next((j for j in self.research if j["id"] == jid), None)

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            return
        fresh = self.untouched
        self.mode = mode
        self.brief = MODES[mode]["brief"]
        self.interval = MODES[mode]["interval"]
        if fresh:
            self.doc = MODES[mode]["template"]
            self.version += 1

    # -- prompt helpers -----------------------------------------------------
    def fmt_line(self, l: dict) -> str:
        ts = datetime.fromtimestamp(l["t"]).strftime("%H:%M:%S")
        return f"[{ts}] {self.speakers.name(l['speaker'])}: {l['text']}"

    def speaker_notes(self) -> list[str]:
        used = {self.speakers.resolve(l["speaker"]) for l in self.lines if l["kind"] == "speech"}
        return [f"{sp['name']}" + (" (auto label)" if sp["auto"] else "") for sid, sp in self.speakers.items.items() if sid in used]

    # -- views --------------------------------------------------------------
    def public_line(self, l: dict) -> dict:
        return {"id": l["id"], "t": l["t"], "source": l["source"], "speaker": self.speakers.resolve(l["speaker"]), "text": l["text"], "kind": l["kind"]}

    def public(self) -> dict:
        return {
            "id": self.id, "title": self.title, "mode": self.mode, "brief": self.brief, "interval": self.interval,
            "doc": self.doc, "version": self.version, "can_undo": bool(self.history),
            "lines": [self.public_line(l) for l in self.lines[-400:]],
            "processed_id": self.lines[self.processed - 1]["id"] if self.processed else 0,
            "speakers": self.speakers.public(), "dir": str(self.dir),
            "research": self.research, "research_auto": self.research_auto,
        }

    # -- persistence --------------------------------------------------------
    def save(self) -> None:
        if self.untouched:
            return  # don't litter the folder with empty sessions
        self.dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.dir / "doc.md", self.doc)
        tr = [f"# Transcript: {self.title}", ""]
        for l in self.lines:
            prefix = "(typed) " if l["kind"] == "typed" else ""
            tr.append(f"{prefix}{self.fmt_line(l)}")
        _atomic_write(self.dir / "transcript.md", "\n".join(tr) + "\n")
        done = [j for j in self.research if j["state"] == "done"]
        if done:
            _atomic_write(self.dir / "research.md", research_markdown(self.title, done))
        state = {
            "id": self.id, "created": self.created, "mode": self.mode, "brief": self.brief, "interval": self.interval,
            "doc": self.doc, "history": self.history[-20:], "lines": self.lines, "processed": self.processed,
            "next_line": self._next_line, "speakers": self.speakers.to_json(),
            "research": self.research, "research_auto": self.research_auto, "next_research": self._next_research,
        }
        _atomic_write(self.dir / "session.json", json.dumps(state))
        if self._log:
            with open(self.dir / "versions.jsonl", "a", encoding="utf-8") as f:
                f.write("".join(json.dumps(e) + "\n" for e in self._log))
            self._log = []

    @classmethod
    def load(cls, root: Path, sid: str, threshold: float = 0.40) -> "Session":
        d = json.loads((root / sid / "session.json").read_text(encoding="utf-8"))
        s = cls(root, d.get("mode", DEFAULT_MODE), threshold)
        s.id = d["id"]
        s.created = d.get("created", s.created)
        s.brief = d.get("brief", s.brief)
        s.interval = d.get("interval", s.interval)
        s.doc = d["doc"]
        s.history = d.get("history", [])
        replayed = replay_versions(root / sid / "versions.jsonl")
        if replayed is not None:
            s._log_started = True
            doc, hist = replayed
            # Only trust the log if it ends where the saved document does.
            if doc == s.doc and len(hist) >= len(s.history):
                s.history = hist
        s.lines = d.get("lines", [])
        s.processed = d.get("processed", len(s.lines))
        s._next_line = d.get("next_line", len(s.lines) + 1)
        s.speakers.load_json(d.get("speakers", {}))
        s.research = d.get("research", [])
        for j in s.research:
            if j["state"] in ("queued", "running"):  # hark was restarted mid-lookup
                j["state"], j["error"] = "error", "Interrupted when hark restarted"
        s.research_auto = d.get("research_auto", True)
        s._next_research = d.get("next_research", len(s.research) + 1)
        return s


def research_markdown(title: str, jobs: list[dict]) -> str:
    out = [f"# Research: {title}", ""]
    for j in jobs:
        out += [f"## {j.get('label') or j['q']}", "", f"_{j['q']}_", ""]
        if j.get("answer"):
            out += [j["answer"], ""]
        for it in j.get("items", []):
            link = f" <{it['url']}>" if it.get("url") else ""
            out.append(f"- **{it['title']}**{link}" + (f": {it['note']}" if it.get("note") else ""))
        out.append("")
    return "\n".join(out)


def read_versions(path: Path) -> list[dict]:
    """Every saved version of a document, oldest first. Skips a half-written last line."""
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except FileNotFoundError:
        pass
    return out


def replay_versions(path: Path) -> tuple[str, list[str]] | None:
    """Rebuild (current document, undo history) from versions.jsonl, or None if there is no log."""
    entries = read_versions(path)
    if not entries:
        return None
    doc: str | None = None
    hist: list[str] = []
    for e in entries:
        if e.get("by") == "undo":
            if hist:
                hist.pop()
        elif doc is not None:
            hist.append(doc)
            del hist[:-HISTORY]
        doc = e["doc"]
    return doc, hist


def list_sessions(root: Path, limit: int = 30) -> list[dict]:
    out = []
    if not root.exists():
        return out
    for p in root.iterdir():
        f = p / "doc.md"
        if (p / "session.json").exists() and f.exists():
            m = re.search(r"^#\s+(.+)$", f.read_text(encoding="utf-8"), re.M)
            out.append({"id": p.name, "title": m.group(1).strip() if m else "Untitled", "updated": f.stat().st_mtime})
    out.sort(key=lambda x: x["updated"], reverse=True)
    return out[:limit]
