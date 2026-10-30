"""Build a new session from a saved transcript, for when a document was lost or ruined.

Reads a Teams transcript (.vtt, as downloaded from the meeting recap) or hark's
own transcript.md, then runs the lines through Claude in chunks, the same way
the live loop does, into a fresh session. Nothing existing is overwritten.
"""

from __future__ import annotations

import html
import re
from datetime import datetime
from pathlib import Path

from .docops import apply_ops
from .scribe import Scribe, ScribeError, build_prompt
from .session import Session

CHUNK_CHARS = 2500  # roughly three minutes of speech per Claude call
CONTEXT_LINES = 12
MERGE_CHARS = 400  # join a speaker's consecutive caption cues up to this length

_TS = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})(?:[.,](\d+))?")
_CUE = re.compile(r"^\s*(\S+)\s+-->\s+(\S+)")
_VOICE = re.compile(r"<v(?:\.[^ >]*)?\s+([^>]+)>")
_TAG = re.compile(r"</?[^>]+>")
_HARK = re.compile(r"^(\(typed\) )?\[(\d{1,2}:\d{2}:\d{2})\] (.+?): (.*)$")


def _seconds(ts: str) -> float:
    m = _TS.fullmatch(ts.strip())
    if not m:
        raise ValueError(f"bad timestamp {ts!r}")
    h, mi, s, frac = m.groups()
    return int(h or 0) * 3600 + int(mi) * 60 + int(s) + (float(f"0.{frac}") if frac else 0.0)


def parse_vtt(text: str) -> list[dict]:
    """Teams WebVTT → [{t, speaker, text, kind}], t in seconds from the start."""
    out: list[dict] = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("﻿", ""))
    for block in blocks:
        lines = block.strip().split("\n")
        cue = next((i for i, l in enumerate(lines) if _CUE.match(l)), None)
        if cue is None:
            continue  # header, NOTE or STYLE block
        start = _seconds(_CUE.match(lines[cue]).group(1))
        body = " ".join(lines[cue + 1:]).strip()
        m = _VOICE.search(body)
        speaker = html.unescape(m.group(1).strip()) if m else "Unknown"
        words = html.unescape(_TAG.sub("", body)).strip()
        if not m and ": " in words[:60]:
            speaker, words = words.split(": ", 1)  # "Name: text" captions without a voice tag
        if not words:
            continue
        prev = out[-1] if out else None
        if prev and prev["speaker"] == speaker and len(prev["text"]) + len(words) < MERGE_CHARS:
            prev["text"] += " " + words
        else:
            out.append({"t": start, "speaker": speaker, "text": words, "kind": "speech"})
    return out


def parse_hark(text: str) -> list[dict]:
    """hark's transcript.md → lines; t is the clock time as seconds after midnight."""
    out = []
    for line in text.splitlines():
        m = _HARK.match(line.strip())
        if m:
            typed, clock, speaker, words = m.groups()
            out.append({"t": _seconds(clock), "speaker": speaker, "text": words, "kind": "typed" if typed else "speech"})
    return out


def read_transcript(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = parse_vtt(text) if text.lstrip("﻿").startswith("WEBVTT") or path.suffix.lower() == ".vtt" else parse_hark(text)
    if not lines:
        raise SystemExit(f"No transcript lines found in {path}. hark reads Teams .vtt files and its own transcript.md.")
    return lines


def chunks(lines: list[dict], size: int = CHUNK_CHARS) -> list[tuple[int, int]]:
    """Split line indexes into [start, end) runs of about `size` characters. Typed instructions end a run."""
    out, a, chars = [], 0, 0
    for i, l in enumerate(lines):
        chars += len(l["text"])
        if chars >= size or l["kind"] == "typed":
            out.append((a, i + 1))
            a, chars = i + 1, 0
    if a < len(lines):
        out.append((a, len(lines)))
    return out


async def rebuild(path: Path, data_dir: Path, mode: str, scribe: Scribe, say=print) -> Session:
    lines = read_transcript(path)
    s = Session(data_dir, mode=mode)
    base = datetime.combine(datetime.fromtimestamp(path.stat().st_mtime).date(), datetime.min.time()).timestamp()
    for l in lines:
        s.add_line(base + l["t"], "rebuild", s.speakers.named(l["speaker"][:40]), l["text"], kind=l["kind"])
    s.save()
    runs = chunks(s.lines)
    say(f"Rebuilding from {len(s.lines)} lines in {len(runs)} step{'s' * (len(runs) != 1)} into {s.dir}")
    for n, (a, b) in enumerate(runs, 1):
        part = s.lines[a:b]
        earlier = [s.fmt_line(l) for l in s.lines[max(0, a - CONTEXT_LINES):a] if l["kind"] == "speech"]
        new = [s.fmt_line(l) for l in part if l["kind"] == "speech"]
        typed = [l["text"] for l in part if l["kind"] == "typed"]
        prompt = build_prompt(s.brief, s.doc, s.speaker_notes(), earlier, new, typed)
        for attempt in (1, 2):
            try:
                data, took = await scribe.edit(prompt, timeout=240)
                break
            except ScribeError as e:
                if attempt == 2:
                    s.save()
                    raise SystemExit(f"  Claude failed at step {n}: {e}\n  The partial document is saved; "
                                     f"open it in hark and it will carry on with the remaining lines.")
                say(f"  step {n} failed ({e}), trying again")
        doc, failed = apply_ops(s.doc, data.get("ops") or [])
        s.set_doc(doc, by="claude")
        s.processed = b
        s.save()
        say(f"  step {n}/{len(runs)}: {len(data.get('ops') or []) - len(failed)} edits in {took:.0f}s")
    say(f"Done: \"{s.title}\". Open it in hark from the ☰ menu: Open a previous session.")
    return s
