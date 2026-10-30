import asyncio

from hark import issues

DOC = """# Backlog grilling

## Items

### 1. Ofgem consultation responses
- **Issue**: acme/research#12
- **Question**: how supplier views on flexibility changed
- **Data**: ?
- **Track**: explore
- **Status**: being grilled
- every response since 2010?
- Look up: whether the archive is public
#### Open questions
- **Q1. Years covered**: Which years should the corpus cover? ➡️ Suggested: every response from 2010 to now.
- **Q2. Flexibility**: What does "flexibility" mean here? ➡️ Suggested: demand moving in time. (disputed: Ana says storage too; Ben says demand only)

### 2. Smart meter data
- **Question**: half-hourly data for 1,000 homes
- **Status**: not started

### 3. Heat pump survey
- **Issue**: acme/research#7
- **Question**: ?
- **Status**: ready

## Shared terms
- **Flexibility**: shifting demand in time. _Avoid_: DSR
- **Backlog item**: one idea on this list. _Avoid_: ticket

## Parked
- acme/research#12: **Q3. Licence**: Can we republish quotes? ➡️ Suggested: no.
- **Q1. Sample**: How many homes? (from item 2)
"""

HEAD = "### 1. Ofgem consultation responses"


def test_parse_ref_forms():
    assert issues.parse_ref("acme/research#12") == ("acme/research", 12)
    assert issues.parse_ref("  #12 ", "acme/research") == ("acme/research", 12)
    assert issues.parse_ref("12", "acme/research") == ("acme/research", 12)
    assert issues.parse_ref("https://github.com/acme/research/issues/12") == ("acme/research", 12)
    assert issues.parse_ref("github.com/acme/re.search-2/issues/12#issuecomment-1") == ("acme/re.search-2", 12)
    assert issues.parse_ref("acme/research#12", "other/repo") == ("acme/research", 12)


def test_parse_ref_rejects_bad_ones():
    for bad in ["", "#12", "12", "acme#12", "acme/research", "acme/research#0", "acme/research#x",
                "-R/x#1", "https://github.com/acme/research/pull/12", "https://evil.com/acme/research/issues/1",
                "acme/research#12; rm -rf /"]:
        assert issues.parse_ref(bad) is None, bad
    assert issues.parse_ref("#12", "not a repo") is None


def test_issue_items_and_item_section():
    found = issues.issue_items(DOC)
    assert found == {"acme/research#12": HEAD, "acme/research#7": "### 3. Heat pump survey"}
    sec = issues.item_section(DOC, HEAD)
    assert sec.startswith(HEAD + "\n- **Issue**") and "#### Open questions" in sec and "Q2. Flexibility" in sec
    assert "Smart meter" not in sec
    assert issues.item_section(DOC, "### 9. Nothing") == ""


def test_issue_items_follow_renamed_and_renumbered_items():
    moved = DOC.replace("### 1. Ofgem consultation responses", "### 4. Ofgem responses on flexibility")
    assert issues.issue_items(moved)["acme/research#12"] == "### 4. Ofgem responses on flexibility"
    # An item whose Issue field was deleted is no longer an issue item.
    gone = DOC.replace("- **Issue**: acme/research#7\n", "")
    assert list(issues.issue_items(gone)) == ["acme/research#12"]
    # A parked item (moved under ## Parked) is still one.
    parked = DOC.replace("## Parked\n", "## Parked\n\n### Old idea\n- **Issue**: acme/research#3\n- **Status**: parked\n")
    assert issues.issue_items(parked)["acme/research#3"] == "### Old idea"


def test_comment_first_post():
    body = issues.comment_body(DOC, HEAD, "Backlog grilling", "30 Oct 2026", None, True)
    assert body.startswith("> *Drafted with hark from a meeting discussion.*\n")
    assert "Since the last update" not in body
    assert "- **Question**: how supplier views on flexibility changed" in body
    assert "**Issue**" not in body
    assert "- every response since 2010?" in body
    assert "Q1. Years covered" in body and "➡️ Suggested: every response from 2010 to now." in body
    assert "(disputed: Ana says storage too; Ben says demand only)" in body
    assert "Q3. Licence" in body  # parked from this item
    assert "**Flexibility**: shifting demand" in body and "Backlog item" not in body  # only terms the item uses
    assert "- whether the archive is public" in body
    assert body.rstrip().endswith("_Backlog grilling, 30 Oct 2026_")
    # Nothing from other items leaks in.
    for other in ["Smart meter", "Heat pump", "Q1. Sample", "acme/research#7"]:
        assert other not in body


def test_comment_without_disclaimer_and_repeat_post():
    snap = issues.item_section(DOC, HEAD)
    later = DOC.replace("- **Data**: ?", "- **Data**: Ofgem website, open").replace(
        "- **Q1. Years covered**: Which years should the corpus cover? ➡️ Suggested: every response from 2010 to now.\n", "")
    body = issues.comment_body(later, HEAD, "Backlog grilling", "30 Oct 2026", snap, False)
    assert not body.startswith(">")
    assert body.startswith("**Since the last update**\n")
    since = body.split("\n\n")[0]
    assert "Filled **Data**: Ofgem website, open" in since and "Answered **Q1. Years covered**" in since


def test_changed_since():
    snap = issues.item_section(DOC, HEAD)
    assert issues.changed_since(snap, snap) == []
    # Renumbering the heading is not a change worth posting.
    assert issues.changed_since(snap, snap.replace("### 1.", "### 4.")) == []
    new = (snap.replace("- **Track**: explore", "- **Track**: spec tightly")
           .replace("(disputed: Ana says storage too; Ben says demand only)", "")
           .replace("every response from 2010 to now.", "every response from 2010 to now. (disputed: Ana: 2010; Ben: 2015)")
           + "\n- **Q3. Owner**: Who runs it? ➡️ Suggested: Ana.")
    lines = issues.changed_since(snap, new)
    assert lines == ["Changed **Track**: spec tightly", "Disputed **Q1. Years covered**: Ana: 2010; Ben: 2015",
                     "Settled the dispute on **Q2. Flexibility**", "Added **Q3. Owner**"]
    # A question that went to ## Parked is reported as parked, not answered.
    gone = snap.split("\n- **Q2. Flexibility**")[0]
    assert issues.changed_since(snap, gone, parked=["- **Q2. Flexibility**: ..."]) == ["Parked **Q2. Flexibility**"]
    assert issues.changed_since(snap, gone) == ["Answered **Q2. Flexibility**"]
    # Any other edit still counts as new.
    assert issues.changed_since(snap, snap.replace("every response since 2010?", "every response since 2012?")) != []


def test_post_is_enabled_only_when_the_item_changed():
    snap = issues.item_section(DOC, HEAD)
    assert issues.changed(None, snap)  # never posted
    assert not issues.changed(snap, snap)
    assert not issues.changed(snap, snap.replace("### 1.", "### 2.") + "\n")
    assert issues.changed(snap, snap.replace("- **Data**: ?", "- **Data**: open"))


def test_readiness_note():
    assert issues.readiness(issues.item_section(DOC, HEAD)) == "Status: being grilled, 2 open questions"
    assert issues.readiness(issues.item_section(DOC, "### 3. Heat pump survey")) is None


def test_keep_issue_fields_after_claude_drops_one():
    renamed = DOC.replace("### 1. Ofgem consultation responses\n- **Issue**: acme/research#12\n",
                          "### 2. Ofgem consultation responses\n")
    kept = issues.keep_issue_fields(DOC, renamed)
    assert issues.issue_items(kept)["acme/research#12"] == "### 2. Ofgem consultation responses"
    assert "### 2. Ofgem consultation responses\n- **Issue**: acme/research#12\n- **Question**" in kept
    assert issues.keep_issue_fields(DOC, DOC) == DOC


class FakeProc:
    def __init__(self, code=0, out=b"", err=b""):
        self.returncode, self.out, self.err, self.stdin_got = code, out, err, None

    async def communicate(self, data=None):
        self.stdin_got = data
        return self.out, self.err


def test_gh_runner_builds_argument_lists_and_reads_errors():
    calls = []

    def fake_exec(code=0, out=b"", err=b""):
        async def run(*argv, **kw):
            proc = FakeProc(code, out, err)
            calls.append((argv, proc))
            return proc
        return run

    gh = issues.GhRunner(exe="gh", spawn=fake_exec(out=b"https://github.com/acme/research/issues/12#issuecomment-9\n"))
    url = asyncio.run(gh.comment("acme/research", 12, "hello"))
    argv, proc = calls[-1]
    assert url.endswith("#issuecomment-9") and proc.stdin_got == b"hello"
    assert argv == ("gh", "issue", "comment", "12", "-R", "acme/research", "--body-file", "-")
    gh = issues.GhRunner(exe="gh", spawn=fake_exec(code=1, err=b"GraphQL: Could not resolve to an issue (repository.issue)\nmore\n"))
    try:
        asyncio.run(gh.view("acme/research", 99))
        raise AssertionError("expected GhError")
    except issues.GhError as e:
        assert str(e) == "GraphQL: Could not resolve to an issue (repository.issue)"
    try:
        asyncio.run(gh.list_open("-oops"))
        raise AssertionError("expected GhError")
    except issues.GhError:
        pass


def test_gh_runner_unavailable_without_gh():
    gh = issues.GhRunner(exe=None)
    assert asyncio.run(gh.available()) is False


def test_terms_without_a_bullet_and_short_titles_in_parked():
    doc = DOC.replace("- **Flexibility**: shifting", "**Flexibility**: shifting")
    assert "- **Flexibility**: shifting demand" in issues.comment_body(doc, HEAD, "T", "1 Jan 2026", None, False)
    short = "# T\n\n## Items\n\n### 1. Data\n- **Issue**: acme/r#1\n- **Status**: being grilled\n\n## Parked\n- **Q1. Sample**: Which data? (item 2)\n"
    assert issues.parked_for(short, "### 1. Data", "acme/r#1") == []
