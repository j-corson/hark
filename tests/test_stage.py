import asyncio
from types import SimpleNamespace

from hark.docops import find_heading
from hark.hub import Hub
from hark.scribe import build_prompt

DOC = """# Paper

## Introduction
- Why it matters

## Methods
- Interviews
### Sampling
- By tariff

## Items
### 3. Ofgem responses
#### Open questions
### 4. Smart meter data
#### Open questions
"""


def test_find_heading_exact_loose_and_renumbered():
    assert find_heading(DOC, "## Methods") == "## Methods"
    assert find_heading(DOC, "methods") == "## Methods"
    assert find_heading(DOC, "### 5. Ofgem responses") == "### 3. Ofgem responses"  # renumbered
    assert find_heading(DOC, "## **Introduction**") == "## Introduction"


def test_find_heading_refuses_missing_ambiguous_and_wrong_levels():
    assert find_heading(DOC, "## Results") is None
    assert find_heading(DOC, "# Paper") is None  # the whole document is not a section to hold
    assert find_heading(DOC, "Open questions") is None  # level 4, and one under every item
    assert find_heading(DOC, "") is None and find_heading(DOC, None) is None


def test_prompt_says_what_is_on_screen():
    p = build_prompt("brief", DOC, [], [], ["[10:00:00] Mike: add one on ethics"], [], on_screen="## Methods")
    assert "<on_screen>\n## Methods\n</on_screen>" in p
    assert "<on_screen>" not in build_prompt("brief", DOC, [], [], [], [])


class Sent:
    def __init__(self):
        self.msgs = []

    async def send_str(self, s):
        import json
        self.msgs.append(json.loads(s))


def make_hub(tmp_path):
    hub = Hub(SimpleNamespace(backlog=0.0, asr_name="x"), SimpleNamespace(available=True, model="fake"), tmp_path,
              researcher=SimpleNamespace(available=False))
    hub.session.mode = "outline"
    hub.session.set_doc(DOC, by="claude")
    ws = Sent()
    hub.clients[ws] = SimpleNamespace(editing=False, streams={})
    return hub, ws


def test_screen_choice_is_shared_and_follows_renumbering(tmp_path):
    hub, ws = make_hub(tmp_path)

    async def go():
        await hub.handle(ws, {"type": "stage", "heading": "### 3. Ofgem responses"})
        assert hub.session.stage == "### 3. Ofgem responses"
        assert ws.msgs[-1] == {"type": "stage", "heading": "### 3. Ofgem responses", "by": "you"}
        hub.session.set_doc(hub.session.doc.replace("### 3. Ofgem", "### 2. Ofgem"))
        await hub.push_doc()
        assert hub.session.stage == "### 2. Ofgem responses" and ws.msgs[-1]["stage"] == "### 2. Ofgem responses"
        await hub.handle(ws, {"type": "stage", "heading": "## Nowhere"})
        assert hub.session.stage == "### 2. Ofgem responses"  # unchanged, and the screen is told so
        assert ws.msgs[-1]["heading"] == "### 2. Ofgem responses"
        await hub.handle(ws, {"type": "stage", "heading": None})
        assert hub.session.stage is None

    asyncio.run(go())


def test_claude_can_show_a_section_it_just_created(tmp_path):
    hub, ws = make_hub(tmp_path)
    s = hub.session
    data = {"ops": [{"op": "append_to_section", "heading": "## Ethics", "text": "- Consent forms"}],
            "show": "## Ethics", "ack": "Added Ethics and showing it"}

    async def go():
        hub.held = (s, len(s.lines), data, 1.0, s.doc)
        await hub._maybe_release_held()
        assert s.stage == "## Ethics"
        hub.held = (s, len(s.lines), {"ops": [], "show": ""}, 1.0, s.doc)
        await hub._maybe_release_held()
        assert s.stage is None

    asyncio.run(go())


# -- switching the screen locally, without a Claude call ------------------------------

from hark.screen import screen_request  # noqa: E402

MEETING = DOC + "\n## Actions\n- [ ] Sam: draft\n\n## Data sources\n- SERL\n"


def test_clear_requests_switch_locally():
    assert screen_request("Okay Claude, let's look at the methods.", DOC) == ("show", "## Methods")
    assert screen_request("Claude, can you show us the intro please", DOC) == ("show", "## Introduction")
    assert screen_request("Claude show item three", DOC) == ("show", "### 3. Ofgem responses")
    assert screen_request("Claude, go back to item 4.", DOC) == ("show", "### 4. Smart meter data")
    assert screen_request("Claude, put the actions up.", MEETING) == ("show", "## Actions")
    assert screen_request("Claude, go to Ofgem responses", DOC) == ("show", "### 3. Ofgem responses")
    assert screen_request("Claude, follow along.", DOC) == ("follow", None)
    assert screen_request("show sampling", DOC, need_wake=False) == ("show", "### Sampling")  # typed


def test_anything_unclear_is_left_to_claude():
    assert screen_request("We should look at the methods.", DOC) is None  # not addressed to Claude
    assert screen_request("Claude, look at the methods and add that we used SERL.", DOC) is None
    assert screen_request("Claude, show that in the methods.", DOC) is None
    assert screen_request("Claude, go to the next item.", DOC) is None
    assert screen_request("Claude, follow up on that.", DOC) is None
    assert screen_request("Claude, show the data.", MEETING) is None  # two sections mention data
    assert screen_request("Claude, show item nine.", DOC) is None
    assert screen_request("Claude, show the results.", DOC) is None  # no such section: Claude may create it


def test_spoken_request_switches_at_once_and_claude_is_told(tmp_path):
    import time

    hub, ws = make_hub(tmp_path)
    s = hub.session

    async def go():
        a = s.add_line(time.time(), "mic", "S1", "Claude,")
        await hub._screen_request(s, a)
        b = s.add_line(time.time(), "mic", "S1", "let's look at the methods.")
        await hub._screen_request(s, b)
        return a, b

    a, b = asyncio.run(go())
    assert s.stage == "## Methods" and a.get("screen") and b.get("screen")
    assert "Showing Methods on the room screen" in s.done[-1]
    a["added"] = b["added"] = time.time() - 3
    hub.last_call = time.time()
    assert not hub._due()  # handled, so it doesn't jump the queue for a Claude call
