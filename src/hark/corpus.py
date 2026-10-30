"""Collections: folders of your own documents (a git repo, a project folder, notes) that Claude can search.

hark keeps a plain-text copy of each collection in its cache. Text files are copied as
they are; Word, PowerPoint and PDF files are converted to text, with ".txt" added to the
name. Claude searches only that copy, with read-only tools, so it never touches the
originals or anything outside them. The copy is brought up to date before each search,
which only re-reads files that changed.

A collection can be a folder you keep yourself, or a GitHub repo that hark clones and
owns (a shallow clone, in ~/.local/share/hark/repos). hark fetches its own clones before a
search, at most every few minutes; it never pulls into a folder you work in. Saved
collections live in hark's config file, which is re-read whenever it changes.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree

log = logging.getLogger("hark")

MAX_FILES = 5000
MAX_TEXT = 2_000_000  # bytes kept from any one file
MAX_ZIP_PART = 50_000_000  # uncompressed size of one part of a Word or PowerPoint file
SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", ".tox", ".mypy_cache",
             ".pytest_cache", ".ruff_cache", ".next", ".cache", "dist", "build", "site-packages"}
PULL_EVERY = 300  # seconds between fetches of a hark-owned clone
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def cache_root() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "hark" / "corpora"


def repos_root() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "hark" / "repos"


GITHUB = re.compile(r"(?:https://github\.com/|git@github\.com:|github\.com/)?([\w.-]+)/([\w.-]+?)(?:\.git)?/?")


def github_repo(spec: str) -> str | None:
    """"owner/repo" from a GitHub link or "owner/repo", unless it names a folder that exists."""
    spec = spec.strip()
    if not spec or spec.startswith(("/", "~", ".")) or os.path.isdir(os.path.expanduser(spec)):
        return None
    m = GITHUB.fullmatch(spec)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def _git_env() -> dict:
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0"}  # fail rather than wait for a password nobody will type


def git(*args: str, timeout: float = 120) -> str:
    try:
        r = subprocess.run(["git", *args], capture_output=True, timeout=timeout, env=_git_env())
    except subprocess.TimeoutExpired:
        raise OSError("git took too long")
    except FileNotFoundError:
        raise OSError("git isn't installed")
    if r.returncode:
        raise OSError((r.stderr.decode("utf-8", "replace").strip().splitlines() or ["git failed"])[-1][:200])
    return r.stdout.decode("utf-8", "replace")


def clone(repo: str, dest: Path) -> None:
    """A shallow clone: just the latest version, which is all a search needs."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        git("clone", "--depth", "1", "-q", f"https://github.com/{repo}.git", str(tmp), timeout=600)
    except OSError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise OSError(f"Couldn't clone {repo}: {e}. Check the name, and that this computer is signed in to GitHub (`gh auth login`).")
    os.replace(tmp, dest)


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "collection"


# -- converting documents to text ------------------------------------------------

def _zip_xml(z: zipfile.ZipFile, name: str):
    info = z.getinfo(name)
    if info.file_size > MAX_ZIP_PART:
        raise ValueError(f"{name} is too large")
    data = z.read(name)
    if b"<!DOCTYPE" in data[:4096] or b"<!ENTITY" in data:  # never in a real Office file: refuse entity tricks
        raise ValueError(f"{name} has a DOCTYPE")
    return ElementTree.fromstring(data)


def docx_text(path: Path) -> str:
    """Paragraphs of a Word document, tables included, one per line."""
    with zipfile.ZipFile(path) as z:
        root = _zip_xml(z, "word/document.xml")
    out = []
    for p in root.iter(f"{W}p"):
        parts = []
        for el in p.iter():
            if el.tag == f"{W}t" and el.text:
                parts.append(el.text)
            elif el.tag == f"{W}tab":
                parts.append("\t")
            elif el.tag in (f"{W}br", f"{W}cr"):
                parts.append("\n")
        line = "".join(parts).strip()
        if line:
            out.append(line)
    return "\n".join(out) + "\n"


def _drawing_text(root) -> list[str]:
    lines = []
    for p in root.iter(f"{A}p"):
        line = "".join(t.text or "" for t in p.iter(f"{A}t")).strip()
        if line:
            lines.append(line)
    return lines


def pptx_text(path: Path) -> str:
    """Each slide's text under a "## Slide N" heading, with its speaker notes."""
    num = lambda n: int(re.search(r"(\d+)\.xml$", n).group(1))
    out = []
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        slides = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=num)
        for i, name in enumerate(slides, 1):
            out.append(f"## Slide {i}")
            out += _drawing_text(_zip_xml(z, name))
            rels = f"ppt/slides/_rels/{name.rsplit('/', 1)[1]}.rels"
            if rels in names:
                for rel in _zip_xml(z, rels):
                    target = rel.get("Target", "")
                    if rel.get("Type", "").endswith("/notesSlide"):
                        note = "ppt/" + target.replace("../", "")
                        if note in names:
                            # Drop the slide-number placeholder that notes pages carry.
                            lines = [l for l in _drawing_text(_zip_xml(z, note)) if not (l.isdigit() or l == "‹#›")]
                            if lines:
                                out.append("Notes: " + " ".join(lines))
            out.append("")
    return "\n".join(out)


def pdf_text(path: Path) -> str | None:
    if shutil.which("pdftotext"):
        r = subprocess.run(["pdftotext", "-enc", "UTF-8", str(path), "-"], capture_output=True, timeout=120)
        return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    return "\n\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)


CONVERTERS = {".docx": docx_text, ".pptx": pptx_text, ".pdf": pdf_text}


def is_text(path: Path) -> bool:
    with open(path, "rb") as f:
        return b"\0" not in f.read(8192)


# -- a collection ----------------------------------------------------------------

class Corpus:
    def __init__(self, name: str, root: str | Path, about: str = "", cache: Path | None = None, repo: str = ""):
        self.name = name
        self.root = Path(root).expanduser().resolve()
        self.about = about
        self.repo = repo  # "owner/repo" when hark owns the clone and keeps it up to date
        self.pulled_at = 0.0
        self.synced_at = 0.0
        self.problem = ""
        key = hashlib.sha1(str(self.root).encode()).hexdigest()[:8]
        base = cache or cache_root()
        self.mirror = base / f"{slug(name)}-{key}"
        self.manifest_path = base / f"{slug(name)}-{key}.json"
        self.files: dict[str, str] = {}  # original path -> its copy's path, both relative
        self.tracked: set[str] = set()  # files in git, which have a web link
        self.web = ""  # https://github.com/owner/repo/blob/branch
        self.skipped = 0
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"Corpus({self.name!r}, {str(self.root)!r})"

    # -- which files -----------------------------------------------------------
    def _git(self, *args: str) -> str | None:
        try:
            r = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None

    def _sources(self) -> list[str]:
        if (self.root / ".git").exists():
            listed = self._git("ls-files", "-z", "--cached", "--others", "--exclude-standard")
            if listed is not None:
                self.tracked = set(filter(None, (self._git("ls-files", "-z") or "").split("\0")))
                self.web = self._web_base()
                return sorted({p for p in listed.split("\0") if p and not (set(Path(p).parts) & SKIP_DIRS)})
        out = []
        for dirpath, dirs, files in os.walk(self.root):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
            for f in sorted(files):
                if not f.startswith("."):
                    out.append(os.path.relpath(os.path.join(dirpath, f), self.root).replace(os.sep, "/"))
        return out

    def _web_base(self) -> str:
        url = (self._git("remote", "get-url", "origin") or "").strip()
        m = re.fullmatch(r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([\w.-]+)/([\w.-]+?)(?:\.git)?/?", url)
        branch = (self._git("rev-parse", "--abbrev-ref", "HEAD") or "").strip()
        if not m or not branch or branch == "HEAD":
            return ""
        return f"https://github.com/{m.group(1)}/{m.group(2)}/blob/{quote(branch)}"

    # -- keeping the copy up to date --------------------------------------------
    def _pull(self, force: bool) -> None:
        """Bring hark's own clone up to date with GitHub (at most every few minutes). Offline, carry on with what's there."""
        if not self.repo or (not force and time.time() - self.pulled_at < PULL_EVERY and self.root.is_dir()):
            return
        self.pulled_at = time.time()
        try:
            if not self.root.is_dir():
                clone(self.repo, self.root)
            else:
                git("-C", str(self.root), "fetch", "--depth", "1", "-q", "origin")
                git("-C", str(self.root), "reset", "-q", "--hard", "@{upstream}")
            self.problem = ""
        except OSError as e:
            self.problem = f"Couldn't update from GitHub ({e})"
            if not self.root.is_dir():
                raise

    def sync(self, force: bool = False) -> dict:
        """Bring the text copy up to date. Safe to call before every search: unchanged files are skipped.
        `force` fetches a hark-owned clone from GitHub even if it did so recently."""
        with self._lock:
            self._pull(force)
            if not self.root.is_dir():
                self.problem = f"The folder is missing: {self.root}"
                raise FileNotFoundError(f"The folder for {self.name!r} is missing: {self.root}")
            try:
                old = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                old = {}
            self.mirror.mkdir(parents=True, exist_ok=True)
            new, files, skipped, changed = {}, {}, 0, 0
            sources = self._sources()
            if len(sources) > MAX_FILES:
                log.warning("collection %s has %d files; searching the first %d", self.name, len(sources), MAX_FILES)
                sources = sources[:MAX_FILES]
            for rel in sources:
                src = self.root / rel
                try:
                    st = os.lstat(src)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):  # no symlinks: they could point outside the folder
                    continue
                ext = src.suffix.lower()
                target = rel + ".txt" if ext in CONVERTERS else rel
                stamp = [st.st_mtime_ns, st.st_size, target]
                dest = self.mirror / target
                if old.get(rel) == stamp and dest.exists():
                    new[rel], files[rel] = stamp, target
                    continue
                try:
                    text = self._convert(src, ext)
                except Exception as e:
                    log.info("collection %s: couldn't read %s (%s)", self.name, rel, e)
                    text = None
                if text is None:
                    skipped += 1
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(text[:MAX_TEXT])
                new[rel], files[rel] = stamp, target
                changed += 1
            keep = set(files.values())
            for dirpath, _dirs, names in os.walk(self.mirror, topdown=False):
                for n in names:
                    p = os.path.join(dirpath, n)
                    if os.path.relpath(p, self.mirror).replace(os.sep, "/") not in keep:
                        os.remove(p)
                if dirpath != str(self.mirror) and not os.listdir(dirpath):
                    os.rmdir(dirpath)
            tmp = self.manifest_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(new), encoding="utf-8")
            os.replace(tmp, self.manifest_path)
            self.files, self.skipped, self.synced_at = files, skipped, time.time()
            if not self.repo:
                self.problem = ""
            return {"files": len(files), "changed": changed, "skipped": skipped}

    def _convert(self, src: Path, ext: str) -> bytes | None:
        if ext in CONVERTERS:
            text = CONVERTERS[ext](src)
            return text.encode("utf-8") if text and text.strip() else None
        if src.stat().st_size > MAX_TEXT * 5 or not is_text(src):
            return None
        with open(src, "rb") as f:
            return f.read(MAX_TEXT)

    # -- paths -----------------------------------------------------------------
    def original(self, path: str) -> str | None:
        """The original file for a path Claude gave: in the copy or the original, absolute or relative."""
        p = str(path or "").strip().strip("`'\"")
        for base in (str(self.mirror), str(self.root)):
            if p.startswith(base + "/") or p.startswith(base + os.sep):
                p = p[len(base) + 1:]
        p = p.replace(os.sep, "/")
        while p.startswith("./"):
            p = p[2:]
        if p in self.files:
            return p
        back = {v: k for k, v in self.files.items()}
        return back.get(p)

    def link(self, rel: str) -> str:
        return f"{self.web}/{quote(rel)}" if self.web and rel in self.tracked else ""

    def describe(self) -> dict:
        return {"name": self.name, "about": self.about, "files": len(self.files), "repo": self.repo,
                "where": self.repo or str(self.root), "updated": round(self.synced_at), "problem": self.problem}

    def forget(self) -> None:
        """Remove hark's text copy, and its clone if hark made one."""
        shutil.rmtree(self.mirror, ignore_errors=True)
        self.manifest_path.unlink(missing_ok=True)
        if self.repo and self.root.is_relative_to(repos_root().resolve()):
            shutil.rmtree(self.root, ignore_errors=True)

    # -- tools for the API backends ---------------------------------------------
    TOOLS = [
        {"name": "list_files", "description": "List the files in the collection. Optionally filter with a glob pattern such as '*.md' or 'Transcripts/*'.",
         "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}}}},
        {"name": "search", "description": "Search the text of every file for a regular expression (case-insensitive). "
                                          "Returns matching lines as path:line: text. Optionally limit to files matching a glob.",
         "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}, "glob": {"type": "string"}}, "required": ["pattern"]}},
        {"name": "read", "description": "Read a file as text, with line numbers. Long files come in parts: use offset (first line, from 1) and limit (number of lines).",
         "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}, "limit": {"type": "integer"}},
                          "required": ["path"]}},
    ]

    def _lines(self, rel: str) -> list[str]:
        return (self.mirror / self.files[rel]).read_text(encoding="utf-8", errors="replace").splitlines()

    def run_tool(self, name: str, args: dict) -> str:
        args = args if isinstance(args, dict) else {}
        if name == "list_files":
            pat = str(args.get("pattern") or "*")
            hits = [p for p in self.files if fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(p.rsplit("/", 1)[-1], pat)]
            return "\n".join(hits[:300]) + (f"\n… and {len(hits) - 300} more" if len(hits) > 300 else "") if hits else "No files match."
        if name == "search":
            raw = str(args.get("pattern") or "")
            try:
                rx = re.compile(raw, re.I)
            except re.error:
                rx = re.compile(re.escape(raw), re.I)
            glob = str(args.get("glob") or "")
            out = []
            for rel in self.files:
                if glob and not (fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(rel.rsplit("/", 1)[-1], glob)):
                    continue
                for i, line in enumerate(self._lines(rel), 1):
                    if rx.search(line):
                        out.append(f"{rel}:{i}: {line.strip()[:300]}")
                        if len(out) >= 80:
                            return "\n".join(out) + "\n… more matches; narrow the search."
            return "\n".join(out) or "No matches."
        if name == "read":
            rel = self.original(str(args.get("path") or ""))
            if not rel:
                return "No such file in the collection. Use list_files to see the paths."
            lines = self._lines(rel)
            start = max(1, int(args.get("offset") or 1))
            count = min(600, max(1, int(args.get("limit") or 400)))
            chunk = [f"{n}\t{l[:2000]}" for n, l in enumerate(lines[start - 1:start - 1 + count], start)]
            text = "\n".join(chunk)[:60000]
            end = start + len(chunk) - 1
            if end < len(lines):
                text += f"\n… {len(lines) - end} more lines (read with offset={end + 1})"
            return text or "(empty)"
        return f"Unknown tool {name!r}"


def parse_spec(spec: str) -> tuple[str, str]:
    """`NAME=PATH` or just `PATH` (named after its folder)."""
    name, sep, path = spec.partition("=")
    if not sep or os.path.sep in name or not name.strip():
        path, name = spec, ""
    path = os.path.expanduser(path.strip())
    return (name.strip() or Path(path).resolve().name), path


# -- the saved list ------------------------------------------------------------------

def read_saved(config_file: Path) -> list[dict]:
    """Collections saved in hark's config file: [{"name", "path", "about", "repo"}]."""
    try:
        data = json.loads(Path(config_file).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for c in (data.get("corpora") if isinstance(data, dict) else None) or []:
        if isinstance(c, dict) and isinstance(c.get("name"), str) and isinstance(c.get("path"), str):
            out.append({"name": c["name"], "path": c["path"], "about": str(c.get("about") or ""), "repo": str(c.get("repo") or "")})
    return out


def make_spec(spec: str, about: str = "", local: bool = True) -> dict:
    """A collection to save, from a folder or a GitHub repo ("owner/repo" or a link). Clones a repo first."""
    name, path = parse_spec(spec)
    repo = github_repo(path)
    if repo:
        name = name if "=" in spec else repo.split("/")[1]
        dest = repos_root() / slug(repo.replace("/", "-"))
        if not dest.is_dir():
            clone(repo, dest)
        return {"name": name, "path": str(dest), "about": about.strip(), "repo": repo}
    if not local:
        raise ValueError("Give a GitHub repo, like owner/repo or its github.com link")
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        raise ValueError(f"No such folder: {path}")
    return {"name": name, "path": path, "about": about.strip(), "repo": ""}


class Library:
    """The collections hark can search: those saved in the config file, re-read whenever the file
    changes (so a collection added while hark runs appears straight away), plus any for this run only."""

    def __init__(self, config_file: Path | None = None, extra: list[dict] | None = None, fixed: list[Corpus] | None = None,
                 cache: Path | None = None):
        self.config_file = config_file
        self.extra = list(extra or [])
        self.cache = cache
        self._all: dict[str, Corpus] = {c.name: c for c in fixed or []}
        self._stamp: tuple | None = None
        self.saved: set[str] = set()

    def current(self) -> dict[str, Corpus]:
        if self.config_file is None:
            return self._all
        try:
            st = Path(self.config_file).stat()
            stamp = (st.st_mtime_ns, st.st_size)  # size too: two quick writes can share a timestamp
        except OSError:
            stamp = (0, 0)
        if stamp != self._stamp:
            self._stamp = stamp
            saved = read_saved(self.config_file)
            self.saved = {c["name"] for c in saved}
            specs = {c["name"]: c for c in saved + self.extra}
            new = {}
            for name, sp in specs.items():
                old = self._all.get(name)
                root = Path(sp["path"]).expanduser().resolve()
                if old and old.root == root and old.repo == sp.get("repo", ""):
                    old.about = sp.get("about", "")
                    new[name] = old  # keep what it knows (files, last fetch)
                else:
                    new[name] = Corpus(name, root, sp.get("about", ""), cache=self.cache, repo=sp.get("repo", ""))
            self._all = new
        return self._all
