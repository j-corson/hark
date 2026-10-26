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
    "notes": {
        "label": "Notes",
        "interval": 15,
        "template": "# Notes\n",
        "brief": """Keep concise, well-organised notes of the conversation's content. Group points under ## headings by topic, and create or rename headings as the topics become clear. Prefer specifics (names, numbers, dates, references) over generalities. Once the subject is clear, replace the "Notes" H1 with a short descriptive title; it names the saved file.""",
    },
}

DEFAULT_MODE = "meeting"
