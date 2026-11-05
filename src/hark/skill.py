"""The grilling skill: Matt Pocock's method that grilling sessions run (MIT).

The grill brief (modes.py) is only a wrapper round it: how the skill runs in a room and how
it is written into the document. hark uses the copy installed in Claude Code when there is
one (the mattpocock-skills plugin), so an update to the skill reaches hark too, and its own
copy (grilling_skill.md) otherwise.
"""

from __future__ import annotations

import re
from pathlib import Path

PLUGIN = "mattpocock-skills"
NAME = f"{PLUGIN}:grilling"
_CACHE = Path.home() / ".claude" / "plugins" / "cache" / "mattpocock" / PLUGIN
_OWN = Path(__file__).with_name("grilling_skill.md")
_FRONT = re.compile(r"\A---\n.*?\n---\n", re.S)
_COMMENT = re.compile(r"\A<!--.*?-->\n", re.S)


def _version(p: Path) -> tuple:
    return tuple(int(n) if n.isdigit() else -1 for n in re.split(r"[.-]", p.name))


def plugin_dir(cache: Path = _CACHE) -> Path | None:
    """The newest installed mattpocock-skills plugin that has the grilling skill, if any."""
    try:
        found = [d for d in cache.iterdir() if d.is_dir() and _skill_file(d)]
    except OSError:
        return None
    return max(found, key=_version) if found else None


def _skill_file(plugin: Path) -> Path | None:
    return next(iter(sorted(plugin.glob("skills/**/grilling/SKILL.md"))), None)


def text(cache: Path = _CACHE) -> str:
    """The skill's instructions, without its front matter: the installed copy if there is one, else hark's own."""
    plugin = plugin_dir(cache)
    path = _skill_file(plugin) if plugin else None
    try:
        raw = (path or _OWN).read_text(encoding="utf-8")
    except OSError:
        raw = _OWN.read_text(encoding="utf-8")
    return _COMMENT.sub("", _FRONT.sub("", raw.replace("\r\n", "\n"))).strip()
