import asyncio
import json
import os
import subprocess
import zipfile
from types import SimpleNamespace

import pytest

from hark import config
from hark.backends import AnthropicAPI, ClaudeCode, ScribeError
from hark.corpus import Corpus, Library, docx_text, github_repo, make_spec, parse_spec, pptx_text
from hark.hub import Hub, research_findings
from hark.research import Researcher
from hark.scribe import Scribe, build_prompt
from hark.session import research_markdown

from test_backends import FakeAnthropic, message, text

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'


def make_docx(path, paras):
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paras)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f"<w:document {W}><w:body>{body}</w:body></w:document>")


def make_pptx(path, slides, notes=None):
    with zipfile.ZipFile(path, "w") as z:
        for i, lines in enumerate(slides, 1):
            ps = "".join(f"<a:p><a:r><a:t>{l}</a:t></a:r></a:p>" for l in lines)
            z.writestr(f"ppt/slides/slide{i}.xml", f"<p:sld {A} xmlns:p='p'><a:txBody>{ps}</a:txBody></p:sld>")
            if notes and i in notes:
                z.writestr(f"ppt/slides/_rels/slide{i}.xml.rels",
                           f'<Relationships><Relationship Type="x/notesSlide" Target="../notesSlides/notesSlide{i}.xml"/></Relationships>')
                z.writestr(f"ppt/notesSlides/notesSlide{i}.xml",
                           f"<p:notes {A} xmlns:p='p'><a:p><a:r><a:t>{notes[i]}</a:t></a:r></a:p><a:p><a:r><a:t>{i}</a:t></a:r></a:p></p:notes>")


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "project"
    (root / "Transcripts").mkdir(parents=True)
    (root / "notes.md").write_text("# Notes\nThe budget is tight.\nImmunity is a metaphor.\n")
    make_docx(root / "Transcripts" / "catch-up.docx", ["Ana 0:03", "We agreed immune memory matters most."])
    make_pptx(root / "deck.pptx", [["Reading list", "Eight core papers"], ["Second slide"]], notes={1: "Read these first"})
    (root / "image.png").write_bytes(b"\x89PNG\r\n\x00\x00binary")
    return root


def test_office_files_become_text(folder):
    assert docx_text(folder / "Transcripts" / "catch-up.docx") == "Ana 0:03\nWe agreed immune memory matters most.\n"
    deck = pptx_text(folder / "deck.pptx")
    assert deck.startswith("## Slide 1\nReading list\nEight core papers\nNotes: Read these first\n")
    assert "## Slide 2\nSecond slide" in deck and "Notes: Read these first 1" not in deck


def test_office_file_with_a_doctype_is_refused(tmp_path):
    p = tmp_path / "x.docx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("word/document.xml", '<!DOCTYPE x [<!ENTITY a "aaaa">]><w:document %s/>' % W)
    with pytest.raises(ValueError):
        docx_text(p)


def test_sync_copies_text_converts_documents_and_keeps_up(folder, tmp_path):
    c = Corpus("proj", folder, cache=tmp_path / "cache")
    assert c.sync() == {"files": 3, "changed": 3, "skipped": 1}  # the PNG isn't text
    assert c.files["Transcripts/catch-up.docx"] == "Transcripts/catch-up.docx.txt"
    assert "immune memory" in (c.mirror / "Transcripts/catch-up.docx.txt").read_text()
    assert c.sync()["changed"] == 0  # nothing re-read when nothing changed
    (folder / "notes.md").write_text("Budget now agreed.\n")
    (folder / "deck.pptx").unlink()
    assert c.sync()["changed"] == 1
    assert not (c.mirror / "deck.pptx.txt").exists() and "deck.pptx" not in c.files
    assert (c.mirror / "notes.md").read_text() == "Budget now agreed.\n"


def test_symlinks_and_hidden_folders_are_left_out(folder, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("password")
    os.symlink(secret, folder / "link.txt")
    (folder / ".private").mkdir()
    (folder / ".private" / "x.md").write_text("hidden")
    c = Corpus("proj", folder, cache=tmp_path / "cache")
    c.sync()
    assert "link.txt" not in c.files and not any(p.startswith(".private") for p in c.files)


def test_git_repo_respects_gitignore_and_links_to_github(folder, tmp_path):
    git = lambda *a: subprocess.run(["git", "-C", str(folder), *a], check=True, capture_output=True)
    git("init", "-b", "main")
    (folder / ".gitignore").write_text("ignored/\n")
    (folder / "ignored").mkdir()
    (folder / "ignored" / "big.md").write_text("skip me")
    git("add", "notes.md", ".gitignore")
    git("-c", "user.name=t", "-c", "user.email=t@e.org", "commit", "-qm", "first")
    git("remote", "add", "origin", "git@github.com:someone/proj.git")
    c = Corpus("proj", folder, cache=tmp_path / "cache")
    c.sync()
    assert "ignored/big.md" not in c.files and "Transcripts/catch-up.docx" in c.files  # untracked but not ignored
    assert c.link("notes.md") == "https://github.com/someone/proj/blob/main/notes.md"
    assert c.link("Transcripts/catch-up.docx") == ""  # not in git, so not on GitHub


def test_paths_from_claude_map_back_to_the_original(folder, tmp_path):
    c = Corpus("proj", folder, cache=tmp_path / "cache")
    c.sync()
    want = "Transcripts/catch-up.docx"
    assert c.original(f"{c.mirror}/Transcripts/catch-up.docx.txt") == want
    assert c.original("Transcripts/catch-up.docx.txt") == want
    assert c.original("./Transcripts/catch-up.docx") == want
    assert c.original(str(folder / "notes.md")) == "notes.md"
    assert c.original("../../etc/passwd") is None and c.original("nope.md") is None


def test_tools_for_the_api_list_search_and_read(folder, tmp_path):
    c = Corpus("proj", folder, cache=tmp_path / "cache")
    c.sync()
    assert "Transcripts/catch-up.docx" in c.run_tool("list_files", {"pattern": "*.docx"})
    hits = c.run_tool("search", {"pattern": "immun"})
    assert "notes.md:3: Immunity is a metaphor." in hits and "Transcripts/catch-up.docx:2:" in hits
    assert c.run_tool("search", {"pattern": "("}) == "No matches."  # bad regex searched literally
    assert c.run_tool("read", {"path": "notes.md", "offset": 2, "limit": 1}).startswith("2\tThe budget is tight.\n… 1 more lines")
    assert "No such file" in c.run_tool("read", {"path": "/etc/passwd"})


def test_parse_spec():
    assert parse_spec("si=~/societal-immunity") == ("si", os.path.expanduser("~/societal-immunity"))
    assert parse_spec("/tmp/x=y/proj")[1] == "/tmp/x=y/proj"  # an = inside a path isn't a name
    assert parse_spec("/srv/papers")[0] == "papers"


# -- researching a collection -----------------------------------------------------

def fake_backend(reply, kind="claude-code"):
    calls = []

    async def search_corpus(system, schema, prompt, corpus, **kw):
        calls.append((system, prompt, corpus, kw))
        return reply, 3.0

    return SimpleNamespace(kind=kind, can_research=True, can_search_corpus=True, search_corpus=search_corpus), calls


def test_researcher_searches_a_collection_and_maps_paths(folder, tmp_path):
    c = Corpus("proj", folder, about="Project notes and transcripts", cache=tmp_path / "cache")
    backend, calls = fake_backend({"answer": "They agreed immune memory matters most.", "items": [
        {"path": f"{c.mirror}/Transcripts/catch-up.docx.txt", "title": "Catch-up", "note": "says so"},
        {"path": "Transcripts/catch-up.docx", "title": "dupe", "note": ""},
        {"path": "made-up.md", "title": "Imagined", "note": "?"}]})
    r = Researcher(SimpleNamespace(backend=backend, model="m"), corpora=[c])
    out, _ = asyncio.run(r.run("What did we agree?", "Paper", "corpus", "proj"))
    assert out["items"][0] == {"title": "Catch-up", "note": "says so", "path": "Transcripts/catch-up.docx", "url": "", "added": False}
    assert len(out["items"]) == 2 and out["items"][1]["path"] == ""  # duplicate dropped; unknown path kept without one
    system, prompt, corpus, _ = calls[0]
    assert corpus is c and "Project notes and transcripts" in prompt and "<request>\nWhat did we agree?" in prompt
    assert "only from what the documents say" in system
    (info,) = r.collections()
    assert info["name"] == "proj" and info["about"] == "Project notes and transcripts" and info["files"] == 3 and not info["saved"]
    assert r.find("PROJ") == "proj" and r.find("other") is None and r.find(None) is None
    with pytest.raises(ScribeError):
        asyncio.run(r.run("q", "", "corpus", "missing"))


def test_no_collections_without_a_backend_that_can_search_them(folder, tmp_path):
    c = Corpus("proj", folder, cache=tmp_path / "cache")
    r = Researcher(SimpleNamespace(backend=SimpleNamespace(can_research=False, can_search_corpus=False), model="m"), corpora=[c])
    assert r.collections() == [] and r.find("proj") is None


def make_hub(tmp_path, corpora):
    backend, _ = fake_backend({"answer": "", "items": []})
    hub = Hub(SimpleNamespace(backlog=0.0, asr_name="x"), SimpleNamespace(available=True, model="fake"), tmp_path / "data",
              researcher=Researcher(SimpleNamespace(backend=backend, model="m"), corpora=corpora))
    hub._pump_research = lambda: None
    return hub


def test_only_the_collection_chosen_for_the_session_is_ever_searched(folder, tmp_path):
    a, b = Corpus("proj", folder, cache=tmp_path / "c"), Corpus("papers", folder, cache=tmp_path / "c")
    hub = make_hub(tmp_path, [a, b])
    s = hub.session
    sent = []

    class WS:
        async def send_str(self, m):
            sent.append(json.loads(m))

    ws = WS()
    hub.clients[ws] = SimpleNamespace(editing=False, streams={})
    spoken = lambda *reqs: asyncio.run(hub._requested_research(s, list(reqs)))

    # Nothing chosen: Claude isn't told about any collection, and a request to search one goes nowhere.
    assert hub._collections_for(s) == []
    spoken({"label": "x", "question": "What did we agree on memory?", "kind": "corpus", "corpus": "proj"})
    assert s.research == [] and "pick it in the Research panel" in sent[-1]["text"] and "proj" in sent[-1]["text"]
    spoken({"label": "x", "question": "What does the repo say?", "kind": "corpus"})
    assert s.research == [] and "pick one in the Research panel" in sent[-1]["text"]
    asyncio.run(hub.handle(ws, {"type": "research_ask", "q": "Budgets?", "corpus": "proj"}))
    assert s.research[-1]["kind"] != "corpus"  # typed: the web, whatever the message claims

    # Choose one: it's the only one Claude hears of, and every request goes to it, whatever was named.
    asyncio.run(hub.handle(ws, {"type": "research_corpus", "name": "Papers"}))
    assert s.corpus == "papers" and [c["name"] for c in hub._collections_for(s)] == ["papers"]
    spoken({"label": "x", "question": "What did we agree on memory?", "kind": "corpus", "corpus": "proj"},
           {"label": "y", "question": "What is the levee effect?", "kind": "quick"})
    assert [(j["kind"], j.get("corpus")) for j in s.research[-2:]] == [("corpus", "papers"), ("quick", None)]
    asyncio.run(hub.handle(ws, {"type": "research_ask", "q": "Any mention of budgets?", "corpus": "proj"}))
    assert s.research[-1]["corpus"] == "papers"
    assert {c["name"] for c in hub.status()["corpora"]} == {"proj", "papers"}  # the picker still lists all

    # Unknown names clear it, and the choice is saved with the session.
    s.save()
    from hark.session import Session
    assert Session.load(hub.data_dir, s.id).corpus == "papers"
    asyncio.run(hub.handle(ws, {"type": "research_corpus", "name": "nope"}))
    assert s.corpus is None


def test_findings_and_exports_from_a_collection_name_the_file(tmp_path):
    hub = make_hub(tmp_path, [])
    s = hub.session
    job = s.add_research("What did we agree?", "typed", "done", "Agreed", "corpus", "proj")
    job.update(answer="Immune memory matters most.", items=[{"title": "Catch-up", "note": "n", "path": "T/c.docx", "url": "", "added": False}])
    found = "\n".join(research_findings(s))
    assert "(from the proj collection)" in found and "Answer: Immune memory" in found and "Catch-up (T/c.docx): n" in found
    md = research_markdown("T", [job])
    assert "(searched proj)" in md and "**Catch-up** (`T/c.docx`)" in md
    assert s.add_research("q", "typed", "queued", kind="corpus")["kind"] == "sources"  # no collection named


def test_scribe_prompt_lists_collections():
    p = build_prompt("b", "# D", [], [], ["Ana: Claude, what do the transcripts say?"], [], collections=[{"name": "si", "about": "transcripts"}])
    assert "<collections>\n- si: transcripts\n</collections>" in p
    assert "<collections>" not in build_prompt("b", "# D", [], [], [], [])
    # Saved but none picked: Claude is told that much (no names), so it passes the request on instead of saying there are none.
    p = build_prompt("b", "# D", [], [], [], [], collections=[], collections_unpicked=True)
    assert "<collections_unpicked/>" in p and "<collections>" not in p
    assert "<collections_unpicked/>" not in build_prompt("b", "# D", [], [], [], [], collections=[{"name": "si"}], collections_unpicked=True)


# -- backends -----------------------------------------------------------------------

def test_claude_code_search_is_read_only_and_confined(folder, tmp_path, monkeypatch):
    cc = ClaudeCode("sonnet", claude_bin="/bin/true")
    cc.flags = {"--tools", "--add-dir", "--restricted", "--permission-mode", "--json-schema", "--safe-mode"}
    c = Corpus("proj", folder, cache=tmp_path / "cache")
    seen = {}

    async def run(cmd, prompt, hint, timeout):
        seen.update(cmd=cmd, prompt=prompt)
        return {"answer": "", "items": []}, 1.0

    monkeypatch.setattr(cc, "_run", run)
    asyncio.run(cc.search_corpus("sys", {}, "question", c))
    cmd = seen["cmd"]
    assert cmd[cmd.index("--tools") + 1] == "Read,Grep,Glob" and cmd[cmd.index("--add-dir") + 1] == str(c.mirror)
    assert "--restricted" in cmd and cmd[cmd.index("--permission-mode") + 1] == "dontAsk"
    assert str(c.mirror) in seen["prompt"]
    cc.flags.discard("--restricted")  # an older CLI couldn't keep it inside the folder
    assert not cc.can_search_corpus
    with pytest.raises(ScribeError, match="recent Claude Code"):
        asyncio.run(cc.search_corpus("sys", {}, "q", c))


def tool_use(i, name, args):
    return SimpleNamespace(type="tool_use", id=f"t{i}", name=name, input=args)


def test_anthropic_search_runs_hark_tools_until_it_answers(folder, tmp_path):
    c = Corpus("proj", folder, cache=tmp_path / "cache")
    c.sync()
    api = AnthropicAPI("sonnet", api_key="k")
    api._client = FakeAnthropic([
        message(tool_use(1, "search", {"pattern": "memory"}), stop="tool_use"),
        message(tool_use(2, "read", {"path": "Transcripts/catch-up.docx"}), stop="tool_use"),
        message(text('{"answer": "Memory matters.", "items": [{"path": "Transcripts/catch-up.docx", "title": "C", "note": "n"}]}')),
    ])
    r = Researcher(Scribe(api), corpora=[c])
    out, _ = asyncio.run(r.run("What matters?", "", "corpus", "proj"))
    first, second, third = api._client.calls
    assert {t["name"] for t in first["tools"]} == {"list_files", "search", "read"}
    msgs = third["messages"]  # one list, added to as the loop goes
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant", "user"]
    result = msgs[2]["content"][0]
    assert result["tool_use_id"] == "t1" and "catch-up.docx:2: We agreed immune memory" in result["content"]
    assert "1\tAna 0:03" in msgs[4]["content"][0]["content"]
    assert out["answer"] == "Memory matters." and out["items"][0]["path"] == "Transcripts/catch-up.docx"


def test_anthropic_search_stops_using_tools_on_the_last_round(folder, tmp_path):
    c = Corpus("proj", folder, cache=tmp_path / "cache")
    c.sync()
    api = AnthropicAPI("sonnet", api_key="k")
    api.CORPUS_ROUNDS = 2
    api._client = FakeAnthropic([message(tool_use(1, "list_files", {}), stop="tool_use"), message(text('{"answer": "x", "items": []}'))])
    asyncio.run(api.search_corpus("s", {}, "p", c))
    assert "tool_choice" not in api._client.calls[0] and api._client.calls[1]["tool_choice"] == {"type": "none"}


def test_setup_keeps_saved_collections(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG", tmp_path)
    config.save_corpora([{"name": "si", "path": "/x", "about": ""}])
    config.save({"provider": "claude-code", "model": "sonnet"})  # what `hark --setup` does
    saved = json.loads((tmp_path / "config.json").read_text())
    assert saved["model"] == "sonnet" and saved["corpora"] == [{"name": "si", "path": "/x", "about": "", "repo": ""}]
    assert config.load() == {"provider": "claude-code", "model": "sonnet"}


# -- adding, removing and keeping up to date ------------------------------------------

def test_github_repo_names_and_links():
    assert github_repo("mikefsway/societal-immunity") == "mikefsway/societal-immunity"
    assert github_repo("https://github.com/mikefsway/hark.git") == "mikefsway/hark"
    assert github_repo("git@github.com:a-b/c.d") == "a-b/c.d"
    assert github_repo("~/projects/x") is None and github_repo("/srv/x") is None and github_repo("one-word") is None


def test_the_page_can_only_add_github_repos(folder):
    with pytest.raises(ValueError, match="GitHub"):
        make_spec(str(folder), local=False)
    assert make_spec(f"notes={folder}")["name"] == "notes"


def test_saved_collections_are_reread_when_the_config_changes(folder, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG", tmp_path / "cfg")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    config.config_dir()
    lib = Library(config.config_file())
    assert lib.current() == {}
    config.add_corpus(f"proj={folder}", "notes")
    first = lib.current()["proj"]
    first.sync()
    assert lib.saved == {"proj"} and first.about == "notes"
    config.add_corpus(f"proj={folder}", "new words")  # same folder: the same collection, keeping what it knows
    assert lib.current()["proj"] is first and first.about == "new words" and first.files
    assert config.remove_corpus("proj") and lib.current() == {}
    assert folder.is_dir() and not first.mirror.exists()  # your folder stays; hark's copy goes
    assert not config.remove_corpus("proj")


def test_a_hark_owned_clone_is_fetched_before_a_search(tmp_path, monkeypatch):
    run = lambda *a, cwd=None: subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e.org", *a], cwd=cwd, check=True, capture_output=True)
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    run("init", "-q", "-b", "main", cwd=upstream)
    (upstream / "a.md").write_text("first\n")
    run("add", ".", cwd=upstream)
    run("commit", "-qm", "1", cwd=upstream)
    clone = tmp_path / "repos" / "o-r"
    run("clone", "-q", "--depth", "1", f"file://{upstream}", str(clone))
    c = Corpus("r", clone, cache=tmp_path / "cache", repo="o/r")
    assert c.sync()["files"] == 1 and c.problem == ""
    (upstream / "b.md").write_text("second\n")
    run("add", ".", cwd=upstream)
    run("commit", "-qm", "2", cwd=upstream)
    c.sync()
    assert "b.md" not in c.files  # fetched moments ago: not again yet
    c.sync(force=True)
    assert "b.md" in c.files
    monkeypatch.setattr("hark.corpus.PULL_EVERY", 0)
    run("remote", "set-url", "origin", str(tmp_path / "gone"), cwd=clone)
    c.sync()  # offline or gone: search what's there, and say so
    assert "b.md" in c.files and "Couldn't update" in c.problem


def test_page_adds_repos_updates_and_forgets(folder, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG", tmp_path / "cfg")
    config.config_dir()
    backend, _ = fake_backend({"answer": "", "items": []})
    hub = Hub(SimpleNamespace(backlog=0.0, asr_name="x"), SimpleNamespace(available=True, model="fake"), tmp_path / "data",
              researcher=Researcher(SimpleNamespace(backend=backend, model="m"), corpora=Library(config.config_file(), cache=tmp_path / "c")))
    sent = []

    class WS:
        async def send_str(self, m):
            sent.append(json.loads(m))

    ws = WS()
    hub.clients[ws] = SimpleNamespace(editing=False, streams={})
    asyncio.run(hub.handle(ws, {"type": "corpus_add", "spec": str(folder)}))
    assert config.load_corpora() == [] and "GitHub" in sent[-2]["text"]  # a folder can't be added from the page
    config.add_corpus(f"proj={folder}")
    asyncio.run(hub.handle(ws, {"type": "corpus_update", "name": "proj"}))
    assert any("proj is up to date: 3 files" in m.get("text", "") for m in sent)
    assert hub.status()["corpora"][0]["saved"]
    asyncio.run(hub.handle(ws, {"type": "corpus_remove", "name": "proj"}))
    assert config.load_corpora() == [] and hub.status()["corpora"] == []
