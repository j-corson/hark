"""GitHub issues as grilling items: bring an issue in, grill it, post its spec back as a comment.

Everything goes through the GitHub CLI (`gh`), so hark never sees a token. An item
is an *issue item* while it has a `- **Issue**: owner/repo#N` field; that field is
the link, wherever the item moves and whatever it is renamed to. Comments are
built from the document as it stands, with no Claude call. Closing an issue is
the one step that asks Claude: it checks every question the item was asked was
answered before hark posts the last comment and closes the issue.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil

from .docops import HEADING, find_heading

_OWNER_REPO = r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+"
REPO = re.compile(rf"^{_OWNER_REPO}$")
_FULL = re.compile(rf"^({_OWNER_REPO})#(\d+)$")
_URL = re.compile(rf"^(?:https?://)?(?:www\.)?github\.com/({_OWNER_REPO})/issues/(\d+)(?:[/?#].*)?$", re.I)
_SHORT = re.compile(r"^#?(\d+)$")
ISSUE_FIELD = re.compile(rf"^[-*]\s+\*\*Issue\*\*:\s*({_OWNER_REPO}#\d+)\s*$")
_FIELD = re.compile(r"^[-*]\s+\*\*([^*]+?)\*\*:\s*(.*?)\s*$")
_TERM = re.compile(r"^(?:[-*]\s+)?\*\*([^*]+?)\*\*:\s*(.*?)\s*$")  # "**Term**: definition", bullet or not
_QUESTION = re.compile(r"^[-*]\s+\*\*(Q\d+[.)]?\s*([^*]*?))\*\*:?\s*(.*?)\s*$")
_LOOKUP = re.compile(r"^\s*[-*]\s+Look up:\s*(.*?)\s*$", re.I)
_DISPUTED = re.compile(r"\s*\(disputed:\s*(.*?)\)\s*$")
_NUMBER = re.compile(r"^(#+\s+)\d+[.)]\s*")
DISCLAIMER = "> *Drafted with hark from a meeting discussion.*"
GH_NEEDED = "GitHub issues need the GitHub CLI, logged in (`gh auth login`)"
UNSET = ("", "?")


def parse_ref(text: str, default_repo: str | None = None) -> tuple[str, int] | None:
    """An issue named as `owner/repo#12`, its URL, or `#12` / `12` in `default_repo`. None if it isn't one."""
    text = (text or "").strip()
    m = _FULL.match(text) or _URL.match(text)
    if m:
        repo, n = m.group(1), int(m.group(2))
    elif (m := _SHORT.match(text)) and default_repo and REPO.match(default_repo.strip()):
        repo, n = default_repo.strip(), int(m.group(1))
    else:
        return None
    return (repo, n) if n > 0 else None


def ref_str(repo: str, n: int) -> str:
    return f"{repo}#{n}"


# -- the GitHub CLI ---------------------------------------------------------
class GhError(Exception):
    """A `gh` call failed. The message is gh's own first line of complaint."""


_WHICH = object()


class GhRunner:
    """The few `gh` calls hark needs. Argument lists only, never a shell; comment bodies go on stdin."""

    def __init__(self, exe=_WHICH, spawn=None, timeout: float = 30):
        self.exe = shutil.which("gh") if exe is _WHICH else exe
        self.spawn = spawn or asyncio.create_subprocess_exec
        self.timeout = timeout
        self._ok: bool | None = None

    async def available(self) -> bool:
        """`gh` is installed and logged in. Checked once, then remembered."""
        if self._ok is None:
            try:
                await self._run(["auth", "status"])
                self._ok = True
            except GhError:
                self._ok = False
        return self._ok

    async def _run(self, args: list[str], stdin: str | None = None) -> str:
        if not self.exe:
            raise GhError("the GitHub CLI (gh) isn't installed")
        pipe = asyncio.subprocess.PIPE
        try:
            proc = await self.spawn(self.exe, *args, stdin=pipe if stdin is not None else asyncio.subprocess.DEVNULL,
                                    stdout=pipe, stderr=pipe)
        except OSError as e:
            raise GhError(f"couldn't run gh: {e}") from e
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin.encode() if stdin is not None else None), self.timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except (OSError, AttributeError):
                pass
            raise GhError(f"gh didn't answer within {self.timeout:.0f}s")
        if proc.returncode:
            first = next((l.strip() for l in (err or b"").decode(errors="replace").split("\n") if l.strip()), "")
            raise GhError(first[:300] or f"gh failed (exit {proc.returncode})")
        return (out or b"").decode(errors="replace")

    @staticmethod
    def _check(repo: str, n: int | None = None) -> None:
        if not REPO.match(repo or ""):
            raise GhError(f"{repo!r} isn't a repo; write it as owner/repo")
        if n is not None and (not isinstance(n, int) or n < 1):
            raise GhError(f"{n!r} isn't an issue number")

    async def list_open(self, repo: str, limit: int = 50) -> list[dict]:
        """Open issues, newest first: [{"number", "title", "labels": [names]}]."""
        self._check(repo)
        out = await self._run(["issue", "list", "-R", repo, "--state", "open", "--limit", str(limit), "--json", "number,title,labels"])
        try:
            rows = json.loads(out or "[]")
        except json.JSONDecodeError as e:
            raise GhError("gh sent back something that wasn't JSON") from e
        return [{"number": r["number"], "title": str(r.get("title") or ""),
                 "labels": [str(l.get("name")) for l in r.get("labels") or [] if isinstance(l, dict)]}
                for r in rows if isinstance(r, dict) and isinstance(r.get("number"), int)]

    async def view(self, repo: str, n: int) -> dict:
        """One issue: {"title", "body", "url", "state"}."""
        self._check(repo, n)
        out = await self._run(["issue", "view", str(n), "-R", repo, "--json", "title,body,url,state"])
        try:
            d = json.loads(out)
        except json.JSONDecodeError as e:
            raise GhError("gh sent back something that wasn't JSON") from e
        return {k: str(d.get(k) or "") for k in ("title", "body", "url", "state")}

    async def comment(self, repo: str, n: int, body: str) -> str:
        """Add one comment and return its URL."""
        self._check(repo, n)
        out = await self._run(["issue", "comment", str(n), "-R", repo, "--body-file", "-"], stdin=body)
        url = next((l.strip() for l in out.split("\n") if l.strip().startswith("http")), "")
        if not url:
            raise GhError("gh didn't say where the comment went, so check the issue before posting again")
        return url

    async def close(self, repo: str, n: int) -> None:
        """Close an issue as completed."""
        self._check(repo, n)
        await self._run(["issue", "close", str(n), "-R", repo, "--reason", "completed"])


# -- issue items in the document -------------------------------------------
def _level(line: str) -> int:
    m = HEADING.match(line.strip())
    return len(m.group(1)) if m else 0


def _items(doc: str) -> list[tuple[str, list[str]]]:
    """Every ### section: (heading line, its lines below the heading, down to the next heading of level 3 or less)."""
    out, cur = [], None
    for line in doc.split("\n"):
        lv = _level(line)
        if lv and lv <= 3:
            cur = (line.strip(), []) if lv == 3 else None
            if cur:
                out.append(cur)
        elif cur:
            cur[1].append(line)
    return out


def issue_items(doc: str) -> dict[str, str]:
    """{"owner/repo#N": heading line} for every item with an Issue field. The first item wins a ref."""
    found: dict[str, str] = {}
    for head, body in _items(doc):
        for line in body:
            m = ISSUE_FIELD.match(line.strip()) if not line.startswith((" ", "\t")) else None
            if m:
                found.setdefault(m.group(1), head)
                break
    return found


def item_section(doc: str, heading: str) -> str:
    """The item's heading and everything under it, down to the next heading of its level or above."""
    lines = doc.split("\n")
    want = heading.strip()
    for i, line in enumerate(lines):
        if line.strip() == want and _level(line):
            lv, j = _level(line), i + 1
            while j < len(lines) and not (_level(lines[j]) and _level(lines[j]) <= lv):
                j += 1
            return "\n".join(lines[i:j]).strip("\n")
    return ""


def keep_issue_fields(old: str, new: str) -> str:
    """Put back an Issue field Claude dropped from an item it kept (renamed, renumbered or rewritten).

    The item is found by its heading, numbering aside; failing that, if there are as many items as
    before, by its place among them, since it was probably renamed."""
    have = issue_items(new)
    old_heads = [h for h, _ in _items(old)]
    for ref, head in issue_items(old).items():
        if ref in have:
            continue
        found = find_heading(new, head)
        new_heads = [h for h, _ in _items(new)]
        if not found and len(new_heads) == len(old_heads):
            candidate = new_heads[old_heads.index(head)]
            found = candidate if not any(h == candidate for h in issue_items(new).values()) else None
        if not found:
            continue  # the item itself is gone
        lines = new.split("\n")
        i = next(k for k, l in enumerate(lines) if l.strip() == found)
        new = "\n".join(lines[:i + 1] + [f"- **Issue**: {ref}"] + lines[i + 1:])
    return new


def _section_lines(doc: str, title: str) -> list[str]:
    """The lines of a `## title` section, without its ### sub-items."""
    out, inside = [], False
    for line in doc.split("\n"):
        lv = _level(line)
        if lv == 2:
            inside = line.strip().lstrip("#").strip().lower() == title.lower()
            continue
        if inside and lv and lv <= 3:
            inside = False if lv <= 2 else "sub"
        if inside is True and line.strip():
            out.append(line)
    return out


def _loose(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*_`]", "", text)).strip().lower()


def _title(heading: str) -> str:
    return re.sub(r"^\d+[.)]\s*", "", heading.strip().lstrip("#").strip())


def parked_for(doc: str, heading: str, ref: str | None = None) -> list[str]:
    """Bullets under ## Parked that belong to this item: they name its Issue ref, or its whole title.
    A short title ("Data") could turn up in anything, so it has to be the ref then."""
    title = _loose(_title(heading))
    title = title if len(title) >= 12 else ""
    out = []
    for line in _section_lines(doc, "Parked"):
        if not re.match(r"^[-*]\s", line):
            continue
        if (ref and ref in line) or (title and re.search(rf"(?<!\w){re.escape(title)}(?!\w)", _loose(line))):
            out.append(re.sub(rf"\(?{re.escape(ref)}\)?:?\s*", "", line.strip(), count=1) if ref else line.strip())
    return out


def _parse(section: str) -> dict:
    """An item's parts: fields, open questions, look-ups and other notes."""
    lines = section.split("\n")
    head = lines[0] if lines and _level(lines[0]) else ""
    p = {"title": _title(head) if head else "", "fields": [], "notes": [], "questions": [], "lookups": [], "other": []}
    where = "body"
    for line in lines[1:] if head else lines:
        lv = _level(line)
        if lv:
            text = line.strip().lstrip("#").strip()
            where = "questions" if text.lower() == "open questions" else "other"
            if where == "other":
                p["other"].append(f"**{text}**")
            continue
        if not line.strip():
            continue
        if (m := _LOOKUP.match(line)):
            p["lookups"].append(m.group(1))
            continue
        indented = line.startswith((" ", "\t"))
        if where == "questions":
            q = _QUESTION.match(line.strip()) if not indented else None
            if q:
                text = q.group(3)
                d = _DISPUTED.search(text)
                p["questions"].append({"label": q.group(1).strip(), "key": _loose(q.group(2) or q.group(1)),
                                       "text": _DISPUTED.sub("", text), "disputed": d.group(1) if d else "", "line": line.strip()})
            elif p["questions"] and indented:
                p["questions"][-1]["line"] += "\n" + line.rstrip()
            else:
                p["other"].append(line.rstrip())
        elif where == "other":
            p["other"].append(line.rstrip())
        elif not indented and (m := _FIELD.match(line.strip())):
            if m.group(1).strip().lower() != "issue":
                p["fields"].append((m.group(1).strip(), m.group(2), line.strip()))
        elif not indented and ISSUE_FIELD.match(line.strip()):
            continue
        else:
            p["notes"].append(line.rstrip())
    return p


def _norm(section: str) -> str:
    lines = (section or "").strip().split("\n")
    if lines:
        lines[0] = _NUMBER.sub(r"\1", lines[0].strip())
    return "\n".join(re.sub(r"\s+", " ", l).strip() for l in lines if l.strip())


def changed(snapshot: str | None, section: str) -> bool:
    """Whether there is anything new to post: never posted, or the item differs from what was posted (numbering aside)."""
    return snapshot is None or _norm(snapshot) != _norm(section)


def changed_since(snapshot: str, section: str, parked: list[str] | tuple = ()) -> list[str]:
    """What changed in an item since it was last posted, as lines for "Since the last update". Empty: nothing new.

    `parked` is the item's lines in ## Parked, so a question that went there reads as parked, not answered."""
    if not changed(snapshot, section):
        return []
    old, new = _parse(snapshot), _parse(section)
    out = []
    if _loose(old["title"]) != _loose(new["title"]):
        out.append(f"Renamed to **{new['title']}**")
    was = {n.lower(): v for n, v, _ in old["fields"]}
    now = {n.lower() for n, _, _ in new["fields"]}
    for name, value, _ in new["fields"]:
        before = was.get(name.lower())
        if before == value:
            continue
        if value in UNSET:
            out.append(f"Cleared **{name}**" if before not in (None, *UNSET) else f"Added **{name}**")
        elif before in (None, *UNSET):
            out.append(f"Filled **{name}**: {value}")
        else:
            out.append(f"Changed **{name}**: {value}")
    out += [f"Removed **{n}**" for n, _, _ in old["fields"] if n.lower() not in now]
    parked_keys = {_loose(m.group(2) or m.group(1)) for l in parked if (m := _QUESTION.match(l.strip()))}
    old_q = {q["key"]: q for q in old["questions"]}
    new_q = {q["key"]: q for q in new["questions"]}
    for key, q in old_q.items():
        if key not in new_q:
            out.append(f"{'Parked' if key in parked_keys else 'Answered'} **{q['label']}**")
    for key, q in new_q.items():
        before = old_q.get(key)
        if before is None:
            out.append(f"Added **{q['label']}**")
        elif q["disputed"] and not before["disputed"]:
            out.append(f"Disputed **{q['label']}**: {q['disputed']}")
        elif before["disputed"] and not q["disputed"]:
            out.append(f"Settled the dispute on **{q['label']}**")
        elif _loose(before["text"]) != _loose(q["text"]) or _loose(before["disputed"]) != _loose(q["disputed"]):
            out.append(f"Reworded **{q['label']}**")
    out += [f"Look up: {l}" for l in new["lookups"] if l not in old["lookups"]]
    if [_loose(n) for n in old["notes"] + old["other"]] != [_loose(n) for n in new["notes"] + new["other"]]:
        out.append("Updated the notes")
    return out or ["Edited the item"]


def readiness(section: str) -> str | None:
    """A note for the top of a preview when the item isn't ready yet, e.g. "Status: being grilled, 2 open questions"."""
    p = _parse(section)
    status = next((v for n, v, _ in p["fields"] if n.lower() == "status"), "") or "?"
    n = len(p["questions"])
    if status.lower().startswith("ready") and not n:
        return None
    return f"Status: {status}" + (f", {n} open question{'s' * (n != 1)}" if n else "")


def close_blockers(section: str) -> list[str]:
    """Why an item can't be closed yet, without asking Claude: open questions, or fields still "?". Empty: Claude may check it."""
    p = _parse(section)
    out = [f"{q['label']} is still open" + (f" (disputed: {q['disputed']})" if q["disputed"] else "") for q in p["questions"]]
    out += [f"**{name}** isn't filled in" for name, value, _ in p["fields"] if value in UNSET and name.lower() != "status"]
    if any(name.lower() == "status" and value.lower().startswith("parked") for name, value, _ in p["fields"]):
        out.append("the item is parked")
    return out


def asked_questions(docs: list[str], ref: str) -> list[str]:
    """Every open question the item had in any version of the document, oldest first, each in its latest wording."""
    seen: dict[str, str] = {}
    for doc in docs:
        head = issue_items(doc).get(ref)
        for q in _parse(item_section(doc, head)).get("questions", []) if head else []:
            seen.pop(q["key"], None)
            seen[q["key"]] = q["line"]
    return list(seen.values())


def next_label(asked: list[str]) -> str:
    """The label for a new question: one past the highest Q number the item was ever asked."""
    nums = [int(m.group(1)) for line in asked if (m := re.match(r"^[-*]\s+\*\*Q(\d+)", line))]
    return f"Q{max(nums, default=0) + 1}"


def add_questions(section: str, questions: list[str]) -> str:
    """The item with `questions` (each a question bullet with its indented options) under its Open questions."""
    lines = [l.rstrip() for q in questions for l in q.strip("\n").split("\n") if l.strip()]
    if not lines:
        return section
    body = section.rstrip("\n").split("\n")
    at = next((i for i, l in enumerate(body) if _level(l) and l.strip().lstrip("#").strip().lower() == "open questions"), None)
    if at is None:
        body += ["#### Open questions"] + lines
    else:
        end = next((i for i in range(at + 1, len(body)) if _level(body[i])), len(body))
        while end > at + 1 and not body[end - 1].strip():
            end -= 1
        body[end:end] = lines
    return "\n".join(body) + "\n" * (len(section) - len(section.rstrip("\n")) or 1)


def with_status(text: str, status: str) -> str:
    """`text` (an item section or a comment) with its first Status field set to `status`."""
    return re.sub(r"^([-*]\s+\*\*Status\*\*:\s*).*$", lambda m: m.group(1) + status, text, count=1, flags=re.M)


def closing_body(body: str, resolution: str) -> str:
    """The last comment before closing: Status ready, and Claude's one-line resolution at the top (after the disclaimer)."""
    body = with_status(body, "ready")
    line = f"**Resolved**: {resolution.strip()}" if resolution.strip() else ""
    if not line:
        return body
    if body.startswith(DISCLAIMER):
        return f"{DISCLAIMER}\n\n{line}{body[len(DISCLAIMER):]}"
    return f"{line}\n\n{body}"


def _terms(doc: str, section: str) -> list[str]:
    """## Shared terms entries whose term the item uses."""
    out = []
    for line in _section_lines(doc, "Shared terms"):
        m = _TERM.match(line.strip())
        if m and re.search(rf"(?<!\w){re.escape(m.group(1).strip())}(?!\w)", section, re.I):
            out.append(line.strip() if re.match(r"^[-*]\s", line.strip()) else "- " + line.strip())
    return out


def comment_body(doc: str, heading: str, session_title: str, date: str, previous_snapshot: str | None,
                 disclaimer: bool) -> str:
    """The comment posted to an item's issue, built from the document alone."""
    section = item_section(doc, heading)
    ref = next((r for r, h in issue_items(doc).items() if h == heading.strip()), None)
    p = _parse(section)
    parked = parked_for(doc, heading, ref)
    blocks = []
    if disclaimer:
        blocks.append(DISCLAIMER)
    if previous_snapshot is not None:
        since = changed_since(previous_snapshot, section, parked)
        if since:
            blocks.append("**Since the last update**\n" + "\n".join(f"- {l}" for l in since))
    spec = [line for _, _, line in p["fields"]] + p["notes"]
    if spec:
        blocks.append("\n".join(spec))
    if p["questions"]:
        blocks.append("**Open questions**\n" + "\n".join(q["line"] for q in p["questions"]))
    if p["other"]:
        blocks.append("\n".join(p["other"]))
    if parked:
        blocks.append("**Parked**\n" + "\n".join(parked))
    terms = _terms(doc, section)
    if terms:
        blocks.append("**Shared terms**\n" + "\n".join(terms))
    if p["lookups"]:
        blocks.append("**Look up**\n" + "\n".join(f"- {l}" for l in p["lookups"]))
    blocks.append(f"---\n_{session_title}, {date}_")
    return "\n\n".join(blocks) + "\n"


def offer(doc: str, posts: dict[str, dict]) -> list[str]:
    """The issue items to offer in "Post updates to issues": those changed since their last post, in document order."""
    return [ref for ref, head in issue_items(doc).items()
            if changed((posts.get(ref) or {}).get("snapshot"), item_section(doc, head))]
