import asyncio
from types import SimpleNamespace

from hark import agenda
from hark.hub import Hub
from hark.modes import MODES
from hark.scribe import ScribeError

AGENDA = """# Research backlog ideas

Bring one or two ideas each.

- Ofgem consultation responses: how supplier views on flexibility changed
  - every response since 2010?
  - who each response is from
- [ ] Half-hourly smart meter data, 1,000 homes
1. Survey of 1,000 people, 20 questions
"""


def test_parse_bullets_notes_and_context():
    title, context, items = agenda.parse(AGENDA)
    assert title == "Research backlog ideas"
    assert context == ["Bring one or two ideas each."]
    assert [i["text"] for i in items] == ["Ofgem consultation responses: how supplier views on flexibility changed",
                                          "Half-hourly smart meter data, 1,000 homes", "Survey of 1,000 people, 20 questions"]
    assert items[0]["notes"] == ["every response since 2010?", "who each response is from"]


def test_parse_headings_when_there_are_no_bullets_and_plain_lines():
    _, _, items = agenda.parse("## 1. Ofgem corpus\nAll responses.\n\n## Smart meters\n")
    assert [i["text"] for i in items] == ["Ofgem corpus", "Smart meters"] and items[0]["notes"] == ["All responses."]
    _, _, items = agenda.parse("Ofgem corpus\nSmart meters\n")
    assert [i["text"] for i in items] == ["Ofgem corpus", "Smart meters"]
    # With bullets as the items, headings only group them.
    _, _, items = agenda.parse("## Datasets\n- Ofgem corpus\n## Ideas\n- Rebound effect\n")
    assert [i["text"] for i in items] == ["Ofgem corpus", "Rebound effect"]


def test_meeting_layout_is_a_checklist_above_discussion():
    doc = agenda.layout(AGENDA, "meeting")
    assert doc.startswith("# Research backlog ideas\n\n## Summary\n")
    assert "## Agenda\nBring one or two ideas each.\n- [ ] Ofgem consultation" in doc
    assert "  - who each response is from\n- [ ] Half-hourly" in doc
    assert doc.index("## Agenda") < doc.index("## Discussion") < doc.index("## Actions")


def test_grill_layout_numbers_items_under_items():
    doc = agenda.layout(AGENDA, "grill")
    assert doc.startswith("# Research backlog ideas\n")
    assert "### 1. Ofgem consultation responses" in doc and "### 3. Survey of 1,000 people" in doc
    assert "- every response since 2010?" in doc
    assert doc.index("## Items") < doc.index("### 1.") < doc.index("## Shared terms") < doc.index("## Parked")


class FakeScribe:
    available = True
    model = "fake"

    def __init__(self, result=None, error=None):
        self.result, self.error, self.prompts = result, error, []

    async def prepare(self, prompt):
        self.prompts.append(prompt)
        await asyncio.sleep(0.01)
        if self.error:
            raise ScribeError(self.error)
        return self.result, 2.0


def make_hub(tmp_path, scribe):
    return Hub(SimpleNamespace(backlog=0.0, asr_name="x"), scribe, tmp_path, researcher=SimpleNamespace(available=False, collections=lambda: [], find=lambda n: None))


PREPARED = """# Research backlog ideas

## Items

### 1. Ofgem consultation responses
- **Question**: how supplier views on flexibility changed
- **Data**: ?
- **Status**: not started
#### Open questions
- **Q1. Which years**: Should we cover every response since 2010? ➡️ Suggested: yes, 2010 to now.

### 2. Half-hourly smart meter data, 1,000 homes
- **Question**: ?

### 3. Survey of 1,000 people, 20 questions
- **Question**: ?

## Shared terms

## Parked
"""


def test_grill_agenda_is_prepared_with_questions_and_research(tmp_path):
    research = [{"label": "Ofgem response archive", "question": "Are all Ofgem consultation responses since 2010 public?"}]
    hub = make_hub(tmp_path, FakeScribe({"document": PREPARED, "summary": "3 items, first questions drafted", "research": research}))
    assert asyncio.run(hub.load_agenda(AGENDA, "grill"))
    s = hub.session
    assert s.mode == "grill" and "### 1. Ofgem" in s.doc and hub._prep_due()
    asyncio.run(hub._prepare())
    assert "➡️ Suggested" in s.doc and hub.scribe.prompts[0].count("<agenda>") == 1
    assert [(j["origin"], j["state"]) for j in s.research] == [("agenda", "proposed")]  # suggested, not run
    assert s.undo() and "➡️" not in s.doc  # Undo goes back to the plain agenda


def test_prepared_document_kept_out_if_agenda_edited_meanwhile(tmp_path):
    hub = make_hub(tmp_path, FakeScribe({"document": PREPARED}))
    asyncio.run(hub.load_agenda(AGENDA, "grill"))

    async def go():
        task = asyncio.create_task(hub._prepare())
        await asyncio.sleep(0)
        hub.session.set_doc(hub.session.doc + "- added by hand\n")
        await task

    asyncio.run(go())
    assert "added by hand" in hub.session.doc and "➡️" not in hub.session.doc


def test_prepare_failure_or_truncation_leaves_the_agenda(tmp_path):
    hub = make_hub(tmp_path, FakeScribe(error="logged out"))
    asyncio.run(hub.load_agenda(AGENDA, "grill"))
    plain = hub.session.doc
    asyncio.run(hub._prepare())
    assert hub.session.doc == plain and hub.claude["state"] == "error" and not hub.busy
    hub = make_hub(tmp_path, FakeScribe({"document": "# Research backlog ideas\n"}))
    asyncio.run(hub.load_agenda(AGENDA, "grill"))
    asyncio.run(hub._prepare())
    assert "### 3. Survey" in hub.session.doc and hub.claude["state"] == "error"


def test_meeting_agenda_needs_no_claude_and_keeps_a_used_session(tmp_path):
    hub = make_hub(tmp_path, FakeScribe())
    first = hub.session
    first.add_line(0, "mic", "S1", "something already said")
    assert asyncio.run(hub.load_agenda(AGENDA, "meeting"))
    assert hub.session is not first and hub.session.mode == "meeting"
    assert "- [ ] Ofgem consultation" in hub.session.doc and hub.prep is None
    assert (tmp_path / first.id / "transcript.md").exists()  # the old session was saved, not replaced
    assert not asyncio.run(hub.load_agenda("   \n", "meeting"))


def test_grill_mode_is_offered():
    assert MODES["grill"]["label"] == "Grilling" and "Suggested" in MODES["grill"]["brief"]
