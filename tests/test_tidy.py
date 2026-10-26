import asyncio
import time
from types import SimpleNamespace

from hark.docops import apply_ops, rename_in_doc
from hark.hub import WAKE, Hub, respelling

DOC = """# Interview questions

## Questions
- Q1: background
- Q1 (draft): why apply
- Q2: policy

## Logistics
- Online, 30 minutes

## Working notes
- Maybe swap Q4 and Q5
"""


def test_replace_section_restructures_in_place():
    out, failed = apply_ops(DOC, [{"op": "replace_section", "heading": "## Questions",
                                   "text": "## Questions\n### Q1\n- Background and why apply\n### Q2\n- Policy"}])
    assert not failed
    assert "Q1 (draft)" not in out
    assert "## Questions\n### Q1\n- Background and why apply\n### Q2\n- Policy\n\n## Logistics" in out


def test_replace_section_body_only_keeps_heading_and_empty_removes():
    out, _ = apply_ops(DOC, [{"op": "replace_section", "heading": "Logistics", "text": "- Online, 30 min, Teams"}])
    assert "## Logistics\n- Online, 30 min, Teams\n" in out
    out, _ = apply_ops(DOC, [{"op": "replace_section", "heading": "## Working notes", "text": ""}])
    assert "Working notes" not in out and out.endswith("- Online, 30 minutes\n")


def test_replace_section_never_replaces_the_whole_document():
    out, failed = apply_ops(DOC, [{"op": "replace_section", "heading": "# Interview questions", "text": "# Gone"}])
    assert failed and out == DOC


def test_new_sections_go_above_working_notes():
    out, _ = apply_ops(DOC, [{"op": "append_to_section", "heading": "## Actions", "text": "- [ ] Mike: share the questions"}])
    assert out.index("## Actions") < out.index("## Working notes")
    out, _ = apply_ops(DOC, [{"op": "replace_section", "heading": "## Scoring", "text": "## Scoring\n- Out of five"}])
    assert out.index("## Scoring") < out.index("## Working notes")


def test_section_rewrite_skipped_if_edited_by_hand_meanwhile():
    edited = DOC.replace("- Q2: policy", "- Q2: policy (my wording)")
    op = {"op": "replace_section", "heading": "## Questions", "text": "## Questions\n- Q1"}
    out, failed = apply_ops(edited, [op], base=DOC)
    assert failed and "(my wording)" in out
    # A hand edit elsewhere doesn't block it.
    elsewhere = DOC.replace("30 minutes", "half an hour")
    out, failed = apply_ops(elsewhere, [op], base=DOC)
    assert not failed and "half an hour" in out and "Q2: policy" not in out


def test_rename_in_doc_whole_words_only():
    doc = "- Speaker 3 asks Q1\n- Speaker 30 asks Q2\n- Claire's question; Claireville"
    assert rename_in_doc(doc, "Speaker 3", "Clare") == "- Clare asks Q1\n- Speaker 30 asks Q2\n- Claire's question; Claireville"
    assert rename_in_doc(doc, "Claire", "Clare").endswith("- Clare's question; Claireville")


def test_respelling_and_wake_word():
    assert respelling("Claire", "Clare") and not respelling("Claire", "Mike") and not respelling("Clare", "clare")
    assert WAKE.search("Okay, Claude, do that. Swap Q4 and Q5.")
    assert not WAKE.search("It's stored in the cloud")


class FakeScribe:
    available = True
    model = "fake"

    def __init__(self, document):
        self.document = document
        self.calls = 0

    async def rewrite(self, prompt):
        self.calls += 1
        await asyncio.sleep(0.01)
        return {"document": self.document, "summary": "Merged duplicate Q1s."}, 1.0


def make_hub(tmp_path, scribe):
    hub = Hub(SimpleNamespace(backlog=0.0, asr_name="x"), scribe, tmp_path, researcher=SimpleNamespace(available=False))
    hub.session.mode = "outline"
    hub.session.set_doc(DOC, by="claude")
    return hub


def test_tidy_applies_and_is_undoable(tmp_path):
    tidied = DOC.replace("- Q1 (draft): why apply\n", "")
    hub = make_hub(tmp_path, FakeScribe(tidied))
    hub.tidy_wanted = "asked"
    assert hub._tidy_due()
    asyncio.run(hub._tidy())
    assert hub.session.doc == tidied
    assert hub.session.undo() and hub.session.doc == DOC


def test_tidy_left_alone_if_document_changed_meanwhile(tmp_path):
    hub = make_hub(tmp_path, FakeScribe(DOC.replace("- Q1 (draft): why apply\n", "")))

    async def go():
        hub.tidy_wanted = "asked"
        task = asyncio.create_task(hub._tidy())
        await asyncio.sleep(0)
        hub.session.set_doc(DOC + "- typed by hand\n")
        await task

    asyncio.run(go())
    assert "typed by hand" in hub.session.doc and "Q1 (draft)" in hub.session.doc


def test_tidy_rejects_a_truncated_document(tmp_path):
    hub = make_hub(tmp_path, FakeScribe("# Interview questions\n"))
    hub.tidy_wanted = "asked"
    asyncio.run(hub._tidy())
    assert hub.session.doc == DOC and hub.claude["state"] == "error"


def test_stop_tidy_waits_for_last_lines_and_skips_short_docs(tmp_path):
    hub = make_hub(tmp_path, FakeScribe(DOC))
    hub.tidy_wanted, hub.tidy_at = "stop", time.time() - 1
    hub.session.add_line(time.time(), "mic", "S1", "one more thing")
    assert not hub._tidy_due()  # that line has to reach the document first
    hub.session.processed = len(hub.session.lines)
    assert hub._tidy_due()
    asyncio.run(hub._tidy())
    assert hub.scribe.calls == 0  # under TIDY_MIN_LINES: not worth a call


def test_wake_word_waits_for_the_end_of_the_sentence(tmp_path):
    hub = make_hub(tmp_path, FakeScribe(DOC))
    hub.last_call = time.time()  # the normal interval hasn't passed
    s = hub.session
    s.processed = len(s.lines)
    line = s.add_line(time.time(), "mic", "S1", "Q three, Claude, please update that to")
    line["added"] = time.time() - 2
    assert not hub._due()  # mid-sentence pause
    s.add_line(time.time(), "mic", "S1", "those follow-ups directly into the main question.")["added"] = time.time() - 1.5
    assert hub._due()
    s.processed = len(s.lines)
    s.add_line(time.time(), "mic", "S1", "Okay Claude swap those two")["added"] = time.time() - 4.5
    assert hub._due()  # no full stop, but a long enough pause


def test_prompt_lists_what_claude_already_did():
    from hark.scribe import build_prompt
    p = build_prompt("brief", "# Doc", [], ["[14:56:06] Mike: Claude, swap Q4 and Q5."], ["[14:56:13] Mike: Now take the questions"], [],
                     done=["[14:56:10] Swapped Q4 and Q5"])
    assert "<done>\n- [14:56:10] Swapped Q4 and Q5\n</done>" in p
    assert p.index("<done>") < p.index("<new_transcript>")
