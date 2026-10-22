"""Ask Claude for document edits, through the local Claude Code CLI.

This uses `claude -p` (headless Claude Code), which authenticates with your
Claude subscription login, so no API key or per-token billing is involved.
Each update is one stateless call: brief + current document + new transcript
in, a list of anchored edits out (validated against a JSON schema).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time

SYSTEM = """You are the scribe for a live document that people glance at while they talk. You receive:
- <brief>: what the document is for and how to organise it. Follow it.
- <document>: the current Markdown, which the user may have edited by hand. It is authoritative. Never undo, re-word or reorder the user's hand edits unless the conversation asks for that.
- <earlier_transcript>: recent speech already reflected in the document (context only).
- <new_transcript>: speech since your last update. Bring the document up to date with it.
- <typed_instructions>, if present: instructions the user typed to you. Carry them out.

The transcript comes from automatic speech recognition, so read it charitably: fix obvious mis-hearings using context. Speaker labels come from automatic voice matching and can be wrong or split one person into two.

Spoken instructions about the document ("put that under X", "no, that's wrong", "scratch that", "change the title to...") are edits for you to make, not content to record.

Write tersely: short bullets, no filler, no meta-commentary, no timestamps. It is fine to return no edits when nothing substantive was said.

Return edits as `ops`, applied in order:
- {"op":"append_to_section","heading":"## Actions","text":"- [ ] **Sam**: send the draft (Fri)"}: add lines at the end of a section. The heading must match an existing heading line exactly; if it does not exist, it is created at the end of the document.
- {"op":"insert_after","find":"<an exact, complete existing line or run of lines>","text":"<new lines>"}
- {"op":"replace","find":"<exact existing text>","text":"<replacement>"}: for corrections, rewrites and moves. `find` must be copied character-for-character from <document> and be unique; prefer whole lines.
- {"op":"delete","find":"<exact existing text>"}
To move something, delete it and insert it in its new place. Keep each `find` short but unambiguous.

Optional `speaker_names`: map an auto-labelled speaker (e.g. "Speaker 2") to a real name only when the transcript makes it near-certain, e.g. someone introduces themselves or is addressed by name and that labelled speaker answers. Otherwise omit it."""

SCHEMA = {
    "type": "object",
    "properties": {
        "ops": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["append_to_section", "insert_after", "replace", "delete"]},
                    "heading": {"type": "string"},
                    "find": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["op"],
            },
        },
        "speaker_names": {"type": "object", "additionalProperties": {"type": "string"}},
    },
    "required": ["ops"],
}


class ScribeError(Exception):
    pass


def build_prompt(brief: str, document: str, speakers: list[str], earlier: list[str], new: list[str], typed: list[str]) -> str:
    parts = [f"<brief>\n{brief.strip()}\n</brief>"]
    if speakers:
        parts.append("<speakers>\n" + "\n".join(f"- {s}" for s in speakers) + "\n</speakers>")
    parts.append(f"<document>\n{document.rstrip()}\n</document>")
    if earlier:
        parts.append("<earlier_transcript>\n" + "\n".join(earlier) + "\n</earlier_transcript>")
    parts.append("<new_transcript>\n" + ("\n".join(new) if new else "(nothing new)") + "\n</new_transcript>")
    if typed:
        parts.append("<typed_instructions>\n" + "\n".join(f"- {t}" for t in typed) + "\n</typed_instructions>")
    parts.append("Return the edit operations.")
    return "\n\n".join(parts)


class Scribe:
    def __init__(self, model: str = "sonnet", effort: str | None = "low", claude_bin: str | None = None):
        self.model = model
        self.effort = effort
        self.bin = claude_bin or shutil.which("claude")
        self.flags: set[str] = set()
        self.workdir = tempfile.mkdtemp(prefix="hark-claude-")  # empty dir: no project CLAUDE.md
        # Pass the prompt as a file: long multi-line arguments get mangled by Windows .cmd shims.
        self.system_file = os.path.join(self.workdir, "system.txt")
        with open(self.system_file, "w", encoding="utf-8") as f:
            f.write(SYSTEM)
        if self.bin:
            try:
                h = subprocess.run([self.bin, "--help"], capture_output=True, text=True, timeout=30).stdout
                self.flags = {f for f in ("--safe-mode", "--no-session-persistence", "--json-schema", "--tools", "--effort", "--strict-mcp-config") if f in h}
            except Exception:
                pass

    @property
    def available(self) -> bool:
        return bool(self.bin)

    def _cmd(self) -> list[str]:
        cmd = [self.bin, "-p", "--model", self.model, "--output-format", "json", "--system-prompt-file", self.system_file]
        if "--safe-mode" in self.flags:
            cmd.append("--safe-mode")  # no CLAUDE.md, MCP servers, hooks or plugins; login still works
        elif "--strict-mcp-config" in self.flags:
            cmd += ["--strict-mcp-config", "--setting-sources", ""]
        if "--no-session-persistence" in self.flags:
            cmd.append("--no-session-persistence")  # keep these calls out of your /resume list
        if "--tools" in self.flags:
            cmd += ["--tools", ""]  # text in, edits out; no tools
        if "--json-schema" in self.flags:
            cmd += ["--json-schema", json.dumps(SCHEMA)]
        if self.effort and "--effort" in self.flags:
            cmd += ["--effort", self.effort]
        return cmd

    async def edit(self, prompt: str, timeout: float = 120) -> tuple[dict, float]:
        if not self.bin:
            raise ScribeError("Claude Code CLI not found. Install it and run `claude` once to log in.")
        env = dict(os.environ)
        # An API key in the environment would make the CLI bill the API instead of your subscription.
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        if "--json-schema" not in self.flags:
            prompt += '\n\nReply with only a JSON object: {"ops": [...], "speaker_names": {...}}'
        t = time.time()
        proc = await asyncio.create_subprocess_exec(
            *self._cmd(), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=self.workdir, env=env)
        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise ScribeError("Claude took too long to respond")
        took = time.time() - t
        try:
            res = json.loads(out.decode() or "{}")
        except json.JSONDecodeError:
            raise ScribeError((err.decode() or out.decode()).strip()[:300] or f"claude exited with {proc.returncode}")
        if res.get("is_error") or proc.returncode:
            raise ScribeError(str(res.get("result") or err.decode().strip() or "Claude returned an error")[:300])
        data = res.get("structured_output")
        if data is None:  # older CLI without --json-schema: parse JSON from the text
            txt = (res.get("result") or "").strip()
            s, e = txt.find("{"), txt.rfind("}")
            try:
                data = json.loads(txt[s:e + 1])
            except Exception:
                raise ScribeError("Could not read Claude's edits")
        return data, took
