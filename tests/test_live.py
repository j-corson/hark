"""Grilling's open Claude conversation (live.py), the grilling skill (skill.py), and how the hub uses them."""

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hark import live as live_mod, skill  # noqa: E402
from hark.backends import ScribeError  # noqa: E402


def plugin(root: Path, version: str, with_skill: bool = True) -> Path:
    d = root / version
    (d / "skills" / "productivity" / "grilling").mkdir(parents=True)
    if with_skill:
        (d / "skills" / "productivity" / "grilling" / "SKILL.md").write_text(
            f"---\nname: grilling\ndescription: x\n---\n\nGrill them ({version}).\n", encoding="utf-8")
    return d


def test_the_newest_installed_skill_is_used_without_its_front_matter(tmp_path):
    plugin(tmp_path, "1.3.1")
    newest = plugin(tmp_path, "1.10.0")
    plugin(tmp_path, "2.0.0", with_skill=False)  # no grilling skill in it
    assert skill.plugin_dir(tmp_path) == newest
    assert skill.text(tmp_path) == "Grill them (1.10.0)."


def test_without_the_plugin_hark_sends_its_own_copy(tmp_path):
    assert skill.plugin_dir(tmp_path / "nowhere") is None
    own = skill.text(tmp_path / "nowhere")
    assert own.startswith("Interview the user relentlessly") and "<!--" not in own and "design tree" in own


class FakeClient:
    """Stands in for ClaudeSDKClient: answers each update, and records what it was sent."""

    def __init__(self, answers):
        self.answers, self.sent, self.closed = answers, [], False

    async def disconnect(self):
        self.closed = True


def live_session(answers, monkeypatch, plugin_dir=None):
    clients = []

    async def connect(system, schema):
        c = FakeClient(answers)
        clients.append(c)
        return c

    async def ask(self, prompt):
        self.client.sent.append(prompt)
        a = self.client.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(live_mod.LiveSession, "_ask", ask)
    monkeypatch.setattr(skill, "plugin_dir", lambda cache=None: plugin_dir)
    return live_mod.LiveSession("s1", "sonnet", "low", connect=connect), clients


def test_a_conversation_carries_on_until_the_brief_changes_or_it_fails(monkeypatch):
    ls, clients = live_session([{"ops": []}] * 3 + [ScribeError("boom"), {"ops": []}], monkeypatch)
    assert not ls.continues("brief")
    asyncio.run(ls.edit("sys", {}, "p1", "brief"))
    assert ls.continues("brief") and not ls.continues("another brief")
    asyncio.run(ls.edit("sys", {}, "p2", "brief"))
    asyncio.run(ls.edit("sys", {}, "p3", "another brief"))  # a new brief: a new conversation
    assert len(clients) == 2 and clients[0].closed and clients[0].sent == ["p1", "p2"]
    try:
        asyncio.run(ls.edit("sys", {}, "p4", "another brief"))
    except ScribeError:
        pass
    assert clients[1].closed and not ls.continues("another brief")  # an error starts it again next time
    asyncio.run(ls.edit("sys", {}, "p5", "another brief"))
    assert len(clients) == 3


def test_a_long_conversation_starts_again(monkeypatch):
    ls, clients = live_session([{"ops": []}] * 2, monkeypatch)
    asyncio.run(ls.edit("sys", {}, "p1", "b"))
    ls.turns = live_mod.MAX_TURNS
    assert not ls.continues("b")
    asyncio.run(ls.edit("sys", {}, "p2", "b"))
    assert len(clients) == 2 and ls.turns == 1


def test_the_skill_is_loaded_from_the_plugin_or_else_sent_as_text(monkeypatch, tmp_path):
    ls, _ = live_session([], monkeypatch, plugin_dir=tmp_path)
    lead, text = ls.opening()
    assert skill.NAME in lead and text == ""
    ls, _ = live_session([], monkeypatch, plugin_dir=None)
    lead, text = ls.opening()
    assert lead == "" and "design tree" in text


def test_one_conversation_at_a_time_for_the_session_being_grilled():
    lv = live_mod.Live("sonnet", "low")
    a = asyncio.run(lv.for_session("s1"))
    assert asyncio.run(lv.for_session("s1")) is a
    assert asyncio.run(lv.for_session("s2")) is not a


def test_live_sessions_need_claude_code_and_the_sdk(monkeypatch):
    cc = SimpleNamespace(kind="claude-code", available=True, model="sonnet", bin="claude")
    assert isinstance(live_mod.make_live(cc, "low"), live_mod.Live)
    assert live_mod.make_live(SimpleNamespace(kind="anthropic", available=True, model="m", bin=None), "low") is None
    monkeypatch.setenv("HARK_LIVE", "0")
    assert live_mod.make_live(cc, "low") is None


# -- the hub: what an update sends ------------------------------------------------------------

from hark.hub import Hub  # noqa: E402
from hark.scribe import Scribe  # noqa: E402


class FakeLiveSession:
    def __init__(self):
        self.prompts, self.turns, self.brief = [], 0, ""

    def continues(self, brief):
        return self.turns > 0 and brief == self.brief

    def opening(self):
        return "LOAD THE SKILL\n\n", ""

    async def edit(self, system, schema, prompt, brief, timeout=120):
        self.prompts.append(prompt)
        self.turns, self.brief = self.turns + 1, brief
        return {"ops": []}, 1.0


class FakeLive:
    def __init__(self, session):
        self.session = session

    async def for_session(self, owner):
        return self.session


def grill_hub(tmp_path, live_session=None):
    backend = SimpleNamespace(available=True, model="fake", problem="", kind="claude-code")
    scribe = Scribe(backend, live=FakeLive(live_session) if live_session else None)
    stateless = []

    async def edit(prompt, timeout=120):
        stateless.append(prompt)
        return {"ops": []}, 1.0

    scribe.edit = edit
    research = SimpleNamespace(available=True, collections=lambda: [], find=lambda n: None)
    hub = Hub(SimpleNamespace(backlog=0.0, asr_name="x"), scribe, tmp_path, researcher=research)
    hub.session.set_mode("grill")
    return hub, stateless


def update(hub, text):
    hub.session.add_line(time.time(), "mic", "S1", text)
    asyncio.run(hub._update())


def test_grilling_sends_only_whats_new_once_the_conversation_is_under_way(tmp_path):
    ls = FakeLiveSession()
    hub, stateless = grill_hub(tmp_path, ls)
    update(hub, "First we look at the Pilot.")
    update(hub, "Then the budget.")
    first, second = ls.prompts
    assert first.startswith("LOAD THE SKILL") and "Grill the group with the grilling skill" in first
    assert "<brief>\n(as before)\n</brief>" in second and "<earlier_transcript>" not in second
    assert "Then the budget." in second and "LOAD THE SKILL" not in second
    assert not stateless


def test_without_a_live_session_grilling_sends_the_skill_each_time(tmp_path):
    hub, stateless = grill_hub(tmp_path)
    update(hub, "First we look at the Pilot.")
    assert "<grilling_skill>" in stateless[0] and "design tree" in stateless[0]
    hub.session.set_mode("meeting")
    update(hub, "Now the minutes.")
    assert "<grilling_skill>" not in stateless[1]


def test_a_conversation_that_wont_open_twice_falls_back_to_stateless_calls(monkeypatch):
    async def connect(system, schema):
        raise RuntimeError("no CLI")

    lv = live_mod.Live("sonnet", "low", connect=connect)
    for _ in range(2):
        ls = asyncio.run(lv.for_session("s1"))
        try:
            asyncio.run(ls.edit("sys", {}, "p", "b"))
        except ScribeError:
            pass
    assert asyncio.run(lv.for_session("s1")) is None and asyncio.run(lv.for_session("s2")) is None


def test_the_sdk_is_never_handed_a_cmd_shim():
    assert live_mod.native(r"C:\nvm4w\nodejs\claude.CMD") is None and live_mod.native(None) is None
    assert live_mod.native(r"C:\Users\x\.local\bin\claude.exe") == r"C:\Users\x\.local\bin\claude.exe"
