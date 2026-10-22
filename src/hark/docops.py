"""Apply Claude's edit operations to a Markdown document.

Claude never rewrites the whole document. It returns small anchored edits
("replace this text", "append under this heading"). They are applied to the
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


def apply_op(doc: str, op: dict) -> str | None:
    """Apply one op. Returns the new document, or None if it could not apply."""
    kind = op.get("op")
    find = op.get("find") or ""
    text = op.get("text") or ""
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
            body = doc.rstrip("\n")
            return f"{body}\n\n{h}\n{text.strip(chr(10))}\n"
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


def apply_ops(doc: str, ops: list[dict]) -> tuple[str, list[dict]]:
    """Apply ops in order. Returns (new_doc, ops_that_failed)."""
    failed = []
    for op in ops:
        new = apply_op(doc, op)
        if new is None:
            failed.append(op)
        else:
            doc = new
    return tidy(doc), failed


def changed_lines(old: str, new: str) -> list[int]:
    """Line numbers in `new` that were added or changed relative to `old`."""
    a, b = old.split("\n"), new.split("\n")
    out = []
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "insert"):
            out.extend(j for j in range(j1, j2) if b[j].strip())
    return out
