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
- Discussion: one ### sub-heading per topic or agenda item, with short bullets of the substance (facts, figures, positions, reasons). Not a transcript.
- Decisions: things the group actually agreed.
- Actions: `- [ ] **Owner**: task (due date if one was given)`.
- Open questions: unresolved issues and things someone said they would check.

When something said earlier is corrected ("it's 43, not 42"), fix the existing text instead of adding a new bullet. Drop small talk and procedure. Name people only where it matters (who owns an action, who disagreed).""",
    },
    "outline": {
        "label": "Co-writing",
        "interval": 5,
        "template": "# Untitled\n",
        "brief": """The speakers are writing something together out loud (for example, outlining a paper). Build the document they are dictating. Do not take minutes of the conversation.

- Use ## for sections, ### for subsections and nested bullets for points. Put the working title in the H1 once it is stated.
- Write each point as a compact, writerly note of what they said, not who said it.
- Spoken editing commands are instructions to you, not content: "put that in the methods", "move X after Y", "scratch that", "rename the section", "no, I meant...". Apply them and do not record them. Work out what "that" or "it" refers to from the recent transcript.
- When someone corrects themselves, keep only the corrected version.
- Mark tentative or disputed points with a trailing "(?)".
- Do not add content nobody said. You may tidy wording and fix obvious mis-transcriptions.""",
    },
    "notes": {
        "label": "Notes",
        "interval": 15,
        "template": "# Notes\n",
        "brief": """Keep concise, well-organised notes of the conversation's content. Group points under ## headings by topic, and create or rename headings as the topics become clear. Prefer specifics (names, numbers, dates, references) over generalities.""",
    },
}

DEFAULT_MODE = "meeting"
