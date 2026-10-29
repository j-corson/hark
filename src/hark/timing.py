"""Meeting time and aims: when a session ends, what's still open, and the reminder near the end.

The end time is a session setting (from the agenda dialog, ☰ → Meeting time, or said out loud
and passed on by Claude). Aims live in the document as a "## Aims" checklist, beside "## Agenda",
so people can see and edit them and Claude ticks them off like agenda items.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta

NUDGE_BEFORE = 10 * 60  # the reminder comes this long before the end
_CLOCK = re.compile(r"^\s*(\d{1,2})(?:[:.h]?(\d{2}))?\s*(am|pm|a\.m\.|p\.m\.)?\s*$", re.I)
_HEAD = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_BOX = re.compile(r"^[-*+]\s+\[([ xX])\]\s+(.*\S)\s*$")


def parse_clock(text: str, now: float | None = None, exact: bool = False) -> float | None:
    """A time of day ("11:30", "1130", "3pm", "15.00") → a timestamp today, or None.

    With `exact`, the hour is on the 24-hour clock (a time picker, or Claude). Otherwise a bare
    "3" or "4:30" with no am/pm is taken as the next time it comes round, so "ends at 4:30"
    said at two in the afternoon means 16:30."""
    m = _CLOCK.match(str(text or ""))
    if not m:
        return None
    h, mins, half = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower().replace(".", "")
    if h > 23 or mins > 59 or (half and not 1 <= h <= 12):
        return None
    if half:
        h = h % 12 + (12 if half == "pm" else 0)
    base = datetime.fromtimestamp(now if now is not None else time.time())
    at = base.replace(hour=h, minute=mins, second=0, microsecond=0)
    if exact or half or h == 0 or h > 12:
        return at.timestamp()
    # Ambiguous: the morning or afternoon time, whichever comes next (allowing for running a little late).
    for cand in (at, at + timedelta(hours=12)) if h < 12 else (at,):
        if cand.timestamp() >= base.timestamp() - 30 * 60:
            return cand.timestamp()
    return at.timestamp()


def hhmm(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%H:%M")


def minutes(secs: float) -> str:
    """"14 minutes", or "2 hours 5 minutes" once it's over an hour and a half."""
    n = max(0, round(secs / 60))
    if n < 90:
        return f"{n} minute{'s' * (n != 1)}"
    h, m = divmod(n, 60)
    return f"{h} hour{'s' * (h != 1)}" + (f" {m} minute{'s' * (m != 1)}" if m else "")


def clock_note(now: float, ends: float | None) -> str:
    """What Claude is told about the time."""
    out = f"Now {hhmm(now)}."
    if ends:
        left = ends - now
        if left >= 0:
            out += f" This session is due to end at {hhmm(ends)}: {minutes(left)} left."
        else:
            out += f" This session was due to end at {hhmm(ends)}, {minutes(-left)} ago."
    return out


def checklist(doc: str, name: str) -> list[tuple[bool, str]] | None:
    """The top-level checklist items of a "## <name>" section: [(ticked, text)], or None if there's no such section."""
    lines = doc.split("\n")
    start = None
    for i, line in enumerate(lines):
        m = _HEAD.match(line.strip())
        if m and len(m.group(1)) == 2 and m.group(2).strip().lower() == name:
            start = i
            break
    if start is None:
        return None
    out = []
    for line in lines[start + 1:]:
        m = _HEAD.match(line.strip())
        if m and len(m.group(1)) <= 2:
            break
        if line[:1] in (" ", "\t"):
            continue  # notes under an item
        b = _BOX.match(line)
        if b:
            out.append((b.group(1) != " ", b.group(2)))
    return out


def still_open(doc: str) -> tuple[list[str], list[str]]:
    """(aims not yet met, agenda items not yet ticked)."""
    aims = checklist(doc, "aims") or []
    agenda = checklist(doc, "agenda") or []
    return [t for done, t in aims if not done], [t for done, t in agenda if not done]


def _plain(text: str) -> str:
    return re.sub(r"[*_`]+", "", text).strip()


def _some(items: list[str], n: int = 3) -> str:
    shown = "; ".join(_plain(i) for i in items[:n])
    return shown + (f"; and {len(items) - n} more" if len(items) > n else "")


def reminder(doc: str, now: float, ends: float) -> str | None:
    """The reminder shown near the end, or None if nothing is left open (or there's nothing to check against)."""
    aims, items = still_open(doc)
    if not aims and not items:
        return None
    parts = [f"About {minutes(ends - now)} left (ends {hhmm(ends)})."]
    if aims:
        parts.append(f"Aim{'s' * (len(aims) != 1)} not met yet: {_some(aims)}.")
    if items:
        parts.append(f"Still on the agenda: {_some(items)}.")
    return " ".join(parts)
