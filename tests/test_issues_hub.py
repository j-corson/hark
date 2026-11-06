import asyncio
import json
from types import SimpleNamespace

import pytest

from hark import config, issues
from hark.cli import issue_plan
from hark.issues import GH_NEEDED
from hark.hub import Hub
from hark.issues import GhError
from hark.session import Session

BODY = "We want every Ofgem consultation response since 2010, to see how views on flexibility changed."


class FakeGh:
    def __init__(self, fail=None):
        self.issues = {("acme/research", 12): {"title": "Ofgem consultation responses", "body": BODY,
                                               "url": "https://github.com/acme/research/issues/12", "state": "OPEN"},
                       ("acme/research", 7): {"title": "Heat pump survey", "body": "A survey of 1,000 heat pump owners.",
                                              "url": "https://github.com/acme/research/issues/7", "state": "CLOSED"}}
        self.fail, self.comments, self.views, self.closed = fail, [], [], []

    async def available(self):
        return True

    async def list_open(self, repo, limit=50):
        return [{"number": n, "title": d["title"], "labels": ["idea"]} for (r, n), d in self.issues.items() if r == repo]

    async def view(self, repo, n):
        self.views.append((repo, n))
        if (repo, n) not in self.issues:
            raise GhError("GraphQL: Could not resolve to an issue")
        return dict(self.issues[(repo, n)])

    async def comment(self, repo, n, body):
        if self.fail:
            raise GhError(self.fail)
        self.comments.append((repo, n, body))
        return f"https://github.com/{repo}/issues/{n}#issuecomment-{len(self.comments)}"

    async def close(self, repo, n):
        self.closed.append((repo, n))


class FakeScribe:
    available = True
    model = "fake"

    def __init__(self, document=None, items=None):
        self.document, self.items, self.prompts = document, items, []
        self.verdict = {"ready": True, "gaps": [], "next_round": [], "resolution": "Every response since 2010, from the Ofgem site."}

    async def prepare(self, prompt):
        self.prompts.append(prompt)
        return {"document": self.document(prompt) if callable(self.document) else self.document, "summary": "1 item"}, 1.0

    async def prepare_items(self, prompt):
        self.prompts.append(prompt)
        return {"items": self.items, "summary": "1 item"}, 1.0

    async def check_close(self, prompt):
        self.prompts.append(prompt)
        return self.verdict, 1.0


class WS:
    def __init__(self):
        self.msgs = []

    async def send_str(self, s):
        self.msgs.append(json.loads(s))

    def of(self, kind):
        return [m for m in self.msgs if m["type"] == kind]


@pytest.fixture(autouse=True)
def own_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG", tmp_path / "config")


def make_hub(tmp_path, scribe=None, gh=None, ok=True):
    hub = Hub(SimpleNamespace(backlog=0.0, asr_name="x"), scribe or FakeScribe(), tmp_path / "data",
              researcher=SimpleNamespace(available=False, collections=lambda: [], find=lambda n: None), gh=gh or FakeGh())
    hub.gh_ok = ok
    return hub


def connect(hub):
    ws = WS()
    asyncio.run(hub.connect(ws))
    return ws


GRILL = """# Backlog

## Items

### 1. Smart meter data
- **Question**: half-hourly data for 1,000 homes
- **Status**: not started

## Shared terms

## Parked
"""


def test_start_with_an_issue_lays_it_out_and_preps_it(tmp_path):
    def prepared(prompt):
        assert BODY in prompt and "acme/research#12" in prompt
        # Claude renames the item and forgets the Issue field: it is put back.
        return ("# Ofgem backlog\n\n## Items\n\n### 1. Ofgem responses on flexibility\n- **Question**: how views on flexibility changed"
                "\n- **Status**: not started\n#### Open questions\n- **Q1. Years**: Which years? ➡️ Suggested: 2010 to now.\n\n"
                "## Shared terms\n\n## Parked\n")
    hub = make_hub(tmp_path, FakeScribe(prepared))
    fetched, errors = asyncio.run(hub.fetch_issues(["acme/research#12"]))
    assert errors == [] and fetched[0]["title"] == "Ofgem consultation responses"
    assert asyncio.run(hub.load_agenda("", "grill", issues=fetched))
    s = hub.session
    assert "### 1. Ofgem consultation responses\n- **Issue**: acme/research#12\n" in s.doc and hub._prep_due()
    asyncio.run(hub._prepare())
    assert "### 1. Ofgem responses on flexibility\n- **Issue**: acme/research#12\n- **Question**: how views" in s.doc
    assert issues.issue_items(s.doc) == {"acme/research#12": "### 1. Ofgem responses on flexibility"}


def test_agenda_dialog_adds_issues_once_and_remembers_the_repo(tmp_path):
    hub = make_hub(tmp_path, FakeScribe(GRILL))
    ws = connect(hub)
    asyncio.run(hub.handle(ws, {"type": "agenda", "text": "- Smart meter data", "mode": "grill", "repo": "acme/research",
                                "issues": ["#12", "https://github.com/acme/research/issues/12", "acme/research#7", "nonsense"]}))
    found = issues.issue_items(hub.session.doc)
    assert list(found) == ["acme/research#12", "acme/research#7"]
    assert found["acme/research#12"].startswith("### 2.") and hub.gh.views.count(("acme/research", 12)) == 1
    assert any("nonsense" in m["text"] for m in ws.of("toast"))
    assert config.issue_settings()["repo"] == "acme/research"
    assert hub.issues_public(hub.session)["repo"] == "acme/research"


def test_issue_list_for_the_picker(tmp_path):
    hub = make_hub(tmp_path)
    ws = connect(hub)
    asyncio.run(hub.handle(ws, {"type": "issues_list", "repo": "acme/research"}))
    [msg] = ws.of("issues_list")
    assert msg["repo"] == "acme/research" and [i["number"] for i in msg["items"]] == [12, 7] and not msg.get("error")
    asyncio.run(hub.handle(ws, {"type": "issues_list", "repo": "not a repo"}))
    assert ws.of("issues_list")[-1]["error"]


def test_mid_session_issue_is_appended_and_prepped_alone(tmp_path):
    section = ("### 9. Ofgem responses\n- **Question**: how supplier views changed\n- **Status**: not started\n"
               "#### Open questions\n- **Q1. Years**: Which years? ➡️ Suggested: 2010 to now.")
    hub = make_hub(tmp_path, FakeScribe(items=[{"ref": "acme/research#12", "section": section}]))
    hub.session.set_mode("grill")
    hub.session.set_doc(GRILL)
    ws = connect(hub)
    asyncio.run(hub.handle(ws, {"type": "issues_add", "refs": ["acme/research#12"], "repo": "acme/research"}))
    s = hub.session
    assert "### 2. Ofgem consultation responses\n- **Issue**: acme/research#12" in s.doc and hub._prep_due()
    other = issues.item_section(s.doc, "### 1. Smart meter data")
    asyncio.run(hub._prepare_items())
    assert issues.item_section(s.doc, "### 1. Smart meter data") == other
    sec = issues.item_section(s.doc, "### 2. Ofgem responses")
    assert sec.startswith("### 2. Ofgem responses\n- **Issue**: acme/research#12\n") and "Q1. Years" in sec
    assert s.doc.index("### 2.") < s.doc.index("## Shared terms")
    assert "Shared terms" not in hub.scribe.prompts[0].split("<new_items>")[1]  # only the new item is sent to prep
    # Adding it again does nothing.
    before = s.doc
    asyncio.run(hub.handle(ws, {"type": "issues_add", "refs": ["#12"], "repo": "acme/research"}))
    assert s.doc == before and any("already" in m["text"] for m in ws.of("toast"))


def test_issues_only_in_grilling_mode(tmp_path):
    hub = make_hub(tmp_path)
    ws = connect(hub)
    asyncio.run(hub.handle(ws, {"type": "issues_add", "refs": ["acme/research#12"]}))
    assert "Issue" not in hub.session.doc and hub.gh.views == []
    hub = make_hub(tmp_path, ok=False)
    hub.session.set_mode("grill")
    ws = connect(hub)
    asyncio.run(hub.handle(ws, {"type": "issues_add", "refs": ["acme/research#12"]}))
    assert hub.gh.views == [] and hub.issues_public(hub.session)["available"] is False


def started(tmp_path, gh=None, scribe=None):
    hub = make_hub(tmp_path, scribe or FakeScribe(GRILL), gh=gh)
    fetched, _ = asyncio.run(hub.fetch_issues(["acme/research#12", "acme/research#7"]))
    asyncio.run(hub.load_agenda("", "grill", issues=fetched))
    hub.prep = None
    return hub, connect(hub)


def test_post_preview_post_and_update(tmp_path):
    hub, ws = started(tmp_path)
    s = hub.session
    assert hub.issues_public(s)["items"]["acme/research#12"]["changed"]  # never posted: there is something to post
    asyncio.run(hub.handle(ws, {"type": "issue_preview", "ref": "acme/research#12"}))
    [pv] = ws.of("issue_preview")
    assert pv["body"].startswith("> *Drafted with hark") and pv["note"] == "Status: not started"
    asyncio.run(hub.handle(ws, {"type": "issue_post", "ref": "acme/research#12", "body": pv["body"] + "edited by hand"}))
    assert hub.gh.comments == [("acme/research", 12, pv["body"] + "edited by hand")]
    assert ws.of("issue_posted")[-1]["url"].endswith("#issuecomment-1")
    item = hub.issues_public(s)["items"]["acme/research#12"]
    assert not item["changed"] and item["url"].endswith("#issuecomment-1") and item["posts"] == 1
    assert s.issues["acme/research#12"]["posts"][0]["url"].endswith("#issuecomment-1")
    # Change the item: a second post lists what changed.
    s.set_doc(s.doc.replace("### 1. Ofgem consultation responses\n- **Issue**: acme/research#12\n- **Question**: ?",
                            "### 1. Ofgem consultation responses\n- **Issue**: acme/research#12\n- **Question**: how views changed"))
    assert hub.issues_public(s)["items"]["acme/research#12"]["changed"]
    asyncio.run(hub.handle(ws, {"type": "issue_preview", "ref": "acme/research#12"}))
    body = ws.of("issue_preview")[-1]["body"]
    assert body.split("\n\n")[1] == "**Since the last update**\n- Filled **Question**: how views changed"


def test_gh_error_posts_nothing(tmp_path):
    hub, ws = started(tmp_path, FakeGh(fail="HTTP 404: Not Found"))
    asyncio.run(hub.handle(ws, {"type": "issue_post", "ref": "acme/research#12", "body": "x"}))
    assert ws.of("issue_error")[-1] == {"type": "issue_error", "ref": "acme/research#12", "error": "HTTP 404: Not Found"}
    assert hub.session.issues == {} and hub.issues_public(hub.session)["items"]["acme/research#12"]["changed"]


def listen_then_stop(hub, ws):
    hub.clients[ws].streams["mic"] = SimpleNamespace(flush=lambda: [])  # no speech engine needed
    asyncio.run(hub.stop_source(ws, "mic"))


def test_stop_offers_changed_issue_items_and_posts_only_ticked(tmp_path):
    hub, ws = started(tmp_path)
    s = hub.session
    asyncio.run(hub.handle(ws, {"type": "issue_post", "ref": "acme/research#7", "body": "first"}))
    assert issues.offer(s.doc, s.issues) == ["acme/research#12"]  # #7 hasn't changed since it was posted
    listen_then_stop(hub, ws)
    [o] = ws.of("issues_offer")
    assert o["session"] == s.id and [i["ref"] for i in o["items"]] == ["acme/research#12"]
    asyncio.run(hub.handle(ws, {"type": "issues_post_many", "session": s.id, "posts": []}))  # nothing ticked
    assert len(hub.gh.comments) == 1
    asyncio.run(hub.handle(ws, {"type": "issues_post_many", "session": s.id,
                                "posts": [{"ref": "acme/research#12", "body": o["items"][0]["body"]}]}))
    assert [c[1] for c in hub.gh.comments] == [7, 12] and ws.of("issues_posted")[-1]["results"][0]["url"]
    # Nothing changed since: stopping again offers nothing.
    listen_then_stop(hub, ws)
    assert len(ws.of("issues_offer")) == 1


def test_switching_session_offers_and_can_still_post_to_the_old_one(tmp_path):
    hub, ws = started(tmp_path)
    old = hub.session
    old.id = "2026-01-01_090000"  # sessions are named by the second they start
    asyncio.run(hub.handle(ws, {"type": "new_session", "mode": "grill"}))
    [o] = ws.of("issues_offer")
    assert o["session"] == old.id and hub.session is not old
    asyncio.run(hub.handle(ws, {"type": "issues_post_many", "session": old.id,
                                "posts": [{"ref": "acme/research#7", "body": "update"}]}))
    assert hub.gh.comments == [("acme/research", 7, "update")]
    assert "acme/research#7" in Session.load(tmp_path / "data", old.id).issues


def test_claude_live_edit_keeps_the_issue_field(tmp_path):
    hub, ws = started(tmp_path)
    s = hub.session
    base = s.doc
    op = {"op": "replace", "find": "### 1. Ofgem consultation responses\n- **Issue**: acme/research#12",
          "text": "### 1. Ofgem consultation responses (2010 on)"}
    hub.held = (s, 0, {"ops": [op]}, 1.0, base)
    asyncio.run(hub._maybe_release_held())
    assert issues.issue_items(s.doc)["acme/research#12"] == "### 1. Ofgem consultation responses (2010 on)"
    # A hand edit that deletes the field is respected.
    s.set_doc(s.doc.replace("- **Issue**: acme/research#12\n", ""))
    assert "acme/research#12" not in issues.issue_items(s.doc)


def test_posts_persist_and_old_sessions_load(tmp_path):
    hub, ws = started(tmp_path)
    s = hub.session
    asyncio.run(hub.handle(ws, {"type": "issue_post", "ref": "acme/research#12", "body": "x"}))
    back = Session.load(tmp_path / "data", s.id)
    assert back.issues == s.issues and back.issues["acme/research#12"]["snapshot"].startswith("### 1. Ofgem")
    raw = json.loads((s.dir / "session.json").read_text(encoding="utf-8"))
    del raw["issues"]
    (s.dir / "session.json").write_text(json.dumps(raw), encoding="utf-8")
    assert Session.load(tmp_path / "data", s.id).issues == {}


def test_cli_issue_plan():
    assert issue_plan([], None, True) == ("meeting", [], [])
    assert issue_plan(["acme/research#12"], None, True) == ("grill", ["acme/research#12"], [])
    assert issue_plan(["acme/research#12"], "grill", True) == ("grill", ["acme/research#12"], [])
    mode, refs, warn = issue_plan(["acme/research#12"], "meeting", True)
    assert (mode, refs) == ("meeting", []) and "--mode grill" in warn[0]
    mode, refs, warn = issue_plan(["acme/research#12"], None, False)
    assert (mode, refs, warn) == ("meeting", [], [GH_NEEDED])
    assert GH_NEEDED == "GitHub issues need the GitHub CLI, logged in (`gh auth login`)"
    mode, refs, warn = issue_plan(["12", "https://github.com/acme/research/issues/3"], None, True)
    assert refs == ["acme/research#3"] and "12" in warn[0]


def test_config_keeps_issue_settings_across_setup():
    assert config.issue_settings() == {"repo": "", "disclaimer": True}
    config.save_issues_repo("acme/research")
    config.save({"provider": "claude-code", "model": "sonnet"})
    assert config.issue_settings()["repo"] == "acme/research"


def test_snapshot_is_the_item_as_previewed(tmp_path):
    hub, ws = started(tmp_path)
    s = hub.session
    asyncio.run(hub.handle(ws, {"type": "issue_preview", "ref": "acme/research#12"}))
    pv = ws.of("issue_preview")[-1]
    # Claude edits the item while the preview is open: that edit hasn't been posted, so it is still new.
    s.set_doc(s.doc.replace("- **Issue**: acme/research#12\n- **Question**: ?", "- **Issue**: acme/research#12\n- **Question**: later"))
    asyncio.run(hub.handle(ws, {"type": "issue_post", "ref": "acme/research#12", "body": pv["body"], "snapshot": pv["snapshot"]}))
    assert hub.issues_public(s)["items"]["acme/research#12"]["changed"]


ASKING = ("### 1. Ofgem consultation responses\n- **Issue**: acme/research#12\n- **Question**: how views changed\n"
          "- **Status**: being grilled\n#### Open questions\n- **Q1. Years**: Which years? ➡️ Suggested: 2010 to now.\n")
ANSWERED = ("### 1. Ofgem consultation responses\n- **Issue**: acme/research#12\n- **Question**: how views changed, 2010 to now\n"
            "- **Status**: being grilled\n#### Open questions\n")


def grilled(tmp_path, scribe=None, gh=None):
    """An issue item that was asked Q1, then had it answered."""
    hub, ws = started(tmp_path, gh, scribe)
    s = hub.session
    section = issues.item_section(s.doc, issues.issue_items(s.doc)["acme/research#12"])
    s.set_doc(s.doc.replace(section, ASKING))
    s.set_doc(s.doc.replace(ASKING, ANSWERED))
    return hub, ws, s


def test_close_is_offered_only_with_no_open_questions(tmp_path):
    hub, ws = started(tmp_path)
    s = hub.session
    section = issues.item_section(s.doc, issues.issue_items(s.doc)["acme/research#12"])
    s.set_doc(s.doc.replace(section, ASKING))
    asyncio.run(hub.handle(ws, {"type": "issue_preview", "ref": "acme/research#12"}))
    assert ws.of("issue_preview")[-1]["blockers"] == ["Q1. Years is still open"]
    # Asking anyway changes nothing and never calls Claude.
    asyncio.run(hub.handle(ws, {"type": "issue_close", "ref": "acme/research#12", "body": "x"}))
    assert ws.of("issue_unready")[-1]["gaps"] == ["Q1. Years is still open"]
    assert hub.gh.comments == [] and hub.gh.closed == [] and not hub.scribe.prompts


def test_post_and_close_after_claude_checks(tmp_path):
    hub, ws, s = grilled(tmp_path)
    asyncio.run(hub.handle(ws, {"type": "issue_preview", "ref": "acme/research#12"}))
    pv = ws.of("issue_preview")[-1]
    assert pv["blockers"] == [] and not pv["closed"]
    asyncio.run(hub.handle(ws, {"type": "issue_close", "ref": "acme/research#12", "body": pv["body"]}))
    prompt = hub.scribe.prompts[-1]
    assert "<asked>\n- **Q1. Years**: Which years?" in prompt and BODY in prompt  # the question asked earlier, and the issue
    assert "<next_label>Q2</next_label>" in prompt and "<grilling_skill>" in prompt
    assert ws.of("issue_checking") and ws.of("issue_posted")[-1]["closed"]
    [(_, _, body)] = hub.gh.comments
    assert body.startswith("> *Drafted with hark from a meeting discussion.*\n\n**Resolved**: Every response since 2010")
    assert "- **Status**: ready" in body and "being grilled" not in body
    assert hub.gh.closed == [("acme/research", 12)]
    assert "- **Status**: ready" in issues.item_section(s.doc, issues.issue_items(s.doc)["acme/research#12"])
    item = hub.issues_public(s)["items"]["acme/research#12"]
    assert item["closed"] and not item["changed"]


def test_claude_finding_gaps_posts_nothing(tmp_path):
    scribe = FakeScribe(GRILL)
    scribe.verdict = {"ready": False, "gaps": ["Q1: the years were never settled"], "resolution": ""}
    hub, ws, s = grilled(tmp_path, scribe)
    asyncio.run(hub.handle(ws, {"type": "issue_close", "ref": "acme/research#12", "body": "x"}))
    assert ws.of("issue_unready")[-1]["gaps"] == ["Q1: the years were never settled"]
    assert hub.gh.comments == [] and hub.gh.closed == [] and "being grilled" in s.doc


def test_a_next_round_goes_on_the_item_and_nothing_is_closed(tmp_path):
    scribe = FakeScribe(GRILL)
    scribe.verdict = {"ready": False, "gaps": [], "resolution": "",
                      "next_round": ["- **Q2. Which regions**: All of GB, or England only? ➡️ Suggested: all of GB.\n"
                                     "  - **A.** All of GB: more data\n  - **B.** England only: simpler",
                                     "not a question"]}
    hub, ws, s = grilled(tmp_path, scribe)
    asyncio.run(hub.handle(ws, {"type": "issue_close", "ref": "acme/research#12", "body": "x"}))
    assert ws.of("issue_unready")[-1] == {"type": "issue_unready", "ref": "acme/research#12", "gaps": [], "added": 1}
    section = issues.item_section(s.doc, issues.issue_items(s.doc)["acme/research#12"])
    assert section.endswith("#### Open questions\n- **Q2. Which regions**: All of GB, or England only? ➡️ Suggested: all of GB.\n"
                            "  - **A.** All of GB: more data\n  - **B.** England only: simpler")
    assert "not a question" not in s.doc and "being grilled" in section
    assert hub.gh.comments == [] and hub.gh.closed == []
    # The new round is open now, so the next try is stopped before Claude.
    asyncio.run(hub.handle(ws, {"type": "issue_close", "ref": "acme/research#12", "body": "x"}))
    assert ws.of("issue_unready")[-1]["gaps"] == ["Q2. Which regions is still open"] and len(scribe.prompts) == 1


def test_close_failing_after_the_post_says_so(tmp_path):
    class NoClose(FakeGh):
        async def close(self, repo, n):
            raise GhError("HTTP 403")
    hub, ws, s = grilled(tmp_path, gh=NoClose())
    asyncio.run(hub.handle(ws, {"type": "issue_close", "ref": "acme/research#12", "body": "x"}))
    msg = ws.of("issue_posted")[-1]
    assert msg["url"] and "wasn't closed: HTTP 403" in msg["error"] and not msg.get("closed")
    assert not s.issues["acme/research#12"].get("closed")
