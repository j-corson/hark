import asyncio
import json
import os
import stat
from types import SimpleNamespace

import anthropic
import httpx2
import openai
import pytest

from hark import config
from hark.backends import AnthropicAPI, ClaudeCode, OpenAICompatible, ScribeError, make_backend, parse_json, strict_schema
from hark.hub import speaker_pairs
from hark.research import Researcher
from hark.scribe import SCHEMA, Scribe

REQ = httpx2.Request("POST", "https://example.test/v1")


def bad_request(cls):
    return cls("unsupported parameter", response=httpx2.Response(400, request=REQ), body=None)


def test_parse_json_tolerates_fences_and_chatter():
    assert parse_json('{"ops": []}') == {"ops": []}
    assert parse_json('Here you go:\n```json\n{"ops": [{"op": "delete"}]}\n```') == {"ops": [{"op": "delete"}]}
    with pytest.raises(ScribeError):
        parse_json("no json here")


def test_strict_schema_closes_every_object_and_leaves_the_original():
    out = strict_schema(SCHEMA)
    assert out["additionalProperties"] is False
    assert out["properties"]["ops"]["items"]["additionalProperties"] is False
    assert out["properties"]["speaker_names"]["items"]["additionalProperties"] is False
    assert "additionalProperties" not in SCHEMA


def test_speaker_names_as_list_or_old_mapping():
    assert speaker_pairs([{"from": "Speaker 2", "to": "Ana"}, "junk"]) == [("Speaker 2", "Ana")]
    assert speaker_pairs({"Claire": "Clare"}) == [("Claire", "Clare")]
    assert speaker_pairs(None) == []


def test_make_backend_and_availability(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("HARK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    a = make_backend("anthropic", "sonnet")
    assert isinstance(a, AnthropicAPI) and a.model == "claude-sonnet-5-5" and not a.available and "API key" in a.problem
    assert make_backend("anthropic", "haiku", api_key="sk-ant-x").available
    o = make_backend("openai", "", base_url="https://api.openai.com/v1", api_key="k")
    assert not o.available and "model" in o.problem
    local = make_backend("openai", "llama3.1", base_url="http://localhost:11434/v1")
    assert local.available and not local.can_research  # local servers need no key; no web research
    assert isinstance(make_backend("claude-code", "sonnet", ), ClaudeCode)
    with pytest.raises(ValueError):
        make_backend("gpt", "x")


# -- Anthropic API, with a stand-in client ----------------------------------------

class FakeStream:
    def __init__(self, owner, kw):
        self.owner, self.kw = owner, kw

    async def __aenter__(self):
        self.owner.calls.append(self.kw)
        result = self.owner.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(get_final_message=lambda: _done(result))

    async def __aexit__(self, *exc):
        return False


async def _done(value):
    return value


class FakeAnthropic:
    def __init__(self, results):
        self.results, self.calls = list(results), []
        self.messages = SimpleNamespace(stream=lambda **kw: FakeStream(self, {"beta": False, **kw}))
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: FakeStream(self, {"beta": True, **kw})))

    def with_options(self, **_):
        return self


def text(t):
    return SimpleNamespace(type="text", text=t)


def message(*blocks, stop="end_turn"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop)


def test_anthropic_json_uses_structured_output_effort_and_fallbacks():
    api = AnthropicAPI("sonnet", api_key="k")
    api._client = FakeAnthropic([message(text('{"ops": []}'))])
    data, _ = asyncio.run(api.json("sys", SCHEMA, "prompt", effort="low"))
    call = api._client.calls[0]
    assert data == {"ops": []}
    assert call["beta"] and call["fallbacks"] == "default" and call["model"] == "claude-sonnet-5-5"
    assert call["output_config"]["effort"] == "low"
    assert call["output_config"]["format"]["schema"]["additionalProperties"] is False


def test_anthropic_drops_optional_features_the_model_rejects_and_remembers():
    api = AnthropicAPI("claude-sonnet-5-5", api_key="k")
    api._client = FakeAnthropic([bad_request(anthropic.BadRequestError), bad_request(anthropic.BadRequestError),
                                 message(text('{"ops": []}')), message(text('{"ops": []}'))])
    asyncio.run(api.json("sys", SCHEMA, "p", effort="low"))
    first, second, third = api._client.calls
    assert first["beta"] and not second["beta"] and second["output_config"].get("effort") == "low"
    assert "effort" not in third["output_config"]
    asyncio.run(api.json("sys", SCHEMA, "p", effort="low"))
    assert "effort" not in api._client.calls[3]["output_config"]  # went straight to what worked


def test_anthropic_refusal_and_cut_off_replies_are_errors():
    api = AnthropicAPI("haiku", api_key="k")
    api._client = FakeAnthropic([message(text("{}"), stop="refusal"), message(text('{"op'), stop="max_tokens")])
    with pytest.raises(ScribeError, match="declined"):
        asyncio.run(api.json("s", SCHEMA, "p"))
    with pytest.raises(ScribeError, match="cut off"):
        asyncio.run(api.json("s", SCHEMA, "p"))
    assert not api._client.calls[0]["beta"]  # Haiku 5.5 has no server-side fallback


def test_anthropic_research_resumes_paused_turns_and_reads_text_after_the_last_search():
    api = AnthropicAPI("sonnet", api_key="k")
    found = '{"answer": "Yes", "items": [{"title": "Ofgem", "note": "archive", "url": "https://www.ofgem.gov.uk/"}]}'
    api._client = FakeAnthropic([
        message(text("Searching."), SimpleNamespace(type="server_tool_use"), stop="pause_turn"),
        message(SimpleNamespace(type="web_search_tool_result"), text(found[:30]), text(found[30:])),
    ])
    research = Researcher(Scribe(api), "opus")
    result, _ = asyncio.run(research.run("Are Ofgem responses public?", "Backlog"))
    first, second = api._client.calls
    assert first["model"] == "claude-opus-5-5" and {t["name"] for t in first["tools"]} == {"web_search", "web_fetch"}
    assert first["tools"][0]["type"] == "web_search_20260209"
    assert len(second["messages"]) == 2  # the paused turn was sent back to carry on
    assert result["answer"] == "Yes" and result["items"][0]["url"] == "https://www.ofgem.gov.uk/"


def test_anthropic_auth_failure_is_explained():
    api = AnthropicAPI("sonnet", api_key="bad")
    err = anthropic.AuthenticationError("invalid x-api-key", response=httpx2.Response(401, request=REQ), body=None)
    api._client = FakeAnthropic([err])
    with pytest.raises(ScribeError, match="hark --setup"):
        asyncio.run(api.json("s", SCHEMA, "p"))


# -- OpenAI-compatible, with a stand-in client ----------------------------------

class FakeOpenAI:
    def __init__(self, results):
        self.results, self.calls = list(results), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kw):
        self.calls.append(kw)
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def with_options(self, **_):
        return self


def completion(content, finish="stop"):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)])


def test_openai_falls_back_through_reply_formats_and_remembers():
    api = OpenAICompatible("some-model", base_url="https://api.example.test/v1", api_key="k")
    api._client = FakeOpenAI([bad_request(openai.BadRequestError), completion('```json\n{"ops": []}\n```'),
                              completion('{"ops": []}')])
    data, _ = asyncio.run(api.json("sys", SCHEMA, "prompt", hint='{"ops": [...]}'))
    first, second = api._client.calls
    assert data == {"ops": []}
    assert first["response_format"]["type"] == "json_schema" and second["response_format"] == {"type": "json_object"}
    assert first["messages"][0] == {"role": "system", "content": "sys"} and "JSON object" in first["messages"][1]["content"]
    asyncio.run(api.json("sys", SCHEMA, "prompt"))
    assert api._client.calls[2]["response_format"] == {"type": "json_object"}


def test_openai_backend_has_no_research():
    api = OpenAICompatible("m", base_url="http://localhost:11434/v1")
    assert not Researcher(Scribe(api)).available
    with pytest.raises(ScribeError, match="needs Claude"):
        asyncio.run(Researcher(Scribe(api)).run("anything"))


# -- saved settings ---------------------------------------------------------------

def test_settings_saved_privately_and_command_line_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG", tmp_path)
    path = config.save({"provider": "openai", "model": "m1", "base_url": "http://localhost:11434/v1", "api_key": "", "junk": "x"})
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert json.loads(path.read_text()) == {"provider": "openai", "model": "m1", "base_url": "http://localhost:11434/v1"}
    from hark import cli
    monkeypatch.setattr(cli.settings_file, "load", config.load)
    for var in ("HARK_PROVIDER", "HARK_MODEL", "HARK_BASE_URL", "HARK_RESEARCH_MODEL"):
        monkeypatch.delenv(var, raising=False)
    args = SimpleNamespace(provider=None, model=None, base_url=None, research_model=None)
    assert cli.resolve(args)["model"] == "m1"
    args.model = "m2"
    assert cli.resolve(args)["model"] == "m2"
    # Switching service on the command line doesn't carry over the other service's settings.
    args = SimpleNamespace(provider="anthropic", model=None, base_url=None, research_model=None)
    cfg = cli.resolve(args)
    assert cfg["provider"] == "anthropic" and cfg["model"] is None and cfg["base_url"] is None
