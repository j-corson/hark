import asyncio
from types import SimpleNamespace

from hark.hub import Hub, research_findings
from hark.research import Researcher, guess_kind, system_for
from hark.session import research_markdown


class Sent:
    def __init__(self):
        self.msgs = []

    async def send_json(self, m):
        self.msgs.append(m)

    async def send_str(self, m):
        import json
        self.msgs.append(json.loads(m))


def make_hub(tmp_path):
    hub = Hub(SimpleNamespace(backlog=0.0, asr_name="x"), SimpleNamespace(available=True, model="fake"), tmp_path,
              researcher=SimpleNamespace(available=True))
    hub.session.set_doc("# Notes\n\n## Budget\n- tight\n", by="claude")
    ws = Sent()
    hub.clients[ws] = SimpleNamespace(editing=False, streams={})
    hub._pump_research = lambda: None  # don't start real lookups
    return hub, ws


def test_guess_kind():
    assert guess_kind("What is a Ralph Wiggum loop?") == "quick"
    assert guess_kind("When did the energy price cap start?") == "quick"
    assert guess_kind("Find the seminal papers on rebound effects") == "sources"
    assert guess_kind("Any good references on fuel poverty?") == "sources"
    assert guess_kind("What's the evidence base for smart meters, with sources") == "sources"


def test_quick_and_sources_prompts_differ():
    assert "quick look-up" in system_for("quick") and "at most 5 findings" not in system_for("quick")
    assert "at most 5 findings" in system_for("sources")


def test_quick_run_is_light_and_keeps_one_source():
    calls = []

    async def research(system, schema, prompt, *, model=None, effort=None, timeout=360, hint=""):
        calls.append((system, effort, timeout))
        return {"answer": "A bash loop that re-runs a coding agent.",
                "items": [{"title": f"Source {i}", "note": "n", "url": f"https://e.org/{i}"} for i in range(3)]}, 4.0

    r = Researcher(SimpleNamespace(backend=SimpleNamespace(research=research, can_research=True), model="m"))
    out, _ = asyncio.run(r.run("What is a Ralph Wiggum loop?", "", "quick"))
    assert len(out["items"]) == 1 and calls[0][1] == "low" and calls[0][2] <= 180
    out, _ = asyncio.run(r.run("Papers on X", "", "sources"))
    assert len(out["items"]) == 3 and calls[1][1] == "medium"


def test_scribe_kind_is_used_else_guessed(tmp_path):
    hub, ws = make_hub(tmp_path)
    s = hub.session
    asyncio.run(hub._requested_research(s, [
        {"label": "Ralph loop", "question": "What is a Ralph Wiggum loop?", "kind": "quick"},
        {"label": "Rebound", "question": "Key papers on rebound effects"},
        {"label": "Odd", "question": "Something", "kind": "nonsense"},
    ]))
    assert [j["kind"] for j in s.research] == ["quick", "sources", "quick"]


def test_quick_answer_can_be_added_or_followed_up_with_sources(tmp_path):
    hub, ws = make_hub(tmp_path)
    s = hub.session
    job = s.add_research("What is a Ralph Wiggum loop?", "conversation", "done", "Ralph loop", "quick")
    job.update(answer="A bash loop that keeps re-running a coding agent until the task is done.", items=[])
    assert "Answer: A bash loop" in "\n".join(research_findings(s))

    async def go():
        await hub.handle(ws, {"type": "research_add", "id": job["id"], "items": None})
        await hub.handle(ws, {"type": "research_sources", "id": job["id"]})
        await hub.handle(ws, {"type": "research_sources", "id": job["id"]})  # only once

    asyncio.run(go())
    assert job["added"] and "Answer: A bash loop" in hub.promote[-1][1]
    assert research_findings(s) == []  # added, so Claude no longer offers it
    deeper = [j for j in s.research if j["id"] != job["id"]]
    assert len(deeper) == 1 and deeper[0]["kind"] == "sources" and deeper[0]["q"] == job["q"]
    assert job["deeper"] == deeper[0]["id"] and deeper[0]["state"] == "queued"


def test_saved_research_says_when_a_quick_answer_has_no_source():
    md = research_markdown("T", [{"q": "q", "label": "L", "kind": "quick", "answer": "A.", "items": []}])
    assert "without a source" in md


def test_long_answers_end_at_a_sentence_not_mid_word():
    from hark.research import clip
    text = "First point here. " * 80
    out = clip(text.strip(), 900)
    assert len(out) <= 902 and out.endswith("here. …")
    assert clip("short answer.", 900) == "short answer."
    assert clip("word " * 300, 100).endswith("word …")


def test_full_answer_is_kept():
    long = "Innate immunity acts fast and is built in. " * 40

    async def research(system, schema, prompt, *, model=None, effort=None, timeout=360, hint=""):
        return {"answer": long, "items": []}, 1.0

    r = Researcher(SimpleNamespace(backend=SimpleNamespace(research=research, can_research=True), model="m"))
    out, _ = asyncio.run(r.run("What is innate immunity?", "", "quick"))
    assert out["answer"] == long.strip()
