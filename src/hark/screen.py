"""What the room screen can show, and spotting "Claude, show the methods" in the transcript
so the screen switches at once.

Only clear requests that name exactly one ## or ### section are handled here, without a
Claude call. Anything vaguer ("the bit where we talked about tariffs") is left to Claude,
which sees the same words in the transcript.
"""

from __future__ import annotations

import re

from .docops import HEADING, _loose

WAKE = re.compile(r"\b(claude|clawed|claud)\b", re.I)
# How speech recognition sometimes writes "Claude". Only trusted as the first word of a line.
SOUNDALIKES = {"claw", "claws", "clark", "clod", "clawd", "cloud", "clode", "clord"}
# Short words a misheard "Claude" or a bit of hesitation turns into ("Or go to previous section").
FILLERS = {"or", "for", "l", "well", "oh", "um", "uh", "er", "erm", "yeah", "yes", "ok", "okay", "right", "so", "and", "now"}
# A misheard name counts only at the start of a sentence, after any fillers: "So yeah. Claw, swap those."
MISHEARD = re.compile(r"(?:^|[.?!])\s*(?:(?:%s)\b[\s,]*)*(?:%s)\b" % ("|".join(sorted(FILLERS)), "|".join(sorted(SOUNDALIKES))), re.I)
LEADS = {"can", "could", "would", "will", "you", "now", "ok", "okay", "right", "so", "and", "then", "just", "hey"}
STOP = {"the", "a", "an", "our", "section", "part", "on", "screen", "up", "now", "again"}
NUMBERS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                       "fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}
MEETING_META = {"summary", "aims", "agenda", "decisions", "actions", "open questions"}
STEP = re.compile(r"(?:go |move |skip |jump )?(?:(?:on |back |forward )?to )?(?:the )?(next|previous|prior) section|"
                  r"(?:go |move |skip |jump )?(back|forward) (?:a|one) section|(?:the )?section (before|after)(?: this)?")
NUMBERED = re.compile(r"(?:section|part|number) (\w+)")
# What can be said with no "Claude": "next section", "go back a section", "section 4", "go to section three".
BARE = re.compile(r"(?:(?:let'?s |let us )?(?:go (?:back |on )?to|jump to|skip to|move to|switch to|show) )?"
                  r"(?:(?:the )?(?:next|previous|prior) section|(?:back|forward) (?:a|one) section|section \w+)")
FOLLOW = re.compile(r"(?:go back to |start )?follow(?:ing)?(?: along| the (?:conversation|discussion)| live)?(?: again)?")
SHOW = re.compile(r"(?:let'?s |let us )?(?:have a )?(?:look at|see|go back to|go to|turn to|show us|show me|show|jump to|"
                  r"switch to|skip to|move to|bring up|pull up|put up|open up)\s+(.+)")
PUT_UP = re.compile(r"put (.+?) (?:up|on (?:the )?screen)")


def screen_sections(doc: str, mode: str) -> list[str]:
    """Heading lines of the sections the room screen steps through, in order. Their position
    is the number shown beside them ("section 3"). Grilling items keep their own numbers."""
    heads = []
    for line in doc.split("\n"):
        m = HEADING.match(line.strip())
        if m:
            heads.append({"line": line.strip(), "level": len(m.group(1)), "key": _loose(m.group(2)), "parent": None, "kids": 0})
    for i, h in enumerate(heads):
        for j in range(i - 1, -1, -1):
            if heads[j]["level"] < h["level"]:
                h["parent"] = heads[j]
                heads[j]["kids"] += 1
                break
    out = []
    for h in heads:
        if not 2 <= h["level"] <= 3:
            continue
        p = h["parent"]["key"] if h["parent"] and h["parent"]["level"] == 2 else ""
        if "working notes" in (h["key"], p):
            continue  # still being weighed: not for the room
        if mode == "grill":
            ok = h["level"] == 3 and p == "items"
        elif mode == "meeting":
            if h["level"] == 3:
                ok = p == "discussion"
            else:
                ok = h["key"] == "summary" or (h["key"] not in MEETING_META and (h["key"] != "discussion" or not h["kids"]))
        else:
            ok = h["level"] == 2
        if ok:
            out.append(h["line"])
    return out


def _words(text: str) -> list[str]:
    """Lower-case words of the first sentence, without "please"."""
    first = re.split(r"[.?!](?:\s|$)", text.strip(), maxsplit=1)[0]
    return [w for w in re.sub(r"[^\w\s']", " ", first.lower()).split() if w != "please"]


def _wake_end(text: str) -> int | None:
    """Where the words to Claude start: just after "Claude", or after a misheard name opening a sentence."""
    m = WAKE.search(text) or MISHEARD.search(text)
    return m.end() if m else None


def addressed(text: str) -> bool:
    """Does this line speak to Claude, allowing for its name being misheard?"""
    return _wake_end(text) is not None


def _number(word: str) -> int | None:
    return int(word) if word.isdigit() else NUMBERS.get(word)


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
        n = _number(item.group(1))
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


def screen_request(text: str, doc: str, need_wake: bool = True, mode: str = "outline",
                   current: str | None = None) -> tuple[str, str | None] | None:
    """("show", heading line), ("follow", None), or None if this isn't a clear request about the screen.
    `current` is the section on screen now, for "next section"."""
    bare = False  # not addressed to Claude: only a sentence that is nothing but navigation counts
    if need_wake:
        end = _wake_end(text)
        if end is None:
            bare = True
        else:
            text = text[end:]
    words = _words(text)
    while words and words[0] in LEADS | (FILLERS if bare else set()):
        words.pop(0)
    said = " ".join(words)
    if bare and not BARE.fullmatch(said):
        return None
    if FOLLOW.fullmatch(said):
        return ("follow", None)
    target = said
    m = SHOW.fullmatch(said) or PUT_UP.fullmatch(said)
    if m:
        target = m.group(1)
    sections = screen_sections(doc, mode)
    step = STEP.fullmatch(target)
    if step:
        if mode == "meeting" or not sections:
            return None  # in meetings the screen lists the agenda, not these sections
        forward = (step.group(1) or step.group(2) or step.group(3)) in ("next", "forward", "after")
        i = sections.index(current) if current in sections else -1
        j = i + 1 if forward else (i - 1 if i >= 0 else 0)
        return ("show", sections[j]) if 0 <= j < len(sections) else None
    num = NUMBERED.fullmatch(target.removeprefix("the "))
    if num and mode in ("outline", "notes"):
        n = _number(num.group(1))
        return ("show", sections[n - 1]) if n and 1 <= n <= len(sections) else None
    if num and mode == "grill":
        target = f"item {num.group(1)}"  # grilling items carry their own numbers
    if not m and not num:
        return None
    heading = match_heading(target, doc)
    return ("show", heading) if heading else None
