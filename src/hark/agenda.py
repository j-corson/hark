"""Start a session from an agenda written before the meeting.

An agenda is plain Markdown or text: an optional "# Title", then one item per
top-level bullet (or per heading, if there are no bullets). Indented lines under
an item are notes on it. Anything before the first item is kept as context.

Meeting (and other modes): the agenda becomes a "## Agenda" checklist that the
scribe ticks off as the meeting moves on. Grilling: each item becomes a numbered
spec, and one Claude call drafts the first questions before anyone speaks.

An agenda can also say what the meeting is for and when it ends:
  Aims: agree the budget split; pick a venue      (or a "## Aims" section of bullets)
  Ends: 11:30
Aims become a "## Aims" checklist above the agenda; the end time is a session setting.
"""

from __future__ import annotations

import re

from .modes import MODES

_H1 = re.compile(r"^#\s+(.+?)\s*#*\s*$")
_HEADING = re.compile(r"^#{2,6}\s+(.+?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.*\S)\s*$")
_NUMBERED = re.compile(r"^\d+[.)]\s+")
_AIMS = re.compile(r"^(?:#{1,6}\s+)?(?:\*\*)?(?:aims?|goals?|objectives?)(?:\*\*)?\s*(?::|$)\s*(?:\*\*)?\s*(.*?)\s*$", re.I)
_ENDS = re.compile(r"^(?:\*\*)?(?:ends?|end time|finish(?:es)?|finish time)(?:\*\*)?(?:\s+at)?\s*:?\s*(?:\*\*)?\s*(.+?)\s*$", re.I)

MAX_ITEMS = 60
MAX_CHARS = 20000

PREP_SYSTEM = """You prepare the document a grilling session will start from, before anyone has spoken. You receive the <brief> the live session follows and the <agenda>: the ideas people put forward, in their own words.

Return the whole document in `document`, laid out as the brief describes:
- An H1 title: the agenda's own title if it has one, otherwise a short descriptive one.
- Every agenda item, in agenda order, as a numbered ### item under "## Items" with all the brief's fields. Fill a field only from what the agenda says, keeping people's wording; otherwise "?". Status: not started. Keep the agenda's notes on an item as extra bullets after its fields. Lose no item and no note.
- Under every item, "#### Open questions" with its first round: up to 3 questions that can be answered now, most important first, in the brief's format, each with your suggested answer. Start with whatever decides Question, Track and Done when.
- Facts the group shouldn't have to decide (whether a dataset is public, where to get it, its size or licence) go under the item as `- Look up: ...`, not as questions.
- If <aims> is present, "## Aims" right after the title, with each aim as `- [ ] aim`, in the user's words.
- "## Shared terms" (with any term the agenda clearly uses in more than one sense, as a question under the item where it matters) and "## Parked", empty if there is nothing for them.
Don't invent facts. Suggested answers are suggestions for the group to accept or change, so make them concrete but plausible.

In `research`, list up to 3 facts most worth looking up before the meeting, each as {"label": "<at most 8 words>", "question": "<self-contained question>"}. Facts only, never decisions.
In `summary`, say in at most 10 words what you prepared, e.g. "6 items, first questions drafted"."""

PREP_SCHEMA = {
    "type": "object",
    "properties": {
        "document": {"type": "string"},
        "summary": {"type": "string"},
        "research": {
            "type": "array",
            "items": {"type": "object", "properties": {"label": {"type": "string"}, "question": {"type": "string"}}, "required": ["question"]},
        },
    },
    "required": ["document"],
}


def plan(text: str) -> tuple[str, list[str], str]:
    """Take the aims and end time out of an agenda: (the rest of the text, aims, end time as written)."""
    rest, aims, ends = [], [], ""
    collecting = None  # "heading": a "## Aims" section, until the next heading; "label": "Aims:", until a blank line
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if collecting:
            if collecting == "label" and not line or collecting == "heading" and (_HEADING.match(line) or _H1.match(line)):
                collecting = None
            elif line:
                b = _BULLET.match(raw)
                aims.append(b.group(2) if b else line)
                continue
        a = _AIMS.match(line) if not _BULLET.match(raw) else None
        e = _ENDS.match(line) if not _BULLET.match(raw) else None
        if a:
            inline = a.group(1)
            aims += [x.strip() for x in inline.split(";") if x.strip()]
            if not inline:
                collecting = "heading" if line.startswith("#") else "label"
            continue
        if e and not ends and re.match(r"\d", e.group(1)):
            ends = e.group(1)
            continue
        rest.append(raw)
    return "\n".join(rest), aims[:10], ends


def parse(text: str) -> tuple[str, list[str], list[dict]]:
    """Agenda text → (title, context lines, [{"text", "notes"}])."""
    lines = text.replace("\r\n", "\n").replace("﻿", "").split("\n")
    title = ""
    top_bullets = any((m := _BULLET.match(l)) and not m.group(1) for l in lines)
    context: list[str] = []
    items: list[dict] = []
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue
        if not title and not items and (m := _H1.match(line)):
            title = m.group(1)
            continue
        b = _BULLET.match(line)
        h = _HEADING.match(line)
        if b and not b.group(1) and top_bullets:
            items.append({"text": b.group(2), "notes": []})
        elif h and not top_bullets:
            items.append({"text": _NUMBERED.sub("", h.group(1)), "notes": []})
        elif h:
            continue  # with bullets as the items, headings only group them
        elif items:
            items[-1]["notes"].append(b.group(2) if b else line.strip())
        else:
            context.append(b.group(2) if b else line.strip())
    if not items:  # a plain list of lines, one idea per line
        items = [{"text": c, "notes": []} for c in context]
        context = []
    return title, context, items[:MAX_ITEMS]


def _with_title(template: str, title: str) -> str:
    return re.sub(r"^# .*$", f"# {title}", template, count=1, flags=re.M) if title else template


def _insert_section(doc: str, section: str, before: str) -> str:
    """Put `section` just above the `before` heading, or at the end if there is none."""
    i = doc.find(f"\n{before}\n")
    if i < 0:
        return doc.rstrip("\n") + "\n\n" + section + "\n"
    return doc[:i + 1] + section + "\n\n" + doc[i + 1:]


def aims_section(aims: list[str]) -> str:
    return "## Aims\n" + "\n".join(f"- [ ] {a}" for a in aims)


def layout(text: str, mode: str, aims: list[str] | None = None) -> str:
    """The document a session starts from, before Claude has seen the agenda."""
    title, context, items = parse(text)
    template = _with_title(MODES[mode]["template"], title)
    if aims and mode != "grill":
        template = _insert_section(template, aims_section(aims), "## Discussion")
    if mode == "grill":
        if aims:
            template = template.replace("\n## Items\n", "\n" + aims_section(aims) + "\n\n## Items\n", 1)
        body = []
        for n, it in enumerate(items, 1):
            body += [f"### {n}. {it['text']}", "- **Question**: ?", *(f"- {x}" for x in it["notes"]), "- **Status**: not started", ""]
        if context:
            template = template.replace("\n## Items\n", "\n" + "\n".join(context) + "\n\n## Items\n", 1)
        return template.replace("## Items\n", "## Items\n\n" + "\n".join(body), 1)
    if not items and not context:
        return template
    rows = list(context)
    for it in items:
        rows.append(f"- [ ] {it['text']}")
        rows += [f"  - {x}" for x in it["notes"]]
    return _insert_section(template, "## Agenda\n" + "\n".join(rows), "## Discussion")


def build_prep_prompt(brief: str, agenda: str, aims: list[str] | None = None) -> str:
    parts = [f"<brief>\n{brief.strip()}\n</brief>", f"<agenda>\n{agenda.strip()[:MAX_CHARS]}\n</agenda>"]
    if aims:
        parts.append("<aims>\n" + "\n".join(f"- {a}" for a in aims) + "\n</aims>")
    return "\n\n".join(parts + ["Return the prepared document."])
