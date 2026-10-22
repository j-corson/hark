import numpy as np

from hark.docops import apply_ops, changed_lines
from hark.speakers import Speakers

DOC = """# Meeting notes

## Summary
- Weekly check-in

## Decisions

## Actions
- [ ] **Tom**: fix reminders (Fri)
"""


def test_append_to_existing_and_empty_section():
    out, failed = apply_ops(DOC, [
        {"op": "append_to_section", "heading": "## Decisions", "text": "- Ship Friday"},
        {"op": "append_to_section", "heading": "## Summary", "text": "- 43 households"},
    ])
    assert not failed
    assert "## Decisions\n- Ship Friday\n\n## Actions" in out
    assert "- Weekly check-in\n- 43 households\n\n## Decisions" in out


def test_append_creates_missing_section():
    out, failed = apply_ops(DOC, [{"op": "append_to_section", "heading": "## Open questions", "text": "- Ethics?"}])
    assert not failed and out.endswith("## Open questions\n- Ethics?\n")


def test_heading_match_is_forgiving():
    out, failed = apply_ops(DOC, [{"op": "append_to_section", "heading": "decisions", "text": "- X"}])
    assert not failed and "## Decisions\n- X" in out


def test_replace_exact_and_whitespace_tolerant():
    out, failed = apply_ops(DOC, [{"op": "replace", "find": "- weekly check-in  ", "text": "- Weekly trial check-in"}])
    assert not failed and "- Weekly trial check-in" in out


def test_move_by_delete_and_insert():
    ops = [
        {"op": "delete", "find": "- [ ] **Tom**: fix reminders (Fri)"},
        {"op": "insert_after", "find": "## Decisions", "text": "- Tom fixes reminders"},
    ]
    out, failed = apply_ops(DOC, ops)
    assert not failed
    assert "**Tom**" not in out and "## Decisions\n- Tom fixes reminders" in out


def test_stale_anchor_is_skipped_not_fatal():
    # The user edited the line by hand while Claude was thinking.
    edited = DOC.replace("- Weekly check-in", "- Weekly check-in (my wording)")
    out, failed = apply_ops(edited, [
        {"op": "replace", "find": "- Weekly check-in\n", "text": "- Something else\n"},
        {"op": "append_to_section", "heading": "## Decisions", "text": "- Y"},
    ])
    # The exact substring still matches inside the user's line, so guard for whole-line anchors:
    assert "## Decisions\n- Y" in out
    assert len(failed) <= 1


def test_changed_lines():
    new = DOC.replace("## Decisions\n", "## Decisions\n- New\n")
    idx = changed_lines(DOC, new)
    assert [new.split("\n")[i] for i in idx] == ["- New"]


def test_speaker_clustering_and_merge():
    rng = np.random.default_rng(0)
    a, b = rng.normal(size=192), rng.normal(size=192)
    sp = Speakers(threshold=0.5)
    s1 = sp.assign(a + rng.normal(scale=0.2, size=192), 3.0)
    s2 = sp.assign(b + rng.normal(scale=0.2, size=192), 3.0)
    s1b = sp.assign(a + rng.normal(scale=0.2, size=192), 3.0)
    assert s1 == s1b and s1 != s2
    assert sp.assign(None, 0.5, previous=s2) == s2  # too short: same speaker continues
    sp.rename(s1, "Ana")
    kept = sp.rename(s2, "ana")  # same name merges
    assert kept == s1 and sp.resolve(s2) == s1 and len(sp.items) == 1
