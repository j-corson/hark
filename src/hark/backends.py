"""Where hark's language-model calls go.

Every call is the same shape: a system prompt, one user prompt and a JSON schema
in, one JSON object out. Three backends:

- claude-code: the local Claude Code CLI, signed in with your Claude subscription.
  No API key, no per-token billing. The default.
- anthropic: the Anthropic API with your own API key, billed per token.
- openai: any service with an OpenAI-compatible chat API (OpenAI, Gemini,
  OpenRouter, Mistral, a local Ollama or LM Studio, ...), with your own key.

Web research needs Claude's web search, so it works with the first two only.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from urllib.parse import urlparse

PROVIDERS = ("claude-code", "anthropic", "openai")


class ScribeError(Exception):
    pass


def parse_json(text: str) -> dict:
    """The JSON object in a model's reply, tolerating code fences or a sentence around it."""
    text = (text or "").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}")
        try:
            data = json.loads(text[s:e + 1]) if s >= 0 < e else None
        except json.JSONDecodeError:
            data = None
    if not isinstance(data, dict):
        raise ScribeError("Couldn't read the model's reply as JSON")
    return data


def strict_schema(schema: dict) -> dict:
    """The schema with `additionalProperties: false` on every object, as structured outputs require."""
    out = copy.deepcopy(schema)

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(out)
    return out


class Backend:
    kind = ""
    can_research = False

    def __init__(self, model: str):
        self.model = model

    @property
    def available(self) -> bool:
        return True

    @property
    def problem(self) -> str:
        """Why this backend can't be used, for the status line and `hark --check`."""
        return ""

    @property
    def label(self) -> str:
        return self.kind

    async def json(self, system: str, schema: dict, prompt: str, *, effort: str | None = None,
                   timeout: float = 120, hint: str = "") -> tuple[dict, float]:
        raise NotImplementedError

    async def aclose(self) -> None:
        """Close any open connection, before a one-off command's event loop ends."""
        client, self._client = getattr(self, "_client", None), None
        if client is not None:
            await client.close()

    async def research(self, system: str, schema: dict, prompt: str, *, model: str | None = None,
                       effort: str | None = None, timeout: float = 360, hint: str = "") -> tuple[dict, float]:
        raise ScribeError("Web research needs Claude: your Claude subscription or an Anthropic API key")

    can_search_corpus = False

    async def search_corpus(self, system: str, schema: dict, prompt: str, corpus, *, model: str | None = None,
                            effort: str | None = None, timeout: float = 300, hint: str = "") -> tuple[dict, float]:
        raise ScribeError("Searching your documents needs Claude: your Claude subscription or an Anthropic API key")


# -- Claude Code CLI (your subscription) ---------------------------------------

def find_claude() -> str | None:
    """Find the Claude Code CLI, even when started without your shell's PATH (e.g. as a service)."""
    if os.environ.get("HARK_CLAUDE"):
        return os.environ["HARK_CLAUDE"]
    found = shutil.which("claude")
    if found:
        return found
    home = os.path.expanduser("~")
    names = ["claude.exe", "claude.cmd", "claude"] if os.name == "nt" else ["claude"]
    dirs = [
        os.path.join(home, ".local", "bin"),
        os.path.join(home, ".claude", "local"),
        os.path.join(home, ".npm-global", "bin"),
        os.path.join(home, ".bun", "bin"),
        os.path.join(os.environ.get("APPDATA", ""), "npm"),
        "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin",
    ]
    for d in dirs:
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
    return None


class ClaudeCode(Backend):
    """`claude -p` (headless Claude Code), which uses your Claude subscription login."""

    kind = "claude-code"
    RESEARCH_TOOLS = "WebSearch,WebFetch"
    CORPUS_TOOLS = "Read,Grep,Glob"

    def __init__(self, model: str = "sonnet", claude_bin: str | None = None):
        super().__init__(model)
        self.bin = claude_bin or find_claude()
        self.flags: set[str] = set()
        self.workdir = tempfile.mkdtemp(prefix="hark-claude-")  # empty dir: no project CLAUDE.md
        self._files: dict[str, str] = {}
        if self.bin:
            try:
                h = subprocess.run([self.bin, "--help"], capture_output=True, text=True, timeout=30).stdout
                self.flags = {f for f in ("--safe-mode", "--no-session-persistence", "--json-schema", "--tools", "--effort", "--strict-mcp-config",
                                                 "--add-dir", "--restricted", "--permission-mode") if f in h}
            except Exception:
                pass

    @property
    def available(self) -> bool:
        return bool(self.bin)

    @property
    def problem(self) -> str:
        return "" if self.bin else "Claude Code CLI not found. Install it and run `claude` once to log in, or run `hark --setup` to use an API key."

    @property
    def label(self) -> str:
        return f"your Claude subscription (Claude Code at {self.bin})"

    @property
    def can_research(self) -> bool:
        return self.available and "--tools" in self.flags

    @property
    def can_search_corpus(self) -> bool:
        # --restricted is what keeps the file tools inside the collection's folder.
        return self.available and {"--tools", "--add-dir", "--restricted"} <= self.flags

    def _system_file(self, text: str) -> str:
        # Pass the prompt as a file: long multi-line arguments get mangled by Windows .cmd shims.
        key = hashlib.sha1(text.encode()).hexdigest()[:16]
        if key not in self._files:
            path = os.path.join(self.workdir, f"system-{key}.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            self._files[key] = path
        return self._files[key]

    def _cmd(self, system: str, schema: dict, effort: str | None, model: str, tools: str = "") -> list[str]:
        cmd = [self.bin, "-p", "--model", model, "--output-format", "json", "--system-prompt-file", self._system_file(system)]
        if "--safe-mode" in self.flags:
            cmd.append("--safe-mode")  # no CLAUDE.md, MCP servers, hooks or plugins; login still works
        elif "--strict-mcp-config" in self.flags:
            cmd += ["--strict-mcp-config", "--setting-sources", ""]
        if "--no-session-persistence" in self.flags:
            cmd.append("--no-session-persistence")  # keep these calls out of your /resume list
        if "--tools" in self.flags:
            cmd += ["--tools", tools]  # none for updates; web search and fetch for research
            if tools:
                cmd += ["--allowedTools", tools]
        if "--json-schema" in self.flags:
            cmd += ["--json-schema", json.dumps(schema)]
        if effort and "--effort" in self.flags:
            cmd += ["--effort", effort]
        return cmd

    async def _run(self, cmd: list[str], prompt: str, hint: str, timeout: float) -> tuple[dict, float]:
        if not self.bin:
            raise ScribeError(self.problem)
        env = dict(os.environ)
        # An API key in the environment would make the CLI bill the API instead of your subscription.
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        if "--json-schema" not in self.flags and hint:
            prompt += f"\n\nReply with only a JSON object: {hint}"
        t = time.time()
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=self.workdir, env=env)
        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as e:
            proc.kill()
            await proc.wait()
            if isinstance(e, asyncio.CancelledError):
                raise
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
            data = parse_json(res.get("result") or "")
        return data, took

    async def json(self, system, schema, prompt, *, effort=None, timeout=120, hint=""):
        return await self._run(self._cmd(system, schema, effort, self.model), prompt, hint, timeout)

    async def research(self, system, schema, prompt, *, model=None, effort=None, timeout=360, hint=""):
        if not self.can_research:
            raise ScribeError("Research needs a recent Claude Code CLI")
        return await self._run(self._cmd(system, schema, effort, model or self.model, self.RESEARCH_TOOLS), prompt, hint, timeout)

    async def search_corpus(self, system, schema, prompt, corpus, *, model=None, effort=None, timeout=300, hint=""):
        if not self.can_search_corpus:
            raise ScribeError("Searching your documents needs a recent Claude Code CLI (run `claude update`)")
        cmd = self._cmd(system, schema, effort, model or self.model, self.CORPUS_TOOLS)
        # Read-only tools, confined to the collection's text copy (and hark's own temp folder).
        cmd += ["--add-dir", str(corpus.mirror), "--restricted"]
        if "--permission-mode" in self.flags:
            cmd += ["--permission-mode", "dontAsk"]
        prompt = f"The collection's files are in the folder {corpus.mirror}. Search there.\n\n{prompt}"
        return await self._run(cmd, prompt, hint, timeout)


# -- Anthropic API ---------------------------------------------------------------

class AnthropicAPI(Backend):
    """The Anthropic Messages API with your own key (ANTHROPIC_API_KEY), billed per token."""

    kind = "anthropic"
    can_research = True
    ALIASES = {"sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5", "haiku": "claude-haiku-5-5"}
    # Models that take server-side refusal fallbacks ("default" routing).
    FALLBACK_MODELS = ("claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5")
    FALLBACK_BETA = "server-side-fallback-2026-07-01"
    MAX_TOKENS = 32000
    RESEARCH_ROUNDS = 5  # resumes of a paused server-tool turn

    def __init__(self, model: str = "sonnet", api_key: str | None = None, base_url: str | None = None):
        super().__init__(self.ALIASES.get(model, model))
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY") or ""
        self.base_url = base_url or None
        self._client = None
        self._level: dict[str, int] = {}  # per model: how many optional request features to leave off

    @property
    def available(self) -> bool:
        return bool(self.api_key or os.environ.get("ANTHROPIC_AUTH_TOKEN"))

    @property
    def problem(self) -> str:
        return "" if self.available else "No Anthropic API key. Set ANTHROPIC_API_KEY or run `hark --setup`."

    @property
    def label(self) -> str:
        return "the Anthropic API" + (f" at {self.base_url}" if self.base_url else "")

    @property
    def client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.AsyncAnthropic(api_key=self.api_key or None, base_url=self.base_url)
        return self._client

    def _model(self, model: str | None) -> str:
        return self.ALIASES.get(model, model) if model else self.model

    @staticmethod
    def _search_tools(model: str) -> list[dict]:
        # Dynamic-filtering web tools on current Opus and Sonnet; the basic ones elsewhere.
        new = any(model.startswith(p) for p in ("claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6",
                                                 "claude-sonnet-5", "claude-sonnet-4-6", "claude-fable"))
        search, fetch = ("web_search_20260209", "web_fetch_20260209") if new else ("web_search_20250305", "web_fetch_20250910")
        return [{"type": search, "name": "web_search", "max_uses": 5}, {"type": fetch, "name": "web_fetch", "max_uses": 5}]

    async def _create(self, kw: dict, effort: str | None, timeout: float):
        """One request, leaving off optional features (refusal fallbacks, then effort) if the model rejects them."""
        import anthropic
        model = kw["model"]
        levels = []
        if model in self.FALLBACK_MODELS:
            levels.append((True, effort))
        levels.append((False, effort))
        if effort:
            levels.append((False, None))
        client = self.client.with_options(timeout=timeout)
        start = min(self._level.get(model, 0), len(levels) - 1)
        for i in range(start, len(levels)):
            fallbacks, eff = levels[i]
            req = dict(kw)
            cfg = dict(req.pop("output_config", None) or {})
            if eff:
                cfg["effort"] = eff
            if cfg:
                req["output_config"] = cfg
            try:
                if fallbacks:
                    async with client.beta.messages.stream(betas=[self.FALLBACK_BETA], fallbacks="default", **req) as s:
                        msg = await s.get_final_message()
                else:
                    async with client.messages.stream(**req) as s:
                        msg = await s.get_final_message()
                self._level[model] = i
                return msg
            except anthropic.BadRequestError as e:
                if i == len(levels) - 1:
                    raise ScribeError(f"Anthropic API: {e.message}"[:300])
            except anthropic.AuthenticationError:
                raise ScribeError("The Anthropic API key was rejected. Run `hark --setup` to change it.")
            except anthropic.PermissionDeniedError as e:
                raise ScribeError(f"The Anthropic API key isn't allowed to do this: {e.message}"[:300])
            except anthropic.NotFoundError:
                raise ScribeError(f"The Anthropic API doesn't know the model {model!r}")
            except anthropic.RateLimitError:
                raise ScribeError("The Anthropic API is rate-limiting this key; hark will try again shortly")
            except anthropic.APITimeoutError:
                raise ScribeError("The Anthropic API took too long to respond")
            except anthropic.APIConnectionError:
                raise ScribeError("Couldn't reach the Anthropic API. Check the internet connection.")
            except anthropic.APIStatusError as e:
                raise ScribeError(f"Anthropic API error {e.status_code}: {e.message}"[:300])

    @staticmethod
    def _check_stop(msg) -> None:
        if msg.stop_reason == "refusal":
            raise ScribeError("Claude declined this request")
        if msg.stop_reason == "max_tokens":
            raise ScribeError("The reply was too long and got cut off")

    async def json(self, system, schema, prompt, *, effort=None, timeout=120, hint=""):
        t = time.time()
        msg = await self._create({
            "model": self.model, "max_tokens": self.MAX_TOKENS, "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {"format": {"type": "json_schema", "schema": strict_schema(schema)}},
        }, effort, timeout)
        self._check_stop(msg)
        return parse_json("".join(b.text for b in msg.content if b.type == "text")), time.time() - t

    async def research(self, system, schema, prompt, *, model=None, effort=None, timeout=360, hint=""):
        # No structured output here: web search answers carry citations, which it can't be combined with.
        t = time.time()
        model = self._model(model)
        messages = [{"role": "user", "content": prompt + (f"\n\nReply with only a JSON object: {hint}" if hint else "")}]
        for _ in range(self.RESEARCH_ROUNDS):
            remaining = max(30.0, timeout - (time.time() - t))
            msg = await self._create({"model": model, "max_tokens": self.MAX_TOKENS, "system": system,
                                      "messages": messages, "tools": self._search_tools(model)}, effort, remaining)
            if msg.stop_reason != "pause_turn":
                break
            messages.append({"role": "assistant", "content": msg.content})  # a long search paused: let it carry on
        self._check_stop(msg)
        # The answer is the text after the last search or fetch result; citations split it into several blocks.
        last = max((i for i, b in enumerate(msg.content) if b.type.endswith("_tool_result")), default=-1)
        return parse_json("".join(b.text for b in msg.content[last + 1:] if b.type == "text")), time.time() - t

    can_search_corpus = True
    CORPUS_ROUNDS = 15

    async def search_corpus(self, system, schema, prompt, corpus, *, model=None, effort=None, timeout=300, hint=""):
        """A tool loop over hark's own read-only tools (list, search, read), which only see the collection's text copy."""
        t = time.time()
        model = self._model(model)
        prompt = "Use the list_files, search and read tools to look through the collection.\n\n" + prompt
        messages = [{"role": "user", "content": prompt + (f"\n\nWhen you're done, reply with only a JSON object: {hint}" if hint else "")}]
        for i in range(self.CORPUS_ROUNDS):
            remaining = timeout - (time.time() - t)
            if remaining < 5:
                raise ScribeError("Searching the documents took too long")
            last = i == self.CORPUS_ROUNDS - 1
            kw = {"model": model, "max_tokens": self.MAX_TOKENS, "system": system, "messages": messages, "tools": corpus.TOOLS}
            if last:
                kw["tool_choice"] = {"type": "none"}  # out of rounds: answer from what it has read
            msg = await self._create(kw, effort, remaining)
            if msg.stop_reason != "tool_use":
                break
            results = []
            for b in msg.content:
                if b.type == "tool_use":
                    out = await asyncio.to_thread(corpus.run_tool, b.name, b.input)
                    results.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
            messages += [{"role": "assistant", "content": msg.content}, {"role": "user", "content": results}]
        self._check_stop(msg)
        return parse_json("".join(b.text for b in msg.content if b.type == "text")), time.time() - t


# -- any OpenAI-compatible API ---------------------------------------------------

class OpenAICompatible(Backend):
    """Any chat-completions API: OpenAI, Gemini, OpenRouter, Mistral, Ollama, LM Studio and so on."""

    kind = "openai"
    DEFAULT_URL = "https://api.openai.com/v1"
    FORMATS = ("json_schema", "json_object", None)  # fall back when a provider doesn't support one

    def __init__(self, model: str = "", base_url: str | None = None, api_key: str | None = None):
        super().__init__(model)
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or self.DEFAULT_URL).rstrip("/")
        self.api_key = api_key or os.environ.get("HARK_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
        self._client = None
        self._format = 0

    @property
    def local(self) -> bool:
        host = urlparse(self.base_url).hostname or ""
        return host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local")

    @property
    def available(self) -> bool:
        return bool(self.model) and bool(self.api_key or self.local)

    @property
    def problem(self) -> str:
        if not self.model:
            return "No model chosen for the API. Pass --model or run `hark --setup`."
        if not (self.api_key or self.local):
            return f"No API key for {self.base_url}. Set HARK_API_KEY or run `hark --setup`."
        return ""

    @property
    def label(self) -> str:
        return f"{urlparse(self.base_url).hostname or self.base_url} (OpenAI-compatible API)"

    @property
    def client(self):
        if self._client is None:
            import openai
            self._client = openai.AsyncOpenAI(base_url=self.base_url, api_key=self.api_key or "not-needed")
        return self._client

    async def json(self, system, schema, prompt, *, effort=None, timeout=120, hint=""):
        import openai
        t = time.time()
        # Say what shape to reply in even when the provider enforces it: some only check it's JSON.
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": prompt + f"\n\nReply with only a JSON object: {hint or json.dumps(schema)}"}]
        client = self.client.with_options(timeout=timeout)
        for i in range(self._format, len(self.FORMATS)):
            fmt = self.FORMATS[i]
            req = {"model": self.model, "messages": messages}
            if fmt == "json_schema":
                req["response_format"] = {"type": "json_schema", "json_schema": {"name": "reply", "schema": schema, "strict": False}}
            elif fmt:
                req["response_format"] = {"type": fmt}
            try:
                res = await client.chat.completions.create(**req)
            except (openai.BadRequestError, openai.UnprocessableEntityError) as e:
                if i == len(self.FORMATS) - 1:
                    raise ScribeError(f"{self.label}: {e.message}"[:300])
                continue
            except openai.AuthenticationError:
                raise ScribeError(f"The API key for {self.base_url} was rejected. Run `hark --setup` to change it.")
            except openai.NotFoundError:
                raise ScribeError(f"{self.base_url} doesn't know the model {self.model!r}")
            except openai.RateLimitError:
                raise ScribeError("The API is rate-limiting this key; hark will try again shortly")
            except openai.APITimeoutError:
                raise ScribeError("The API took too long to respond")
            except openai.APIConnectionError:
                raise ScribeError(f"Couldn't reach {self.base_url}")
            except openai.APIStatusError as e:
                raise ScribeError(f"API error {e.status_code}: {e.message}"[:300])
            self._format = i
            choice = res.choices[0] if res.choices else None
            if choice is None or not choice.message.content:
                raise ScribeError("The API returned an empty reply")
            if choice.finish_reason == "length":
                raise ScribeError("The reply was too long and got cut off")
            return parse_json(choice.message.content), time.time() - t
        raise ScribeError("The API rejected every reply format")


def make_backend(provider: str = "claude-code", model: str | None = None, base_url: str | None = None,
                 api_key: str | None = None) -> Backend:
    if provider == "anthropic":
        return AnthropicAPI(model or "sonnet", api_key=api_key, base_url=base_url)
    if provider == "openai":
        return OpenAICompatible(model or "", base_url=base_url, api_key=api_key)
    if provider not in ("claude-code", "", None):
        raise ValueError(f"unknown provider {provider!r}; choose from {', '.join(PROVIDERS)}")
    return ClaudeCode(model or "sonnet")
