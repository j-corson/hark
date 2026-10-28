"""Apply Claude's edit operations to a Markdown document.

Claude's live updates are small anchored edits ("replace this text", "append
under this heading", "rewrite this section"). They are applied to the
document as it is *now*, so a hand edit made while Claude was thinking survives:
an edit whose anchor has gone is simply skipped.
"""

from __future__ import annotations

import difflib
import re

HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def _heading_key(line: str) -> str | None:
    m = HEADING.match(line.strip())
    return _norm(m.group(2)) if m else None


def _loose(text: str) -> str:
    """Heading text without emphasis or a leading number, so "### 3. **X**" matches "### 4. X"."""
    return _norm(re.sub(r"^\d+[.)]\s*", "", re.sub(r"[*_`]", "", text)))


def find_heading(doc: str, want: str | None) -> str | None:
    """The ## or ### heading line in `doc` that `want` names (a heading line, or just its text).

    An exact line wins; otherwise a unique match ignoring case, emphasis and numbering, so a
    section still matches after it is renumbered. None if nothing (or more than one) matches."""
    want = (want or "").strip()
    if not want:
        return None
    heads = []
    for line in doc.split("\n"):
        m = HEADING.match(line.strip())
        if m and 2 <= len(m.group(1)) <= 3:
            heads.append((line.strip(), m.group(2)))
    if any(line == want for line, _ in heads):
        return want
    m = HEADING.match(want)
    key = _loose(m.group(2) if m else want)
    hits = [line for line, text in heads if _loose(text) == key]
    return hits[0] if len(hits) == 1 else None


def _find_lines(lines: list[str], find: str) -> tuple[int, int] | None:
    """Locate `find` as a run of whole lines, ignoring whitespace and case.

    Returns (start, end) line indices, end exclusive."""
    want = [_norm(x) for x in find.strip("\n").split("\n")]
    want = [w for w in want if w] or None
    if not want:
        return None
    have = [_norm(x) for x in lines]
    for i in range(len(have)):
        if have[i] != want[0]:
            continue
        j, k = i, 0
        while j < len(have) and k < len(want):
            if have[j] == "" and want[k] != "":
                j += 1  # tolerate blank lines inside the anchor
                continue
            if have[j] != want[k]:
                break
            j += 1
            k += 1
        if k == len(want):
            return i, j
    return None


def _section_end(lines: list[str], start: int) -> int:
    """Index of the first line after the section whose heading is at `start`."""
    level = len(HEADING.match(lines[start].strip()).group(1))
    for i in range(start + 1, len(lines)):
        m = HEADING.match(lines[i].strip())
        if m and len(m.group(1)) <= level:
            return i
    return len(lines)


def _insert_block(lines: list[str], at: int, text: str) -> list[str]:
    new = text.strip("\n").split("\n")
    # Headings read better with a blank line above them.
    if new and HEADING.match(new[0].strip()) and at > 0 and lines[at - 1].strip():
        new = [""] + new
    return lines[:at] + new + lines[at:]


def _replace_text(doc: str, find: str, text: str) -> str | None:
    if find and find in doc:
        return doc.replace(find, text, 1)
    lines = doc.split("\n")
    span = _find_lines(lines, find)
    if span is None:
        return None
    a, b = span
    repl = text.strip("\n").split("\n") if text.strip() else []
    return "\n".join(lines[:a] + repl + lines[b:])


WORKING_NOTES = "working notes"


def _add_section(doc: str, block: str) -> str:
    """Add a new section at the end, but above "## Working notes", which stays last."""
    lines = doc.rstrip("\n").split("\n")
    at = next((i for i, l in enumerate(lines) if _heading_key(l) == WORKING_NOTES), len(lines))
    new = block.strip("\n").split("\n")
    return "\n".join(lines[:at] + [""] + new + [""] + lines[at:]) + "\n"


def _section_span(lines: list[str], heading: str) -> tuple[int, int] | None:
    """(start, end) of the section under `heading`, subsections included."""
    key = _heading_key(heading) if HEADING.match(heading) else _norm(heading)
    idx = next((i for i, l in enumerate(lines) if _heading_key(l) == key), None)
    return None if idx is None else (idx, _section_end(lines, idx))


def section_text(doc: str, heading: str) -> str | None:
    lines = doc.split("\n")
    span = _section_span(lines, heading)
    return None if span is None else "\n".join(lines[span[0]:span[1]]).strip()


def apply_op(doc: str, op: dict) -> str | None:
    """Apply one op. Returns the new document, or None if it could not apply."""
    kind = op.get("op")
    find = op.get("find") or ""
    text = op.get("text") or ""
    if kind == "replace_section":
        heading = (op.get("heading") or "").strip()
        if not heading:
            return None
        lines = doc.split("\n")
        span = _section_span(lines, heading)
        new = text.strip("\n").split("\n") if text.strip() else []
        if new and not HEADING.match(new[0].strip()):
            # Body only: keep the existing heading line.
            new = [lines[span[0]] if span else (heading if HEADING.match(heading) else f"## {heading}")] + new
        if span is None:
            return _add_section(doc, "\n".join(new)) if new else None
        a, b = span
        if len(HEADING.match(lines[a].strip()).group(1)) == 1:
            return None  # the title's "section" is the whole document; that's a tidy, not an edit
        if new and b < len(lines):
            new = new + [""]
        return "\n".join(lines[:a] + new + lines[b:])
    if kind == "replace":
        if not find:
            return None
        return _replace_text(doc, find, text)
    if kind == "delete":
        if not find:
            return None
        return _replace_text(doc, find, "")
    if kind == "insert_after":
        lines = doc.split("\n")
        span = _find_lines(lines, find)
        if span is None:
            return None
        return "\n".join(_insert_block(lines, span[1], text))
    if kind == "append_to_section":
        heading = (op.get("heading") or "").strip()
        lines = doc.split("\n")
        key = _heading_key(heading) if HEADING.match(heading) else _norm(heading)
        idx = next((i for i, l in enumerate(lines) if _heading_key(l) == key), None)
        if idx is None:
            # Section does not exist yet: create it at the end.
            h = heading if HEADING.match(heading) else f"## {heading}"
            return _add_section(doc, f"{h}\n{text.strip(chr(10))}")
        end = _section_end(lines, idx)
        at = end
        while at - 1 > idx and not lines[at - 1].strip():
            at -= 1  # insert before the section's trailing blank lines
        out = _insert_block(lines, at, text)
        # Keep a blank line between this section and the next heading.
        nxt = at + len(text.strip("\n").split("\n"))
        if nxt < len(out) and HEADING.match(out[nxt].strip()):
            out.insert(nxt, "")
        return "\n".join(out)
    return None


def tidy(doc: str) -> str:
    """Collapse runs of blank lines and make sure the file ends with one newline."""
    doc = re.sub(r"\n{3,}", "\n\n", doc.replace("\r\n", "\n"))
    return doc.strip("\n") + "\n"


def apply_ops(doc: str, ops: list[dict], base: str | None = None) -> tuple[str, list[dict]]:
    """Apply ops in order. Returns (new_doc, ops_that_failed).

    `base` is the document Claude was shown. If someone edited a section by hand
    since then, a whole-section rewrite of it is skipped so their edit survives."""
    failed = []
    start = doc
    for op in ops:
        if (op.get("op") == "replace_section" and base is not None and base != start
                and section_text(base, op.get("heading") or "") != section_text(start, op.get("heading") or "")):
            failed.append(op)
            continue
        new = apply_op(doc, op)
        if new is None:
            failed.append(op)
        else:
            doc = new
    return tidy(doc), failed


def rename_in_doc(doc: str, old: str, new: str) -> str:
    """Swap a person's old name or label for the new one, as whole words only."""
    if not old.strip() or not new.strip() or old == new:
        return doc
    return re.sub(rf"(?<!\w){re.escape(old)}(?!\w)", new, doc)


def changed_lines(old: str, new: str) -> list[int]:
    """Line numbers in `new` that were added or changed relative to `old`."""
    a, b = old.split("\n"), new.split("\n")
    out = []
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "insert"):
            out.extend(j for j in range(j1, j2) if b[j].strip())
    return out
