"""Spot "Claude, show the methods" in the transcript, so the room screen switches at once.

Only clear requests that name exactly one ## or ### section are handled here, without a
Claude call. Anything vaguer ("the bit where we talked about tariffs") is left to Claude,
which sees the same words in the transcript.
"""

from __future__ import annotations

import re

from .docops import HEADING, _loose

WAKE = re.compile(r"\b(claude|clawed|claud)\b", re.I)
LEADS = {"can", "could", "would", "will", "you", "now", "ok", "okay", "right", "so", "and", "then", "just", "hey"}
STOP = {"the", "a", "an", "our", "section", "part", "on", "screen", "up", "now", "again"}
NUMBERS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                       "fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}
FOLLOW = re.compile(r"(?:go back to |start )?follow(?:ing)?(?: along| the (?:conversation|discussion)| live)?(?: again)?")
SHOW = re.compile(r"(?:let'?s |let us )?(?:have a )?(?:look at|see|go back to|go to|turn to|show us|show me|show|jump to|"
                  r"switch to|skip to|move to|bring up|pull up|put up|open up)\s+(.+)")
PUT_UP = re.compile(r"put (.+?) (?:up|on (?:the )?screen)")


def _stem(w: str) -> str:
    w = w.strip("'")
    return w[:-1] if len(w) > 3 and w.endswith("s") else w


def match_heading(target: str, doc: str) -> str | None:
    """The one ## or ### heading line that `target` ("methods", "item three") names, or None."""
    heads = []
    for line in doc.split("\n"):
        m = HEADING.match(line.strip())
        if m and 2 <= len(m.group(1)) <= 3:
            heads.append((line.strip(), m.group(2), len(m.group(1))))
    words = target.split()
    while words and words[0] in STOP:
        words.pop(0)
    while words and words[-1] in STOP:
        words.pop()
    item = re.fullmatch(r"(?:item|number|idea) (\w+)", " ".join(words))
    if item:
        n = NUMBERS.get(item.group(1)) if not item.group(1).isdigit() else int(item.group(1))
        hits = [line for line, text, lv in heads if lv == 3 and re.match(rf"{n}[.)]\s", text)]
        return hits[0] if len(hits) == 1 and n is not None else None
    content = [_stem(w) for w in words if w not in STOP]
    if not content or len(content) > 6:
        return None
    exact = [line for line, text, _ in heads if _loose(text) == " ".join(words)]
    if len(exact) == 1:
        return exact[0]

    def names(text: str) -> bool:
        hw = {_stem(w) for w in re.findall(r"[\w']+", _loose(text))}
        # every word said is in the heading; "intro" matches "introduction"
        return all(any(h == c or (len(c) >= 4 and h.startswith(c)) for h in hw) for c in content)

    hits = [line for line, text, _ in heads if names(text)]
    return hits[0] if len(hits) == 1 else None


def screen_request(text: str, doc: str, need_wake: bool = True) -> tuple[str, str | None] | None:
    """("show", heading line), ("follow", None), or None if this isn't a clear request about the screen."""
    if need_wake:
        m = WAKE.search(text)
        if not m:
            return None
        text = text[m.end():]
    first = re.split(r"[.?!](?:\s|$)", text.strip(), maxsplit=1)[0]  # the first sentence only
    words = [w for w in re.sub(r"[^\w\s']", " ", first.lower()).split() if w != "please"]
    while words and words[0] in LEADS:
        words.pop(0)
    said = " ".join(words)
    if FOLLOW.fullmatch(said):
        return ("follow", None)
    m = SHOW.fullmatch(said) or PUT_UP.fullmatch(said)
    if not m:
        return None
    heading = match_heading(m.group(1), doc)
    return ("show", heading) if heading else None
