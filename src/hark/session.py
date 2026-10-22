"""A session: one live document, its transcript, speakers and undo history.

Saved as plain files in a folder per session:
  doc.md          the document
  transcript.md   who said what, when
  session.json    everything needed to reopen it
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
        self.lines: list[dict] = []
        self.processed = 0  # lines[:processed] are reflected in the document
        self.speakers = Speakers(threshold)
        self._next_line = 1

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
        return not self.lines and self.doc.strip() == MODES[self.mode]["template"].strip()

    # -- mutations ----------------------------------------------------------
    def set_doc(self, markdown: str) -> bool:
        markdown = tidy(markdown)
        if markdown == self.doc:
            return False
        self.history.append(self.doc)
        del self.history[:-HISTORY]
        self.doc = markdown
        self.version += 1
        return True

    def undo(self) -> bool:
        if not self.history:
            return False
        self.doc = self.history.pop()
        self.version += 1
        return True

    def add_line(self, t: float, source: str, speaker: str, text: str, kind: str = "speech") -> dict:
        line = {"id": self._next_line, "t": t, "added": time.time(), "source": source, "speaker": speaker, "text": text, "kind": kind}
        self._next_line += 1
        self.lines.append(line)
        return line

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
        state = {
            "id": self.id, "created": self.created, "mode": self.mode, "brief": self.brief, "interval": self.interval,
            "doc": self.doc, "history": self.history[-20:], "lines": self.lines, "processed": self.processed,
            "next_line": self._next_line, "speakers": self.speakers.to_json(),
        }
        _atomic_write(self.dir / "session.json", json.dumps(state))

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
        s.lines = d.get("lines", [])
        s.processed = d.get("processed", len(s.lines))
        s._next_line = d.get("next_line", len(s.lines) + 1)
        s.speakers.load_json(d.get("speakers", {}))
        return s


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
