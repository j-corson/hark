import asyncio
import time
from types import SimpleNamespace

from hark.slides import job_slide

LONG = ("Rebound effects after efficiency retrofits are usually modest. " * 12).strip()


def test_slide_from_a_sources_card():
    job = {"id": 4, "kind": "sources", "q": "Find papers on retrofit rebound effects in UK homes", "label": "Retrofit rebound papers",
           "answer": "Rebound is usually 10-30%.", "items": [
               {"title": "Sorrell (2009). Rebound review.", "note": "The standard review", "url": "https://doi.org/10.1/x", "added": False},
               {"title": "A report with no link", "note": "", "url": "", "added": False}]}
    s = job_slide(job)
    assert s["title"] == "Retrofit rebound papers"
    assert s["body"] == [{"text": "Rebound is usually 10-30%.", "level": 0},
                         {"text": "Sorrell (2009). Rebound review.", "url": "https://doi.org/10.1/x", "level": 0},
                         {"text": "A report with no link", "level": 0}]
    assert "Sorrell (2009). Rebound review.: The standard review" in s["notes"]
    assert "Find papers on retrofit rebound effects in UK homes" in s["notes"]
    assert s["notes"].endswith("from hark research")
    assert "Rebound is usually" not in s["notes"]  # not shortened, so not repeated


def test_long_answer_is_shortened_and_kept_whole_in_the_notes():
    s = job_slide({"id": 1, "kind": "quick", "q": "What is rebound?", "label": "Rebound", "answer": LONG, "items": []})
    assert len(s["body"]) == 1 and s["body"][0]["text"].endswith("…") and len(s["body"][0]["text"]) <= 402
    assert LONG in s["notes"]


def test_at_most_six_findings_and_collection_files_show_their_path():
    items = [{"title": f"Doc {i}", "note": "", "path": f"notes/{i}.md", "url": "", "added": False} for i in range(8)]
    s = job_slide({"id": 2, "kind": "corpus", "q": "q", "label": "From the notes", "answer": "", "items": items})
    assert [b["text"] for b in s["body"]] == [f"Doc {i} (notes/{i}.md)" for i in range(6)]
    assert job_slide({"id": 3, "kind": "meeting", "q": "What's left?", "label": "What's left?", "answer": "Two aims.", "items": []})["body"] \
        == [{"text": "Two aims.", "level": 0}]


# -- a slide from a section of the document ---------------------------------------------

from hark.slides import section_slide  # noqa: E402

OUTLINE = """# Paper

## Methods
- **Interviews** with 20 households, see [the protocol](https://example.org/p)
  - recorded and transcribed
- [x] Ethics approved
### Sampling
- By tariff
### Analysis

## Working notes
- maybe a policy box
### Later
- something
"""


def test_outline_section_with_subsections_as_indented_bullets():
    s = section_slide(OUTLINE, "## Methods", "outline")
    assert s["title"] == "Methods"
    assert s["body"] == [{"text": "Interviews with 20 households, see the protocol", "url": "https://example.org/p", "level": 0},
                         {"text": "recorded and transcribed", "level": 1},
                         {"text": "✓ Ethics approved", "level": 0},
                         {"text": "Sampling", "level": 1},
                         {"text": "Analysis", "level": 1}]
    assert s["notes"].endswith("from hark (room screen)")
    assert section_slide(OUTLINE, "methods", "outline")["title"] == "Methods"  # found as the screen finds it


def test_working_notes_and_missing_sections_are_never_slid():
    assert section_slide(OUTLINE, "## Working notes", "outline") is None
    assert section_slide(OUTLINE, "### Later", "outline") is None
    assert section_slide(OUTLINE, "## Results", "outline") is None


def test_a_long_section_overflows_into_the_notes():
    doc = "# D\n\n## Long\n" + "\n".join(f"- point {i}" for i in range(20)) + "\n"
    s = section_slide(doc, "## Long", "notes")
    assert 0 < len(s["body"]) < 20
    assert "point 19" in s["notes"] and "point 0" not in s["notes"]


GRILL = """# Backlog

## Items
### 3. Ofgem responses
- **Question**: How did supplier views change?
- **Status**: being grilled
#### Open questions
- **Q1. Years covered**: Which years should the corpus cover? ➡️ Suggested: every response from 2010 to now.
- **Q2. Format**: PDF or text?
### 4. Smart meter data
- **Question**: ?
"""


def test_grilling_puts_the_question_and_suggested_answer_on_the_slide():
    s = section_slide(GRILL, "### 3. Ofgem responses", "grill")
    assert s["title"] == "Which years should the corpus cover?"
    assert s["body"] == [{"text": "Suggested: every response from 2010 to now.", "level": 0}]
    assert "Ofgem responses" in s["notes"]
    # No open question: the item and its fields.
    s = section_slide(GRILL, "### 4. Smart meter data", "grill")
    assert s["title"] == "Smart meter data" and s["body"] == [{"text": "Question: ?", "level": 0}]


MEETING = """# Budget review

## Discussion
### Venue
- Leeds is cheapest
### Travel
- Trains only

## Decisions
- Venue: Leeds, in March
- Travel by train

## Actions
"""


def test_meeting_topic_with_its_notes_and_decisions():
    s = section_slide(MEETING, "### Venue", "meeting")
    assert s["title"] == "Venue"
    assert [b["text"] for b in s["body"]] == ["Leeds is cheapest", "Decided: Venue: Leeds, in March"]


# -- finding the linked deck among the open presentations ----------------------------------

from hark.slides import match_deck  # noqa: E402


def test_open_deck_matches_ignoring_case_and_slashes():
    opened = [r"C:\Users\jamie\Documents\other.pptx", r"c:\users\JAMIE\documents\Talk.pptx"]
    assert match_deck(r"C:/Users/jamie/Documents/talk.pptx", opened) == opened[1]
    assert match_deck(r"C:\Users\jamie\Documents\talk.pptx", opened[:1]) is None
    assert match_deck(r"C:\Users\jamie\Documents\talk.pptx", []) is None


def test_onedrive_deck_matches_by_its_place_in_onedrive_not_its_name():
    root = r"C:\Users\jamie\OneDrive"
    linked = r"C:\Users\jamie\OneDrive\Talks\2026\Talk.pptx"
    url = "https://d.docs.live.net/4f2a9c/Talks/2026/Talk.pptx"
    assert match_deck(linked, [url], root) == url
    assert match_deck(linked, ["https://d.docs.live.net/4f2a9c/Talks/2026/Talk%20v2.pptx"], root) is None
    assert match_deck(linked.replace("Talk.pptx", "Talk v2.pptx"), ["https://d.docs.live.net/4f2a9c/Talks/2026/Talk%20v2.pptx"], root)
    assert match_deck(linked, ["https://d.docs.live.net/4f2a9c/Elsewhere/Talk.pptx"], root) is None  # same name only
    assert match_deck(root + r"\Talk.pptx", ["https://d.docs.live.net/4f2a9c/Talk.pptx"], root)  # in OneDrive's top folder
    assert match_deck(root + r"\Talk.pptx", ["https://d.docs.live.net/4f2a9c/Talks/Talk.pptx"], root) is None
    work = "https://contoso-my.sharepoint.com/personal/jamie_contoso_com/Documents/Talks/2026/Talk.pptx"
    assert match_deck(linked, [work], root) == work
    assert match_deck(r"D:\Decks\Talk.pptx", ["https://d.docs.live.net/4f2a9c/Talk.pptx"], root) is None  # not in OneDrive
    assert match_deck(linked, [url], None) is None


# -- "Claude, put this on a slide" ------------------------------------------------------------

from hark.screen import screen_request  # noqa: E402


def test_slide_requests_are_recognised_without_a_claude_call():
    ask = lambda said, **kw: screen_request(said, OUTLINE, slides=True, **kw)  # noqa: E731
    assert ask("Claude, put this on a slide.") == ("slide", None)
    assert ask("Okay Claude, can you add that to the deck please") == ("slide", None)
    assert ask("Claw, put this on a slide.") == ("slide", None)  # misheard name
    assert ask("Claude, slide this.") == ("slide", None)
    assert ask("Claude, put the methods on a slide.") == ("slide", "## Methods")
    assert ask("Claude, add sampling to the slides") == ("slide", "### Sampling")
    assert ask("put this on a slide", need_wake=False) == ("slide", None)  # typed


def test_unclear_slide_requests_are_left_to_claude():
    ask = lambda said, **kw: screen_request(said, OUTLINE, slides=True, **kw)  # noqa: E731
    assert ask("Put this on a slide.") is None  # not addressed to Claude
    assert ask("Claude, put the bit about tariffs on a slide.") is None
    assert ask("Claude, put the working notes on a slide.") is None
    assert ask("Claude, add a point on slides to the methods.") is None
    assert screen_request("Claude, put this on a slide.", OUTLINE) is None  # no PowerPoint here


# -- adding to the deck, with PowerShell stubbed --------------------------------------------

from hark.slides import BUSY, NOT_FOUND, NOT_OPEN, Deck, SlidesError  # noqa: E402

SLIDE = {"title": "T", "body": [], "notes": ""}


class FakePowerPoint:
    """Answers the deck's scripts the way PowerPoint would, and records what was asked."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def __call__(self, script, payload, timeout, sta=False):
        self.calls.append((script, payload))
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def deck_with(run):
    naps = []

    async def sleep(t):
        naps.append(t)

    return Deck(run=run, available=True, sleep=sleep), naps


def test_adds_to_the_open_deck(tmp_path):
    f = tmp_path / "Talk.pptx"
    f.write_bytes(b"x")
    pp = FakePowerPoint({"decks": [{"name": str(f).upper(), "readonly": False}]}, {"ok": True})
    deck, _ = deck_with(pp)
    assert asyncio.run(deck.add(str(f), SLIDE)) is None
    assert pp.calls == [("list", {}), ("add", {"deck": str(f).upper(), "slide": SLIDE})]


def test_nothing_is_written_unless_the_deck_is_open_and_writable(tmp_path):
    f = tmp_path / "Talk.pptx"
    f.write_bytes(b"x")
    for answers in ([{"error": "not_running"}],
                    [{"decks": [{"name": str(tmp_path / "Other.pptx"), "readonly": False}]}],
                    [{"decks": [{"name": str(f), "readonly": True}]}]):
        pp = FakePowerPoint(*answers)
        deck, _ = deck_with(pp)
        assert asyncio.run(deck.add(str(f), SLIDE)) == NOT_OPEN
        assert [c[0] for c in pp.calls] == ["list"]
    pp = FakePowerPoint({"decks": [{"name": str(f), "readonly": False}]}, {"error": "not_open"})  # closed meanwhile
    assert asyncio.run(deck_with(pp)[0].add(str(f), SLIDE)) == NOT_OPEN
    pp = FakePowerPoint()
    assert asyncio.run(deck_with(pp)[0].add(str(tmp_path / "Gone.pptx"), SLIDE)) == NOT_FOUND and not pp.calls


def test_busy_powerpoint_is_tried_once_more_after_three_seconds(tmp_path):
    f = tmp_path / "Talk.pptx"
    f.write_bytes(b"x")
    listed = {"decks": [{"name": str(f), "readonly": False}]}
    deck, naps = deck_with(FakePowerPoint(listed, {"error": "busy"}, listed, {"ok": True}))
    assert asyncio.run(deck.add(str(f), SLIDE)) is None and naps == [3]
    deck, naps = deck_with(FakePowerPoint({"error": "busy"}, {"error": "busy"}))
    assert asyncio.run(deck.add(str(f), SLIDE)) == BUSY and naps == [3]
    deck, _ = deck_with(FakePowerPoint(SlidesError("PowerPoint didn't answer in time")))
    assert asyncio.run(deck.add(str(f), SLIDE)) == "couldn't add to slides: PowerPoint didn't answer in time"


# -- the link and the setting are kept with the session ---------------------------------------

from hark.session import Session  # noqa: E402


def test_linked_deck_and_auto_append_survive_reopening(tmp_path):
    s = Session(tmp_path)
    s.set_doc("# Talk\n\n## Methods\n- x\n")
    s.pptx, s.slides_auto = r"C:\Decks\Talk.pptx", True
    job = s.add_research("q", "typed", "done")
    job.update(slide=123.0, slides_note=NOT_OPEN)
    s.save()
    back = Session.load(tmp_path, s.id)
    assert back.pptx == r"C:\Decks\Talk.pptx" and back.slides_auto is True
    assert back.research[0]["slide"] == 123.0 and "slides_note" not in back.research[0]  # the note was for then
    assert back.public()["deck"] == "Talk.pptx" and back.public()["slides_auto"] is True
    old = tmp_path / "old"
    (old / "x").mkdir(parents=True)
    import json
    d = json.loads((tmp_path / s.id / "session.json").read_text())
    for k in ("pptx", "slides_auto"):
        d.pop(k)
    (old / "x" / "session.json").write_text(json.dumps(d))
    older = Session.load(old, "x")
    assert older.pptx is None and older.slides_auto is False and older.public()["deck"] is None


# -- the hub: what gets slid, and when ---------------------------------------------------------

import hark.hub as hub_mod  # noqa: E402
from hark.hub import Hub  # noqa: E402


class Sent:
    def __init__(self):
        self.msgs = []

    async def send_str(self, s):
        import json
        self.msgs.append(json.loads(s))

    def toasts(self):
        return [m["text"] for m in self.msgs if m["type"] == "toast"]


class FakeDeck:
    """Stands in for the open deck: records slides, or says why it couldn't add one."""

    def __init__(self, note=None, available=True):
        self.available, self.note, self.added = available, note, []

    async def add(self, linked, slide):
        if self.note is None:
            self.added.append(slide)
        return self.note


def slide_hub(tmp_path, deck=None, local=False):
    research = SimpleNamespace(available=True, collections=lambda: [], find=lambda n: None)
    hub = Hub(SimpleNamespace(backlog=0.0, asr_name="x"), SimpleNamespace(available=True, model="fake"), tmp_path,
              researcher=research, deck=deck or FakeDeck())
    hub.session.mode = "outline"
    hub.session.set_doc(OUTLINE, by="claude")
    hub.session.pptx = str(tmp_path / "Talk.pptx")
    ws = Sent()
    hub.clients[ws] = SimpleNamespace(editing=False, streams={}, local=local)
    return hub, ws


async def settle(hub):
    while hub._tasks:
        await asyncio.gather(*list(hub._tasks))


def say(hub, text, ago=0.0):
    async def go():
        s = hub.session
        line = s.add_line(time.time(), "mic", "S1", text)
        await hub._screen_request(s, line)
        await settle(hub)
        line["added"] = time.time() - 3
        return line

    return asyncio.run(go())


def test_voice_slides_the_held_section_with_no_claude_call(tmp_path):
    hub, ws = slide_hub(tmp_path)
    hub.session.stage = "## Methods"
    line = say(hub, "Claude, put this on a slide.")
    assert [x["title"] for x in hub.deck.added] == ["Methods"]
    assert line.get("screen") and "✓ Added Methods to slides" in ws.toasts()
    hub.last_call = time.time()
    assert not hub._due()  # handled, so it doesn't jump the queue for a Claude call
    say(hub, "Claude, put sampling on a slide.")
    assert hub.deck.added[-1]["title"] == "Sampling"


def test_a_pop_up_answer_is_slid_for_30_seconds_after_it_goes(tmp_path, monkeypatch):
    hub, ws = slide_hub(tmp_path)
    s = hub.session
    s.stage = "## Methods"
    now = [1000.0]
    monkeypatch.setattr(hub_mod.time, "time", lambda: now[0])
    asyncio.run(hub._answered(s, {"answer": {"question": "What's left?", "text": "Two aims are still open."}}))
    up = hub.popup[2] - now[0]  # how long the room screen shows it
    assert 40 <= up <= 90
    now[0] += up + 29
    say(hub, "Claude, put that on a slide.")
    assert hub.deck.added[-1]["title"] == "What's left?"
    assert s.research[-1]["slide"]
    now[0] += 2  # gone more than 30 s ago: the section on screen instead
    say(hub, "Claude, put that on a slide.")
    assert hub.deck.added[-1]["title"] == "Methods"


def test_sources_and_collection_results_are_added_automatically_only_when_asked(tmp_path):
    hub, ws = slide_hub(tmp_path)
    s = hub.session

    async def run(q, title, kind, corpus):
        return {"answer": "An answer.", "items": []}, 1.0

    hub.researcher.run = run

    async def finish(kind):
        job = s.add_research(f"a {kind} question", "typed", "queued", kind=kind)
        await hub._run_research(s, job)
        await settle(hub)
        return job

    asyncio.run(finish("sources"))
    assert hub.deck.added == []  # off by default
    asyncio.run(hub.handle(ws, {"type": "slides_auto", "on": True}))
    assert s.slides_auto
    for kind in ("quick", "meeting"):
        asyncio.run(finish(kind))
    assert hub.deck.added == []  # never automatic for quick and meeting answers
    job = asyncio.run(finish("sources"))
    assert [x["title"] for x in hub.deck.added] == ["a sources question"] and job["slide"]


def test_add_to_slides_works_on_any_card_and_says_when_the_deck_is_closed(tmp_path):
    hub, ws = slide_hub(tmp_path, FakeDeck(NOT_OPEN))
    s = hub.session
    job = s.add_research("What is rebound?", "typed", "done", kind="quick")
    job["answer"] = "It's when savings are spent."

    async def add():
        await hub.handle(ws, {"type": "slides_add", "id": job["id"]})
        await settle(hub)

    asyncio.run(add())
    assert job["slides_note"] == NOT_OPEN and "slide" not in job
    assert ws.msgs[-2]["type"] == "research" and ws.msgs[-2]["job"]["slides_note"] == NOT_OPEN
    hub.deck.note = None  # the deck is opened
    asyncio.run(add())
    assert job["slide"] and "slides_note" not in job and hub.deck.added[-1]["title"] == "What is rebound?"


def test_claude_slides_a_vague_request_but_not_one_hark_did(tmp_path):
    hub, ws = slide_hub(tmp_path)
    s = hub.session
    prompts = []

    async def edit(prompt, timeout=120):
        prompts.append(prompt)
        return {"ops": [], "slide": "## Methods"}, 1.0

    hub.scribe.edit = edit

    async def update(text):
        line = s.add_line(time.time(), "mic", "S1", text)
        await hub._screen_request(s, line)
        await settle(hub)
        await hub._update()
        await settle(hub)

    asyncio.run(update("Claude, put the bit on how we did it on a slide."))
    assert [x["title"] for x in hub.deck.added] == ["Methods"]
    assert "<slide_deck/>" in prompts[0]
    s.stage = "## Methods"
    asyncio.run(update("Claude, put this on a slide."))
    assert len(hub.deck.added) == 2  # hark did it; Claude's answer doesn't add it again
    assert "[screen request: done by hark]" in prompts[1]


def test_without_powerpoint_there_is_nothing_to_slide(tmp_path):
    hub, ws = slide_hub(tmp_path, FakeDeck(available=False))
    hub.session.stage = "## Methods"
    line = say(hub, "Claude, put this on a slide.")
    assert not line.get("screen") and hub.deck.added == []
    asyncio.run(hub.push_state(ws))
    state = next(m for m in ws.msgs if m["type"] == "state")
    assert state["slides_available"] is False and state["can_link"] is False


def test_only_this_computer_may_link_a_deck(tmp_path):
    picked = []

    class Picker(FakeDeck):
        async def pick(self, start):
            picked.append(start)
            return str(tmp_path / "New.pptx")

    for local in (False, True):
        hub, ws = slide_hub(tmp_path, Picker(), local=local)

        async def go():
            await hub.push_state(ws)
            await hub.handle(ws, {"type": "slides_link"})
            await settle(hub)

        asyncio.run(go())
        state = next(m for m in ws.msgs if m["type"] == "state")
        assert state["can_link"] is local
    assert len(picked) == 1 and hub.session.pptx == str(tmp_path / "New.pptx")
    assert any("Linked New.pptx" in t for t in ws.toasts())
