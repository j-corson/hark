"""Background research: look things up on the web while the conversation goes on.

A request comes from the scribe (someone asked out loud, or in Tell Claude, for
something to be looked up) or straight from the research panel. Each one runs
its own `claude -p` with only WebSearch and WebFetch, and returns a short answer
plus a few sourced findings. Nothing reaches the document until someone adds it.
"""

from __future__ import annotations

import asyncio
import json
import os
import time

from .scribe import Scribe, ScribeError

SYSTEM = """You research questions that come up during a live meeting or writing session. People read your result at a glance while they are still talking, so be brief and concrete.

- Search the web to find and check what you report. Only cite sources you actually found. Never invent a title, author, year, figure or URL; if you could not verify something, say so in its note.
- `answer`: one or two sentences that directly answer the request.
- `items`: at most 5 findings (papers, figures, sources, examples), most useful first.
  - `title`: for a paper, "Authors (Year). Title. Venue." Otherwise a short headline.
  - `note`: why it matters here, in at most 20 words.
  - `url`: the best source link (a DOI link for papers when you have one).
- Stop once you have a good answer; a handful of searches is usually enough.

<document_context> shows the live document, so you can see what the conversation is about. Use it to make the request specific; do not summarise it."""

SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "note": {"type": "string"}, "url": {"type": "string"}},
                "required": ["title", "note"],
            },
        },
    },
    "required": ["answer", "items"],
}

TOOLS = "WebSearch,WebFetch"


class Researcher:
    """Runs research calls with the same Claude CLI as the scribe, but with web tools on."""

    def __init__(self, scribe: Scribe, model: str | None = None, effort: str = "medium", timeout: float = 360):
        self.scribe = scribe
        self.model = model or scribe.model
        self.effort = effort
        self.timeout = timeout
        self.system_file = os.path.join(scribe.workdir, "research-system.txt")
        with open(self.system_file, "w", encoding="utf-8") as f:
            f.write(SYSTEM)

    @property
    def available(self) -> bool:
        return self.scribe.available and "--tools" in self.scribe.flags

    def _cmd(self) -> list[str]:
        flags = self.scribe.flags
        cmd = [self.scribe.bin, "-p", "--model", self.model, "--output-format", "json",
               "--system-prompt-file", self.system_file, "--tools", TOOLS, "--allowedTools", TOOLS]
        if "--safe-mode" in flags:
            cmd.append("--safe-mode")
        if "--no-session-persistence" in flags:
            cmd.append("--no-session-persistence")
        if "--json-schema" in flags:
            cmd += ["--json-schema", json.dumps(SCHEMA)]
        if self.effort and "--effort" in flags:
            cmd += ["--effort", self.effort]
        return cmd

    async def run(self, question: str, document: str) -> tuple[dict, float]:
        if not self.available:
            raise ScribeError("Research needs a recent Claude Code CLI")
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        prompt = f"<document_context>\n{document[:6000].rstrip()}\n</document_context>\n\n<request>\n{question.strip()}\n</request>"
        if "--json-schema" not in self.scribe.flags:
            prompt += '\n\nReply with only a JSON object: {"answer": "...", "items": [{"title": "...", "note": "...", "url": "..."}]}'
        t = time.time()
        proc = await asyncio.create_subprocess_exec(
            *self._cmd(), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=self.scribe.workdir, env=env)
        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), self.timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as e:
            proc.kill()
            await proc.wait()
            if isinstance(e, asyncio.CancelledError):
                raise
            raise ScribeError("Research took too long")
        took = time.time() - t
        try:
            res = json.loads(out.decode() or "{}")
        except json.JSONDecodeError:
            raise ScribeError((err.decode() or out.decode()).strip()[:300] or f"claude exited with {proc.returncode}")
        if res.get("is_error") or proc.returncode:
            raise ScribeError(str(res.get("result") or err.decode().strip() or "Claude returned an error")[:300])
        data = res.get("structured_output")
        if data is None:
            txt = (res.get("result") or "").strip()
            s, e = txt.find("{"), txt.rfind("}")
            try:
                data = json.loads(txt[s:e + 1])
            except Exception:
                raise ScribeError("Could not read the research result")
        items = []
        for it in (data.get("items") or [])[:6]:
            url = str(it.get("url") or "").strip()
            items.append({"title": str(it.get("title") or "").strip(), "note": str(it.get("note") or "").strip(),
                          "url": url if url.startswith(("http://", "https://")) else "", "added": False})
        return {"answer": str(data.get("answer") or "").strip(), "items": [i for i in items if i["title"]]}, took
