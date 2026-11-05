"""A live grilling session's updates through one Claude Code conversation kept open.

Every other call hark makes is stateless: a fresh `claude -p` that is sent everything. A
grilling session instead keeps one conversation open through the Claude Agent SDK, with the
grilling skill loaded from the installed mattpocock-skills plugin (or sent as text when it
isn't installed). After the first turn each update sends only what is new: the document,
which stays authoritative, and the speech since the last update. It is about twice as fast,
and the skill's rounds stay in Claude's memory.

The conversation starts again, from a full prompt, on a new session, a changed brief, an
error, or after MAX_TURNS updates. Without the SDK (or with another provider) hark uses the
stateless call, with the skill as text.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time

from . import skill
from .backends import ScribeError, parse_json

log = logging.getLogger("hark")

MAX_TURNS = 150  # about two hours of updates; then start again from the document, before context grows large
NOTE = """

This is one continuing conversation: you get each update of the live session in turn, and your earlier turns are your memory of it. <document> is still authoritative over your memory, as people edit it by hand. Instructions in earlier turns have been carried out already: never carry one out again."""


def sdk_installed() -> bool:
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        return False
    return True


def native(path: str | None) -> str | None:
    """The CLI for the SDK to run: the SDK won't run a Windows .cmd shim (npm's), so then it uses its own bundled one."""
    return path if path and not path.lower().endswith((".cmd", ".bat")) else None


class LiveSession:
    """One open conversation for one hark session. `connect` makes the SDK client (stubbed in tests)."""

    def __init__(self, owner: str, model: str, effort: str | None, claude_bin: str | None = None, connect=None):
        self.owner = owner
        self.model = model
        self.effort = effort
        self.bin = claude_bin
        self.connect = connect or self._connect
        self.client = None
        self.turns = 0
        self.brief = ""
        self.plugin = skill.plugin_dir()
        self.failed_starts = 0  # conversations that wouldn't open; see Live.for_session

    def continues(self, brief: str) -> bool:
        """Whether the next update can send only what's new: the conversation is open, on the same brief, and not too long."""
        return self.client is not None and 0 < self.turns < MAX_TURNS and brief == self.brief

    def opening(self) -> tuple[str, str]:
        """For a fresh conversation: (words to put before the prompt, skill text to send in it)."""
        if self.plugin:
            return f"First load the `{skill.NAME}` skill with the Skill tool: it is the method the brief runs.\n\n", ""
        return "", skill.text()

    async def _connect(self, system: str, schema: dict):
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        opts = ClaudeAgentOptions(
            system_prompt=system + NOTE, model=self.model, effort=self.effort,
            output_format={"type": "json_schema", "schema": schema},
            setting_sources=[], tools=["Skill"] if self.plugin else [],
            plugins=[{"type": "local", "path": str(self.plugin)}] if self.plugin else [],
            skills=[skill.NAME] if self.plugin else [],
            cwd=tempfile.mkdtemp(prefix="hark-live-"), max_turns=6, cli_path=native(self.bin),
            # An API key in the environment would make the CLI bill the API instead of your subscription.
            env={"ANTHROPIC_API_KEY": "", "ANTHROPIC_AUTH_TOKEN": ""})
        client = ClaudeSDKClient(opts)
        await client.connect()
        return client

    async def edit(self, system: str, schema: dict, prompt: str, brief: str, timeout: float = 120) -> tuple[dict, float]:
        """One update. `prompt` is whole on a fresh conversation, and only what's new when it `continues`."""
        if not self.continues(brief):
            await self.aclose()
        t = time.time()
        try:
            if self.client is None:
                try:
                    self.client = await asyncio.wait_for(self.connect(system, schema), timeout)
                except Exception:
                    self.failed_starts += 1
                    raise
                self.failed_starts = 0
                self.brief = brief
            data = await asyncio.wait_for(self._ask(prompt), timeout)
        except asyncio.TimeoutError:
            await self.aclose()
            raise ScribeError("Claude took too long to respond")
        except ScribeError:
            await self.aclose()
            raise
        except Exception as e:  # the SDK's own errors: start again next time
            await self.aclose()
            raise ScribeError(f"the live Claude session failed: {e}"[:300])
        self.turns += 1
        return data, time.time() - t

    async def _ask(self, prompt: str) -> dict:
        from claude_agent_sdk import ResultMessage

        await self.client.query(prompt)
        res = None
        async for m in self.client.receive_response():
            if isinstance(m, ResultMessage):
                res = m
        if res is None or res.is_error:
            raise ScribeError(str((res and res.result) or "Claude returned an error")[:300])
        data = res.structured_output
        if data is None:
            data = parse_json(res.result or "")
        if isinstance(data, str):
            data = json.loads(data)
        return data

    async def aclose(self) -> None:
        client, self.client, self.turns = self.client, None, 0
        if client is not None:
            try:
                await client.disconnect()
            except Exception as e:  # noqa: BLE001  (closing a broken session)
                log.info("live session: %s", e)


class Live:
    """The open conversation, if any: one at a time, for the hark session being grilled."""

    def __init__(self, model: str, effort: str | None, claude_bin: str | None = None, connect=None):
        self.model, self.effort, self.bin, self.connect = model, effort, claude_bin, connect
        self.session: LiveSession | None = None
        self.broken = False

    async def for_session(self, owner: str) -> LiveSession | None:
        """The conversation for this session, or None to use stateless calls: after a conversation
        has failed to open twice in a row, hark stops trying until it restarts."""
        if self.broken:
            return None
        if self.session and self.session.failed_starts >= 2:
            self.broken = True
            log.warning("live session: couldn't start one, so grilling uses one call per update")
            await self.aclose()
            return None
        if self.session and self.session.owner != owner:
            await self.session.aclose()
            self.session = None
        if self.session is None:
            self.session = LiveSession(owner, self.model, self.effort, self.bin, self.connect)
        return self.session

    async def aclose(self) -> None:
        if self.session:
            await self.session.aclose()
            self.session = None


def make_live(backend, effort: str | None) -> Live | None:
    """Live sessions need Claude Code (your subscription) and the Agent SDK; otherwise None."""
    if backend.kind != "claude-code" or not backend.available or os.environ.get("HARK_LIVE") == "0" or not sdk_installed():
        return None
    return Live(backend.model, effort, backend.bin)
