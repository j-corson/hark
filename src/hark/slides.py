"""Slides: add a research card, or what the room screen shows, to a PowerPoint deck that is
open on this computer (Windows only).

hark never writes the .pptx file itself. It asks the running PowerPoint, through COM from
Windows PowerShell, to append a slide to the open presentation. The slide stays unsaved
until you save, so PowerPoint's own Undo covers a slide you didn't want. A deck that isn't
open is left alone.

A slide is built here as plain data, {title, body: [{text, url?, level}], notes}, and
handed to the script as JSON on stdin, never written into the script's text.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .docops import HEADING, _loose, _section_end, find_heading, headings_above, in_working_notes
from .research import clean, clean_url, clip

BODY_CHARS = 400  # the answer on a slide; the rest goes to the speaker notes
MAX_FINDINGS = 6
MAX_LINES = 8  # bullets from a section; the rest goes to the speaker notes
LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
BULLET = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+(.*)$")
BOX = re.compile(r"^\[([ xX])\]\s+")
QUESTION = re.compile(r"^\*\*(Q\d+)\.?\s*([^*]*?):?\*\*:?\s*(.*)$")


def _line(text: str, level: int = 0, url: str = "") -> dict:
    out = {"text": text, "level": level}
    if url:
        out["url"] = url
    return out


def job_slide(job: dict) -> dict:
    """A slide for a finished research card: its label, the answer (shortened) and the findings, linked."""
    answer = clean(job.get("answer"), 6000)
    short = clip(answer, BODY_CHARS)
    body = [_line(short)] if short else []
    notes = [answer] if short != answer else []
    for it in (job.get("items") or [])[:MAX_FINDINGS]:
        title = clean(it.get("title"), 250)
        if not title:
            continue
        url = clean_url(it.get("url"))
        path = clean(it.get("path"), 200)
        body.append(_line(title if url or not path else f"{title} ({path})", url=url))
        note = clean(it.get("note"), 250)
        if note:
            notes.append(f"{title}: {note}")
    notes += [f"Asked: {clean(job.get('q'), 500)}", "from hark research"]
    return {"title": clean(job.get("label") or job.get("q"), 120), "body": body, "notes": "\n".join(notes)}


def _plain(text: str) -> tuple[str, str]:
    """Markdown to slide text: no emphasis, links as their words. The link, if there is just one, comes back too."""
    links = LINK.findall(text)
    box = BOX.match(text)
    if box:
        text = ("✓ " if box.group(1) != " " else "") + text[box.end():]
    text = re.sub(r"[*`]", "", LINK.sub(r"\1", text))
    return clean(text, 300), clean_url(links[0][1]) if len(links) == 1 else ""


def _section(doc: str, heading: str) -> tuple[list[str], int, int]:
    """(lines, start, end) of the section under this exact heading line."""
    lines = doc.split("\n")
    start = next(i for i, l in enumerate(lines) if l.strip() == heading)
    return lines, start, _section_end(lines, start)


def _bullets(lines: list[str], level: int) -> list[dict]:
    """The section's own lines as bullets, with its direct subsections as indented bullets."""
    out, child = [], False
    for line in lines:
        m = HEADING.match(line.strip())
        if m:
            child = True
            if len(m.group(1)) == level + 1:
                out.append(_line(_plain(m.group(2))[0], 1))
            continue
        if child or not line.strip():
            continue
        b = BULLET.match(line)
        text, url = _plain(b.group(2) if b else line.strip())
        if text:
            out.append(_line(text, min(len(b.group(1)) // 2, 2) if b else 0, url))
    return out


def _fit(title: str, body: list[dict], notes: list[str]) -> dict:
    """Bullets past what fits on a slide move to the speaker notes."""
    over = ["  " * b["level"] + "- " + b["text"] for b in body[MAX_LINES:]]
    return {"title": title, "body": body[:MAX_LINES], "notes": "\n".join(over + notes + ["from hark (room screen)"])}


def _grill_slide(lines: list[str], title: str) -> dict:
    """A grilling item: its first open question and the suggested answer, or else its fields."""
    fields, qs, in_q = [], [], False
    for line in lines:
        m = HEADING.match(line.strip())
        if m:
            in_q = "open questions" in _loose(m.group(2))
            continue
        b = BULLET.match(line)
        if not b:
            continue
        if in_q:
            q = QUESTION.match(b.group(2))
            text = q.group(3) if q else b.group(2)
            ask, sugg = (re.split(r"➡️|=>|→", text, maxsplit=1) + [""])[:2]
            qs.append((_plain(ask)[0], _plain(re.sub(r"^\s*suggested:?\s*", "", sugg, flags=re.I))[0]))
        elif not re.match(r"^\*\*status", b.group(2), re.I):
            fields.append(_line(_plain(b.group(2))[0]))
    if not qs:
        return _fit(title, fields, [])
    (ask, sugg), more = qs[0], qs[1:]
    notes = [f"Item: {title}"] + [f"Also open: {q}" for q, _ in more]
    return _fit(ask, [_line(f"Suggested: {sugg}")] if sugg else [], notes)


def section_slide(doc: str, heading: str | None, mode: str) -> dict | None:
    """A slide for a section, as the room screen shows it. None for a missing section, or one in Working notes."""
    found = find_heading(doc, heading)
    if not found or in_working_notes(doc, found):
        return None  # Working notes are still being weighed: not for the room
    lines, start, end = _section(doc, found)
    m = HEADING.match(found)
    level, key, parent = len(m.group(1)), _loose(m.group(2)), headings_above(lines, start)[:1]
    title = _plain(m.group(2))[0]
    inside = lines[start + 1:end]
    if mode == "grill" and level == 3 and parent == ["items"]:
        return _grill_slide(inside, re.sub(r"^\d+[.)]\s*", "", title))
    body = _bullets(inside, level)
    decisions = find_heading(doc, "## Decisions")
    if mode == "meeting" and level == 3 and parent == ["discussion"] and decisions:
        # The meeting's decisions that name this topic.
        dl, ds, de = _section(doc, decisions)
        body += [_line(f"Decided: {b['text']}", 0, b.get("url", "")) for b in _bullets(dl[ds + 1:de], 2)
                 if b["level"] == 0 and key in _loose(b["text"])]
    return _fit(title, body, [])


def _norm_path(p: str) -> str:
    return re.sub(r"/+", "/", str(p or "").replace("\\", "/")).rstrip("/").lower()


def match_deck(linked: str, open_fullnames: list[str], onedrive_root: str | None = None) -> str | None:
    """Which open presentation (by PowerPoint's FullName) is the linked deck, if any.

    A deck in OneDrive shows its web address as its FullName, so it matches by its place
    inside OneDrive (folders and name), never by its file name alone."""
    want = _norm_path(linked)
    root = _norm_path(onedrive_root) if onedrive_root else ""
    tail = want[len(root) + 1:] if root and want.startswith(root + "/") else ""
    for name in open_fullnames:
        if _norm_path(name) == want:
            return name
        if tail and re.match(r"https?://", name, re.I):
            # Personal OneDrive: /<drive id>/<path>; work or school: /personal/<user>/Documents/<path>
            path = _norm_path(unquote(urlsplit(name).path))
            if re.sub(r"^/(?:personal/[^/]+/documents|[^/]+)/", "", path) == tail:
                return name
    return None


# -- talking to PowerPoint -------------------------------------------------------------

NOT_OPEN = "deck not open"
NOT_FOUND = "linked deck not found"
BUSY = "couldn't add to slides (PowerPoint busy)"
TIMEOUT = 20
BUSY_WAIT = 3

# Every script reads its input as JSON on stdin and writes one line of JSON.
_HEAD = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$in = [Console]::In.ReadToEnd() | ConvertFrom-Json
function Say($o) { [Console]::Out.WriteLine(($o | ConvertTo-Json -Compress -Depth 6)) }
"""

_APP = _HEAD + r"""
try { $app = [Runtime.InteropServices.Marshal]::GetActiveObject('PowerPoint.Application') }
catch { Say @{ error = 'not_running' }; exit 0 }
try {
%s
} catch {
  if ("$($_.Exception)" -match '80010001|RPC_E_CALL_REJECTED') { Say @{ error = 'busy' } }
  else { Say @{ error = "$($_.Exception.Message)" } }
}
"""

LIST = _APP % r"""
  $decks = @()
  foreach ($p in $app.Presentations) { $decks += @{ name = $p.FullName; readonly = ($p.ReadOnly -eq -1) } }
  Say @{ decks = $decks }
"""

# Appended at the end, never selected or shown, so a running slide show doesn't move.
# Layout: the last slide's, if it has a title and a body. In an empty deck, or one with just a
# title slide, ppLayoutText. Otherwise any of the deck's layouts with a title and a body.
# Failing all of those, a blank slide with text boxes.
ADD = _APP % r"""
  $pres = $null
  foreach ($p in $app.Presentations) { if ($p.FullName -eq $in.deck) { $pres = $p; break } }
  if (-not $pres -or $pres.ReadOnly -eq -1) { Say @{ error = 'not_open' }; exit 0 }
  function Ph($shapes, $kinds) {
    foreach ($sh in $shapes.Placeholders) { if ($kinds -contains [int]$sh.PlaceholderFormat.Type) { return $sh } }
    return $null
  }
  function TitleAndBody($shapes) { return ((Ph $shapes @(1, 3)) -and (Ph $shapes @(2, 7))) }
  $n = $pres.Slides.Count
  $slide = $null
  if ($n -ge 1) {
    $last = $pres.Slides.Item($n)
    if (TitleAndBody $last.CustomLayout.Shapes) { $slide = $pres.Slides.AddSlide($n + 1, $last.CustomLayout) }
  }
  if (-not $slide -and ($n -eq 0 -or ($n -eq 1 -and @(1, 11) -contains [int]$last.Layout))) {
    $slide = $pres.Slides.Add($n + 1, 2)
    if (-not (TitleAndBody $slide.Shapes)) { $slide.Delete(); $slide = $null }
  }
  if (-not $slide) {
    foreach ($l in $pres.SlideMaster.CustomLayouts) { if (TitleAndBody $l.Shapes) { $slide = $pres.Slides.AddSlide($n + 1, $l); break } }
  }
  if (-not $slide) { $slide = $pres.Slides.Add($n + 1, 12) }
  $w = $pres.PageSetup.SlideWidth; $h = $pres.PageSetup.SlideHeight
  $s = $in.slide
  $title = Ph $slide.Shapes @(1, 3)
  if (-not $title) { $title = $slide.Shapes.AddTextbox(1, 40, 30, $w - 80, 70) }
  $title.TextFrame.TextRange.Text = $s.title
  $lines = @($s.body)
  $body = Ph $slide.Shapes @(2, 7)
  if ($lines.Count -eq 0) { if ($body) { $body.Delete() } }
  else {
    if (-not $body) { $body = $slide.Shapes.AddTextbox(1, 40, 110, $w - 80, $h - 150) }
    $tr = $body.TextFrame.TextRange
    $tr.Text = ($lines | ForEach-Object { $_.text }) -join "`r"
    for ($i = 0; $i -lt $lines.Count; $i++) {
      $para = $tr.Paragraphs($i + 1)
      $para.IndentLevel = [int]$lines[$i].level + 1
      if ($lines[$i].url) { $para.TrimText().ActionSettings(1).Hyperlink.Address = $lines[$i].url }
    }
  }
  if ($s.notes) {
    $np = Ph $slide.NotesPage.Shapes @(2)
    if ($np) { $np.TextFrame.TextRange.Text = $s.notes }
  }
  Say @{ ok = $true; slide = $slide.SlideIndex }
"""

# A native file dialog, in front of the browser, starting in the linked deck's folder.
PICK = _HEAD + r"""
Add-Type -AssemblyName System.Windows.Forms
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true; $owner.ShowInTaskbar = $false; $owner.Opacity = 0
$owner.StartPosition = 'CenterScreen'; $owner.Size = New-Object System.Drawing.Size(1, 1)
$owner.Show(); $owner.Activate()
try {
  $f = New-Object System.Windows.Forms.OpenFileDialog
  $f.Title = 'Link a PowerPoint deck to hark'
  $f.Filter = 'PowerPoint presentations (*.pptx)|*.pptx'
  if ($in.dir -and (Test-Path -LiteralPath $in.dir)) { $f.InitialDirectory = $in.dir }
  if ($f.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) { Say @{ path = $f.FileName } } else { Say @{ path = $null } }
} finally { $owner.Close() }
"""

SCRIPTS = {"list": LIST, "add": ADD, "pick": PICK}


class SlidesError(Exception):
    pass


def on_windows() -> bool:
    """Slides need PowerPoint on Windows: elsewhere hark shows no slide controls."""
    return sys.platform == "win32"


async def powershell(script: str, payload: dict, timeout: float = TIMEOUT, sta: bool = False) -> dict:
    """Run one of SCRIPTS in Windows PowerShell 5.1 (which can reach PowerPoint over COM), with `payload` on stdin."""
    code = base64.b64encode(SCRIPTS[script].encode("utf-16-le")).decode("ascii")
    args = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"] + (["-STA"] if sta else [])
    try:
        proc = await asyncio.create_subprocess_exec(*args, "-EncodedCommand", code, stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError as e:
        raise SlidesError(f"couldn't start PowerShell ({e})")
    try:
        # ASCII JSON (non-ASCII as \u escapes) survives whatever code page PowerShell reads stdin with.
        out, err = await asyncio.wait_for(proc.communicate(json.dumps(payload).encode("ascii")), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError) as e:
        proc.kill()
        await proc.wait()
        if isinstance(e, asyncio.CancelledError):
            raise
        raise SlidesError("PowerPoint didn't answer in time")
    for line in reversed(out.decode("utf-8", "replace").splitlines()):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise SlidesError(clean(err.decode("utf-8", "replace"), 200) or "PowerShell gave no answer")


class Deck:
    """The linked deck, as PowerPoint has it open. `run` runs a script (stubbed in tests)."""

    def __init__(self, run=None, available: bool | None = None, onedrive: str | None = None, sleep=asyncio.sleep):
        self.run = run or powershell
        self.available = on_windows() if available is None else available
        self.onedrive = onedrive or os.environ.get("OneDrive")  # the local OneDrive folder, for decks opened from it
        self.sleep = sleep

    async def add(self, linked: str | None, slide: dict) -> str | None:
        """Append `slide` to the linked deck if it's open. None if added, otherwise why not, in a few words."""
        if not linked or not Path(linked).is_file():
            return NOT_FOUND
        try:
            note = await self._add(linked, slide)
            if note == "busy":
                await self.sleep(BUSY_WAIT)
                note = await self._add(linked, slide)
        except SlidesError as e:
            return f"couldn't add to slides: {e}"
        return BUSY if note == "busy" else note

    async def _add(self, linked: str, slide: dict) -> str | None:
        found = await self.run("list", {}, TIMEOUT)
        if found.get("error"):
            return self._problem(found)
        decks = found.get("decks") or []
        decks = [decks] if isinstance(decks, dict) else decks  # PowerShell sends a lone item bare
        names = [d.get("name") for d in decks if isinstance(d, dict) and d.get("name") and not d.get("readonly")]
        name = match_deck(linked, names, self.onedrive)
        if not name:
            return NOT_OPEN
        done = await self.run("add", {"deck": name, "slide": slide}, TIMEOUT)
        return None if done.get("ok") else self._problem(done)

    @staticmethod
    def _problem(answer: dict) -> str:
        err = str(answer.get("error") or "no answer")
        if err in ("not_running", "not_open"):
            return NOT_OPEN
        return err if err == "busy" else f"couldn't add to slides: {clean(err, 200)}"

    async def pick(self, start: str | None) -> str | None:
        """Ask for a .pptx with a file dialog on this computer. None if cancelled."""
        folder = str(Path(start).parent) if start else ""
        got = await self.run("pick", {"dir": folder}, 3600, sta=True)
        path = got.get("path")
        return str(path) if isinstance(path, str) and path.strip() else None


# -- what the hub needs to decide -----------------------------------------------------------

POPUP_KINDS = ("quick", "corpus", "meeting")  # answers that pop up on the room screen
AUTO_KINDS = ("sources", "corpus")  # results added to the deck automatically, if asked
POPUP_GRACE = 30  # "put that on a slide" still means a pop-up this long after it went


def popup_seconds(answer: str) -> float:
    """How long the room screen shows a pop-up answer (as present.js works it out)."""
    words = len(clip(answer or "", 700).split())
    return min(90, max(40, 25 + words * 0.4))
