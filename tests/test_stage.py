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
              researcher=SimpleNamespace(available=False, collections=lambda: [], find=lambda n: None))
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


# -- the screen's section list, next / previous, and numbers ---------------------------

from hark.screen import screen_sections  # noqa: E402

OUTLINE = """# Paper

## Introduction
- Why

## Methods
### Sampling
- By tariff

## Results

## Working notes
- maybe a policy box
"""


def test_section_list_per_mode():
    assert screen_sections(OUTLINE, "outline") == ["## Introduction", "## Methods", "## Results"]
    assert screen_sections(DOC, "grill") == ["### 3. Ofgem responses", "### 4. Smart meter data"]
    meeting = "# M\n\n## Agenda\n- [ ] a\n\n## Summary\n- s\n\n## Discussion\n### Budget\n- b\n### Travel\n\n## Decisions\n"
    assert screen_sections(meeting, "meeting") == ["## Summary", "### Budget", "### Travel"]


def test_next_previous_and_numbers():
    ask = lambda said, cur=None, mode="outline", doc=OUTLINE: screen_request(said, doc, mode=mode, current=cur)  # noqa: E731
    assert ask("Claude, next section.", "## Introduction") == ("show", "## Methods")
    assert ask("Claude, go to the next section", "## Methods") == ("show", "## Results")
    assert ask("Claude, next section", "## Results") is None  # already at the end
    assert ask("Claude, previous section", "## Methods") == ("show", "## Introduction")
    assert ask("Claude, go back a section", "## Results") == ("show", "## Methods")
    assert ask("Claude, next section") == ("show", "## Introduction")  # nothing on screen yet: start at the top
    assert ask("Claude, section three") == ("show", "## Results")
    assert ask("Claude, show section 2 please") == ("show", "## Methods")
    assert ask("Claude, section nine") is None
    # Grilling: "section 3" is item 3, and "next item" stays with Claude (it also marks the item ready).
    assert ask("Claude, section three", mode="grill", doc=DOC) == ("show", "### 3. Ofgem responses")
    assert ask("Claude, next section", "### 3. Ofgem responses", mode="grill", doc=DOC) == ("show", "### 4. Smart meter data")
    assert ask("Claude, next item", "### 3. Ofgem responses", mode="grill", doc=DOC) is None
    # Meetings: the screen lists the agenda, so numbers and next/previous are left to Claude.
    assert ask("Claude, next section", mode="meeting") is None and ask("Claude, section two", mode="meeting") is None


def test_next_section_steps_from_what_the_screen_shows(tmp_path):
    import time

    hub, ws = make_hub(tmp_path)
    s = hub.session
    s.set_doc(OUTLINE, by="claude")

    async def go():
        await hub.handle(ws, {"type": "showing", "heading": "## Methods"})  # following, not held
        line = s.add_line(time.time(), "mic", "S1", "Okay Claude, next section.")
        await hub._screen_request(s, line)

    asyncio.run(go())
    assert s.stage == "## Results"


# -- what speech recognition actually wrote in a real session ---------------------------

PAPER = """# Societal Immunity

## Introduction
## Review of Existing Attempts
## Key Functions and Mechanisms of the Immune System
## Application to Societal Immunity
## Novelty versus Existing Concepts
## Added Value Beyond Novelty
## Ways the Framing Might Mislead or Harm
## Critical Discussion
## Future Directions (?)
"""


def test_misheard_claude_and_bare_navigation():
    ask = lambda said, cur="## Critical Discussion": screen_request(said, PAPER, current=cur)  # noqa: E731
    assert ask("Or go to previous section.") == ("show", "## Ways the Framing Might Mislead or Harm")
    assert ask("Claw go to section three.") == ("show", "## Key Functions and Mechanisms of the Immune System")
    assert ask("Clark Section Six.") == ("show", "## Added Value Beyond Novelty")
    assert ask("Okay, section four.") == ("show", "## Application to Societal Immunity")
    assert ask("For next section.") == ("show", "## Future Directions (?)")
    assert ask("L go to next section.") == ("show", "## Future Directions (?)")
    assert ask("Claw, show future directions.") == ("show", "## Future Directions (?)")


def test_ordinary_talk_does_not_move_the_screen():
    ask = lambda said: screen_request(said, PAPER, current="## Introduction")  # noqa: E731
    assert ask("The next section should cover the ants.") is None
    assert ask("In section four we say something different.") is None
    assert ask("Let's look at the critical discussion.") is None  # naming a section needs "Claude"
    assert ask("Cloud storage is covered in section two.") is None
    assert ask("We follow along with the literature.") is None


def test_prompt_numbers_sections_like_the_screen():
    p = build_prompt("brief", PAPER, [], [], ["[10:00:00] Mike: Claude, section six"], [], sections=["## Introduction", "## Methods"])
    assert "<screen_sections>\n1. ## Introduction\n2. ## Methods\n</screen_sections>" in p


# -- a misheard "Claude" still gets the instant response ---------------------------------

from hark.screen import addressed  # noqa: E402


def test_misheard_name_counts_as_speaking_to_claude():
    assert addressed("Claude, swap Q4 and Q5.")
    assert addressed("Claw, swap Q4 and Q5.")
    assert addressed("Okay, Clark, add a bullet on limitations.")
    assert addressed("So yeah. Claw, move that to the methods.")  # opening a later sentence
    assert not addressed("We talked about the claw machine.")  # mid-sentence: just a word
    assert not addressed("It's stored in the cloud.")
    assert screen_request("So yeah. Claw, next section.", PAPER, current="## Introduction") == ("show", "## Review of Existing Attempts")


def test_misheard_instruction_triggers_an_update_at_once(tmp_path):
    import time

    hub, ws = make_hub(tmp_path)
    s = hub.session
    hub.last_call = time.time()  # the normal interval hasn't passed
    s.processed = len(s.lines)
    s.add_line(time.time(), "mic", "S1", "Claw, swap the first two sections.")["added"] = time.time() - 1.5
    assert hub._due()


def test_claude_does_not_repeat_a_screen_switch_hark_made(tmp_path):
    """"Next section" is relative: if Claude acted on it too, the screen would move twice."""
    import time

    hub, ws = make_hub(tmp_path)
    s = hub.session
    s.set_doc(OUTLINE, by="claude")
    prompts = []

    async def edit(prompt, timeout=120):
        prompts.append(prompt)
        return {"ops": [], "show": "## Results", "ack": "Showing Results"}, 1.0

    hub.scribe.edit = edit

    async def go():
        await hub.handle(ws, {"type": "stage", "heading": "## Introduction"})
        line = s.add_line(time.time(), "mic", "S1", "Claude, next section.")
        await hub._screen_request(s, line)
        assert s.stage == "## Methods"
        await hub._update()

    asyncio.run(go())
    assert s.stage == "## Methods"  # not moved on again to Results
    assert "next section.  [screen request: done by hark]" in prompts[0]


def test_claude_does_not_undo_a_switch_made_while_it_was_thinking(tmp_path):
    import time

    hub, ws = make_hub(tmp_path)
    s = hub.session
    s.set_doc(OUTLINE, by="claude")

    async def edit(prompt, timeout=120):
        await hub.handle(ws, {"type": "stage", "heading": "## Results"})  # someone clicks meanwhile
        return {"ops": [], "show": "## Methods"}, 1.0

    hub.scribe.edit = edit

    async def go():
        s.add_line(time.time(), "mic", "S1", "Claude, let's have a look at the bit on methods sometime.")
        await hub._update()

    asyncio.run(go())
    assert s.stage == "## Results"
