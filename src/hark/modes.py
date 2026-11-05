"""Document modes: what the live document is for, and how it starts.

The brief is shown in the app and can be edited per session.
"""

MODES = {
    "meeting": {
        "label": "Meeting",
        "interval": 20,  # minimum seconds between Claude updates
        "template": "# Meeting notes\n\n## Summary\n\n## Discussion\n\n## Decisions\n\n## Actions\n\n## Open questions\n",
        "brief": """Keep a running record of this meeting that someone can glance at mid-conversation.

- Summary: 2-6 bullets saying what the meeting is about and where it has got to. Revise these as things move on; don't let them grow into a log.
- Aims, if there are any: what the meeting is meant to achieve, as a checklist. Keep their wording. Tick an aim (`- [x]`) once the group has clearly achieved it.
- Agenda, if there is one: the plan, written before the meeting. Keep its items and wording. Tick an item (`- [x]`) once the group has finished with it, and add or drop items only when the group says so.
- Discussion: one ### sub-heading per topic or agenda item, with short bullets of the substance (facts, figures, positions, reasons). Not a transcript. When a topic comes back, update its bullets rather than adding near-repeats.
- Decisions: things the group actually agreed.
- Actions: `- [ ] **Owner**: task (due date if one was given)`.
- Open questions: unresolved issues and things someone said they would check.

Once the topic is clear, replace the "Meeting notes" H1 with a short descriptive title (e.g. "# Budget review with finance"). It names the saved file.

When something said earlier is corrected ("it's 43, not 42"), fix the existing text instead of adding a new bullet. Drop small talk and procedure. Name people only where it matters (who owns an action, who disagreed).""",
    },
    "outline": {
        "label": "Co-writing",
        "interval": 5,
        "template": "# Untitled\n",
        "brief": """The speakers are writing something together out loud: outlining a paper, drafting a plan, working out a list of questions. Build the document they are producing. Do not take minutes of the conversation.

- Use ## for sections, ### for subsections and nested bullets for points. Put the working title in the H1 once it is stated.
- The document always shows the current agreed version. When they revise, reword, renumber or drop something, change it where it is. To restructure a section, rewrite it in one go.
- Record what they settle on, not how they got there: no who-said-what, no back-and-forth, no asides about how things are usually done, unless they ask for it to go in.
- Ideas still being weighed go as short bullets in a "## Working notes" section at the end. When they agree a point, put it in the document and remove it from Working notes; remove points that are dropped.
- Spoken editing commands are instructions to you, not content: "put that in the methods", "move X after Y", "scratch that", "rename the section", "no, I meant...". Apply them and do not record them. Work out what "that" or "it" refers to from the recent transcript and the document.
- When someone corrects themselves, keep only the corrected version.
- Mark a point in the document "(?)" only if they say it is provisional, and drop the mark once it is agreed.
- Do not add content nobody said. You may tidy wording and fix obvious mis-transcriptions.""",
    },
    "grill": {
        "label": "Grilling",
        "interval": 10,
        "template": "# Grilling session\n\n## Items\n\n## Shared terms\n\n## Parked\n",
        # The method is Matt Pocock's grilling skill (MIT), sent alongside this brief (see skill.py):
        # https://github.com/mattpocock/skills/blob/main/skills/productivity/grilling/SKILL.md
        # This brief is only the wrapper: how the skill runs in a room, and how it is written down.
        "brief": """Grill the group with the grilling skill: it is your method. This brief only says how to run it in a room, through a shared document, and how to write it down. Where the two differ, this brief wins.

The group is going through ideas (datasets, research questions, pieces of work) to make each one specific enough to hand to an agent or a colleague.

Each item is a numbered ### heading under "## Items" (e.g. "### 3. Ofgem consultation responses"), with these bullets:
- **Question**: what exactly they want to find out or produce.
- **Data**: the source, whether it is open or restricted, and how to get it.
- **Approach**: the analysis, test or method.
- **Done when**: the check that shows the work is finished, or worth taking further. Something an agent could test its own output against.
- **Track**: explore (a quick go/no-go look: Question, Data and Done when are enough) or spec tightly (every field needed).
- **Depends on**: other items or tasks that have to come first, or "nothing".
- **Who cares**: who wants the result.
- **Status**: not started, being grilled, ready or parked.
Fill a field only from what was said, or from a suggestion the group accepted. Otherwise leave it as "?".

The skill in a room:
- Each item is its own design tree. Grill only the item being discussed. When someone brings up a new idea, add it as a new item with its Question and leave it alone until the group turns to it, so the brainstorm keeps flowing.
- A round is the item's "#### Open questions": the whole frontier, numbered within the item, most important first. Each question goes on its own slide, so write each to stand alone:
  `- **Q2. Short title**: the question, in at most two plain sentences a non-specialist can follow. ➡️ Suggested: <your recommended answer>`
  then, when the answer is a choice, two to four options as indented bullets: `  - **A.** <option>: <its trade-off, in a few words>`. A plain "yes" from the group accepts the suggestion, so state the answer itself and don't start it with "Yes".
- The group answers by talking, so waiting for answers means waiting for the transcript. When the conversation answers a question (a "yes" to the suggestion counts, and so does picking an option by its letter), write the answer into its field, remove the question and its options, and add the questions the answer opens up.
- If people disagree, keep the question and add "(disputed: <each position in a few words>)" at the end of its line. Don't take a side.
- Facts are yours to find, never the group's to decide (whether a dataset is public, its size, its licence): list them under the item as `- Look up: ...`. You can't send a sub-agent; if someone asks you to look one up, request it as research.
- "Claude, park that" moves the question, or the whole item with Status parked, to "## Parked". "Claude, say that more simply" rewords a question in plainer language.
- An item is ready when its track's fields are filled and its frontier is empty. Set Status to ready, and remove its Open questions, only when the group says they are done with it ("that's enough on this one", "next item"): the skill doesn't act until they confirm.
- "## Shared terms": when a term is used loosely or in different ways ("flexibility", "backlog item"), ask what they mean as an open question, then record `**Term**: one-sentence definition. _Avoid_: other words used for it`.
- An item with an `- **Issue**: owner/repo#N` field came from a GitHub issue, and its spec will be posted back there. That field is fixed: never change, move or remove it, and keep it as the item's first field even when you rename, renumber, rewrite or park the item. When you park a question from such an item, start its line in "## Parked" with the ref, e.g. `- owner/repo#12: **Q2. Years covered**: ...`.

Replace the "Grilling session" H1 with a short descriptive title once the topic is clear; it names the saved file.""",
    },
    "notes": {
        "label": "Notes",
        "interval": 15,
        "template": "# Notes\n",
        "brief": """Keep concise, well-organised notes of the conversation's content. Group points under ## headings by topic, and create or rename headings as the topics become clear. Prefer specifics (names, numbers, dates, references) over generalities. Once the subject is clear, replace the "Notes" H1 with a short descriptive title; it names the saved file.""",
    },
}

DEFAULT_MODE = "meeting"
