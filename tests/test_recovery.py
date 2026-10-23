import asyncio
import json

from hark.rebuild import chunks, parse_hark, parse_vtt, rebuild
from hark.session import Session, read_versions

VTT = """WEBVTT

0f3e7a1c-1/12-0
00:00:03.120 --> 00:00:05.450
<v Ana López>We agreed to ship</v>

0f3e7a1c-1/13-0
00:00:05.500 --> 00:00:07.000
<v Ana López>on Friday.</v>

0f3e7a1c-1/20-0
00:01:02.000 --> 00:01:04.000
<v Tom O&apos;Neill>I'll send the draft.</v>
"""


def test_parse_vtt_merges_a_speakers_cues_and_unescapes():
    lines = parse_vtt(VTT)
    assert [(l["speaker"], l["text"]) for l in lines] == [
        ("Ana López", "We agreed to ship on Friday."),
        ("Tom O'Neill", "I'll send the draft."),
    ]
    assert lines[0]["t"] == 3.12 and lines[1]["t"] == 62.0


def test_parse_hark_transcript_keeps_typed_instructions():
    text = "# Transcript: Budget\n\n[10:00:05] Speaker 1: Hello there\n(typed) [10:00:09] You (typed): make Actions a table\n"
    lines = parse_hark(text)
    assert lines[0] == {"t": 36005, "speaker": "Speaker 1", "text": "Hello there", "kind": "speech"}
    assert lines[1]["kind"] == "typed" and lines[1]["text"] == "make Actions a table"


def test_chunks_split_by_size_and_after_typed():
    lines = [{"text": "x" * 1000, "kind": "speech"}] * 5 + [{"text": "do it", "kind": "typed"}, {"text": "y", "kind": "speech"}]
    assert chunks(lines, 2500) == [(0, 3), (3, 6), (6, 7)]


def test_versions_log_lets_undo_survive_reopening(tmp_path):
    s = Session(tmp_path)
    s.set_doc("# A\n", by="claude")
    s.set_doc("# B\n")
    s.set_doc("# C\n", by="claude")
    s.undo()
    s.save()
    by = [e["by"] for e in read_versions(s.dir / "versions.jsonl")]
    assert by == ["start", "claude", "you", "claude", "undo"]

    r = Session.load(tmp_path, s.id)
    assert r.doc == "# B\n"
    assert r.undo() and r.doc == "# A\n"
    r.set_doc("# D\n")
    r.save()
    assert [e["by"] for e in read_versions(s.dir / "versions.jsonl")][-2:] == ["undo", "you"]  # no second "start"


def test_half_written_last_version_is_ignored(tmp_path):
    s = Session(tmp_path)
    s.set_doc("# A\n")
    s.save()
    with open(s.dir / "versions.jsonl", "a") as f:
        f.write('{"v": 9, "doc": "# trunc')
    assert Session.load(tmp_path, s.id).doc == "# A\n"


class FakeScribe:
    def __init__(self):
        self.prompts = []

    async def edit(self, prompt, timeout=120):
        self.prompts.append(prompt)
        return {"ops": [{"op": "append_to_section", "heading": "## Decisions", "text": f"- point {len(self.prompts)}"}]}, 0.1


def test_rebuild_from_vtt_makes_a_new_session(tmp_path):
    src = tmp_path / "meeting.vtt"
    src.write_text(VTT, encoding="utf-8")
    scribe = FakeScribe()
    s = asyncio.run(rebuild(src, tmp_path / "data", "meeting", scribe, say=lambda *_: None))
    assert "- point 1" in s.doc and s.processed == len(s.lines) == 2
    assert "Ana López: We agreed to ship on Friday." in scribe.prompts[0]
    saved = json.loads((s.dir / "session.json").read_text())
    assert saved["processed"] == 2 and (s.dir / "versions.jsonl").exists()
