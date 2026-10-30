import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

from hark import agenda, timing
from hark.hub import Hub, research_findings
from hark.scribe import build_prompt
from hark.screen import screen_sections
from hark.session import Session

NOON = datetime(2026, 10, 29, 12, 0).timestamp()  # "now" for the clock tests
at = lambda h, m=0: datetime(2026, 10, 29, h, m).timestamp()


def test_parse_clock():
    assert timing.parse_clock("11:30", NOON, exact=True) == at(11, 30)
    assert timing.parse_clock("15.00", NOON) == at(15) and timing.parse_clock("1530", NOON) == at(15, 30)
    assert timing.parse_clock("3pm", NOON) == at(15) and timing.parse_clock("11 am", NOON) == at(11)
    # No am/pm: the next time it comes round, allowing for running a little late.
    assert timing.parse_clock("4:30", NOON) == at(16, 30)
    assert timing.parse_clock("11:45", NOON) == at(11, 45)
    assert timing.parse_clock("4:30", NOON, exact=True) == at(4, 30)
    for bad in ("", "soon", "25:00", "13pm", "11:75"):
        assert timing.parse_clock(bad, NOON) is None


DOC = """# Budget catch-up

## Aims
- [x] Agree the split
- [ ] Pick a **venue**

## Agenda
- [x] Q3 numbers
  - [ ] a note, not an item
- [ ] Hiring
- [ ] AOB

## Discussion
- [ ] not on the agenda
"""


def test_what_is_still_open_and_the_reminder():
    assert timing.still_open(DOC) == (["Pick a **venue**"], ["Hiring", "AOB"])
    text = timing.reminder(DOC, at(11, 20), at(11, 30))
    assert text == "About 10 minutes left (ends 11:30). Aim not met yet: Pick a venue. Still on the agenda: Hiring; AOB."
    assert timing.reminder(DOC.replace("[ ]", "[x]"), at(11, 20), at(11, 30)) is None
    assert timing.reminder("# Notes\n\n- stuff\n", at(11, 20), at(11, 30)) is None  # nothing to check against


def test_clock_note():
    assert timing.clock_note(at(11, 15), None) == "Now 11:15."
    assert timing.clock_note(at(11, 15), at(11, 30)) == "Now 11:15. This session is due to end at 11:30: 15 minutes left."
    assert timing.clock_note(at(11, 31), at(11, 30)) == "Now 11:31. This session was due to end at 11:30, 1 minute ago."
    assert timing.clock_note(at(11, 0), at(23, 50)).endswith("12 hours 50 minutes left.")
    assert timing.minutes(2 * 3600) == "2 hours" and timing.minutes(91 * 60) == "1 hour 31 minutes"
    p = build_prompt("b", "# D", [], [], ["Ana: Claude, what's left?"], [], clock="Now 11:15.")
    assert "<clock>\nNow 11:15.\n</clock>" in p


AGENDA = """# Budget catch-up
Aims: Agree the split; Pick a venue
Ends: 11:30

- Q3 numbers
- Hiring
"""


def test_agenda_aims_and_end_time():
    rest, aims, ends = agenda.plan(AGENDA)
    assert aims == ["Agree the split", "Pick a venue"] and ends == "11:30" and "Aims" not in rest and "Ends" not in rest
    assert agenda.plan("## Aims\n- one\n- two\n## Agenda\n- x\n")[1] == ["one", "two"]
    assert agenda.plan("Aims:\n- one\n\n- x\n")[1:] == (["one"], "")
    # Items that merely start with the word aren't aims.
    assert agenda.plan("- Goals for next year\n- Ends of the project\n")[1:] == ([], "")
    doc = agenda.layout(rest, "meeting", aims)
    assert doc.index("## Summary") < doc.index("## Aims\n- [ ] Agree the split\n- [ ] Pick a venue") < doc.index("## Agenda") < doc.index("## Discussion")
    assert "## Agenda" not in agenda.layout("", "meeting", ["only aim"])
    # Aims are shown in the rail, not stepped through as a section.
    assert "## Aims" not in screen_sections(doc, "meeting")


def make_hub(tmp_path):
    hub = Hub(SimpleNamespace(backlog=0.0, asr_name="x"), SimpleNamespace(available=False, model="x"), tmp_path,
              researcher=SimpleNamespace(available=False, collections=lambda: [], find=lambda n: None))
    sent = []

    class WS:
        async def send_str(self, m):
            sent.append(json.loads(m))

    ws = WS()
    hub.clients[ws] = SimpleNamespace(editing=False, streams={})
    return hub, ws, sent


def test_agenda_from_the_dialog_sets_aims_end_and_reminder(tmp_path):
    hub, ws, sent = make_hub(tmp_path)
    asyncio.run(hub.handle(ws, {"type": "agenda", "text": "- Hiring", "mode": "meeting", "aims": ["Agree the split", ""],
                                "ends": "23:59", "nudge": True}))
    s = hub.session
    assert "## Aims\n- [ ] Agree the split\n\n## Agenda\n- [ ] Hiring" in s.doc
    assert timing.hhmm(s.ends) == "23:59" and s.nudge
    # Aims alone are enough to start from.
    asyncio.run(hub.handle(ws, {"type": "agenda", "text": "", "aims": ["Decide"]}))
    assert "## Aims\n- [ ] Decide" in hub.session.doc
    asyncio.run(hub.handle(ws, {"type": "agenda", "text": ""}))
    assert "No agenda items or aims" in sent[-1]["text"]


def test_meeting_time_dialog(tmp_path):
    hub, ws, sent = make_hub(tmp_path)
    s = hub.session
    asyncio.run(hub.handle(ws, {"type": "timing", "ends": "23:58", "nudge": True}))
    assert timing.hhmm(s.ends) == "23:58" and s.nudge and sent[-2]["session"]["ends"] == s.ends
    asyncio.run(hub.handle(ws, {"type": "timing", "ends": "half eleven"}))
    assert "wasn't a time" in sent[-1]["text"] and s.ends
    asyncio.run(hub.handle(ws, {"type": "timing", "ends": "", "nudge": False}))
    assert s.ends is None and not s.nudge


def test_claude_answers_questions_about_the_meeting(tmp_path):
    hub, ws, sent = make_hub(tmp_path)
    s = hub.session
    future = timing.hhmm(datetime.now().timestamp() + 3600)
    asyncio.run(hub._answered(s, {"answer": {"question": "What's left?", "text": "Hiring and AOB. Park AOB."}, "ends": future}))
    job = s.research[-1]
    assert job["kind"] == "meeting" and job["state"] == "done" and job["answer"] == "Hiring and AOB. Park AOB."
    assert timing.hhmm(s.ends) == future and any("ends at" in m.get("text", "") for m in sent)
    # Not fed back to Claude as findings, and the saved research says where it came from.
    assert research_findings(s) == []
    s.lines.append({"id": 1, "t": 0, "added": 0, "source": "mic", "speaker": "x", "text": "hi", "kind": "speech"})
    s.save()
    assert "_Answered from this session's notes and discussion._" in (s.dir / "research.md").read_text()
    assert Session.load(tmp_path, s.id).research[-1]["kind"] == "meeting"
    # An end time in the past (a mistake) is ignored; no answer, no card.
    n = len(s.research)
    asyncio.run(hub._answered(s, {"ends": "00:00"}))
    assert timing.hhmm(s.ends) == future and len(s.research) == n


def test_reminder_near_the_end(tmp_path, monkeypatch):
    hub, ws, sent = make_hub(tmp_path)
    s = hub.session
    s.set_doc(DOC)
    s.set_ends(at(11, 30), nudge=True)
    clock = {"now": at(11, 15)}
    monkeypatch.setattr(timing.time, "time", lambda: clock["now"])
    import hark.hub as hubmod
    monkeypatch.setattr(hubmod.time, "time", lambda: clock["now"])
    check = lambda: asyncio.run(hub._time_check())
    check()
    assert s.research == []  # too early
    clock["now"] = at(11, 21)
    check()
    assert s.research == []  # nobody listening
    hub.clients[ws].streams["mic"] = object()
    check()
    job = s.research[-1]
    assert job["kind"] == "meeting" and job["origin"] == "reminder" and job["label"] == "9 minutes left"
    assert "Pick a venue" in job["answer"] and "Hiring; AOB" in job["answer"]
    check()
    assert len(s.research) == 1  # only once
    s.set_ends(at(11, 45))  # a new end time gets its own reminder
    clock["now"] = at(11, 40)
    check()
    assert len(s.research) == 2
    # Off unless asked for.
    s.nudge, s.nudged = False, False
    check()
    assert len(s.research) == 2
