import asyncio
import json
from types import SimpleNamespace

import numpy as np

from hark.hub import Hub
from hark.session import Session
from hark.speakers import PROMOTE_AT, SMALL, UNKNOWN, UNKNOWN_NOTE, Speakers

DIM = 192


def voice(rng) -> np.ndarray:
    """A person's voice: in 192 dimensions two of these are all but unrelated."""
    v = rng.normal(size=DIM)
    return v / np.linalg.norm(v)


def like(v: np.ndarray, rng, sim: float) -> np.ndarray:
    """A different voice that scores `sim` against `v`, as two people in one room can."""
    w = rng.normal(size=DIM)
    w -= (w @ v) * v
    return sim * v + np.sqrt(1 - sim ** 2) * w / np.linalg.norm(w)


def said(v: np.ndarray, rng, blur: float) -> np.ndarray:
    """One utterance of `v`, blurred by the room: `blur` of 3 scores about 0.32 against the voice itself."""
    return v + rng.normal(scale=blur / np.sqrt(DIM), size=DIM)


def profile(sp: Speakers, v: np.ndarray, n: int) -> str:
    """An auto speaker whose voice profile is already built from `n` utterances, as a reopened session has."""
    sid = sp._new()
    sp.items[sid].update(sum=v / np.linalg.norm(v) * n, n=n)
    return sid


def cos(x: np.ndarray, y: np.ndarray) -> float:
    return float(x @ y / (np.linalg.norm(x) * np.linalg.norm(y)))


class Page:
    """A browser that is listening but says nothing back."""

    async def send_str(self, s):
        pass


def make_hub(tmp_path, session: Session):
    hub = Hub(SimpleNamespace(backlog=0.0, asr_name="x"), SimpleNamespace(available=True, model="fake"), tmp_path,
              researcher=SimpleNamespace(available=False, collections=lambda: [], find=lambda n: None))
    hub.session = session
    ws = Page()
    hub.clients[ws] = SimpleNamespace(editing=False, streams={})
    return hub, ws


def test_a_voice_heard_once_stays_unknown_and_three_alike_become_a_speaker():
    rng = np.random.default_rng(1)
    a, b = voice(rng), voice(rng)
    sp = Speakers()
    once = sp.assign(said(b, rng, 0.7), 3.0)
    first = sp.assign(said(a, rng, 0.7), 3.0)
    assert sp.resolve(once) == UNKNOWN and sp.resolve(first) == UNKNOWN and sp.items.keys() == {UNKNOWN}
    second = sp.assign(said(a, rng, 0.7), 3.0)
    third = sp.assign(said(a, rng, 0.7), 3.0)
    new = sp.resolve(third)
    assert new != UNKNOWN and sp.items[new]["name"] == "Speaker 1" and sp.items[new]["n"] == PROMOTE_AT
    # The two earlier lines are that speaker's too, and the one-off is still nobody's.
    assert sp.resolve(first) == new and sp.resolve(second) == new and sp.resolve(once) == UNKNOWN


def test_one_microphone_in_a_room_of_four_makes_four_speakers_not_one_per_turn():
    rng = np.random.default_rng(7)
    people = [voice(rng) for _ in range(4)]
    sp, heard, faint = Speakers(), {}, 0
    for i in range(152):
        who = i % 4 if rng.random() < 0.8 else int(rng.integers(0, 4))  # mostly taking turns, sometimes not
        emb = said(people[who], rng, 0.7 if rng.random() < 0.6 else 3.0)  # three turns in five are picked up clearly
        faint += cos(emb, people[who]) < 0.40
        heard.setdefault(who, set()).add(sp.resolve(sp.assign(emb, 3.0)))
    assert faint > 40  # a third of the room's utterances don't match their own voice
    assert len(sp.items) - 1 == 4 and len(sp.pending) == faint
    assert all(s["n"] > SMALL for sid, s in sp.items.items() if sid != UNKNOWN)
    for seen in heard.values():
        assert len(seen - {UNKNOWN}) == 1  # each person got one speaker, not one per turn


def test_two_different_voices_in_the_same_room_stay_apart():
    rng = np.random.default_rng(3)
    a = voice(rng)
    b = like(a, rng, 0.2)
    sp = Speakers()
    for _ in range(4):
        sp.assign(said(a, rng, 0.7), 3.0)
        sp.assign(said(b, rng, 0.7), 3.0)
    assert len(sp.items) - 1 == 2


def test_a_small_speaker_joins_a_firmer_profile_but_two_settled_ones_do_not():
    rng = np.random.default_rng(5)
    a = voice(rng)
    sp = Speakers()
    grown = profile(sp, a, 20)
    small = profile(sp, like(a, rng, 0.45), 1)  # one faint utterance of the same person, as a reopened session has by the dozen
    assert 0.40 <= cos(sp.items[small]["sum"], a) < 0.50  # near enough to be them, not near enough for _absorb
    sp.assign(said(a, rng, 0.7), 3.0)
    assert sp.resolve(small) == grown and small not in sp.items

    other = profile(sp, like(a, rng, 0.46), 20)
    sp.assign(said(a, rng, 0.7), 3.0)
    assert other in sp.items and sp.resolve(other) == other  # 0.46 is two people, not one


def test_a_name_or_a_line_the_user_gave_is_never_merged_automatically():
    rng = np.random.default_rng(11)
    a = voice(rng)
    sp = Speakers()
    grown = profile(sp, a, 20)
    ana = profile(sp, like(a, rng, 0.6), 1)
    sp.rename(ana, "Ana")
    theirs = profile(sp, like(a, rng, 0.6), 1)
    sp.claim(theirs)  # someone gave this one a line by hand
    sp.assign(said(a, rng, 0.7), 3.0)
    assert sp.resolve(ana) == ana and sp.name(ana) == "Ana"
    assert sp.resolve(grown) == ana  # the auto speaker folds into the name, never the other way round
    assert sp.resolve(theirs) == theirs


def test_giving_two_speakers_the_same_name_merges_them():
    rng = np.random.default_rng(37)
    sp = Speakers()
    ana, mike = profile(sp, voice(rng), 4), profile(sp, voice(rng), 2)
    sp.rename(ana, "Ana")
    kept = sp.rename(mike, "ana")  # the same name, spelt as they typed it
    assert kept == ana and sp.resolve(mike) == ana and sp.items[ana]["n"] == 6


def test_unknown_is_not_a_person_so_it_cannot_be_renamed_or_merged():
    rng = np.random.default_rng(13)
    sp = Speakers()
    sp.assign(said(voice(rng), rng, 0.7), 3.0)
    assert sp.rename(UNKNOWN, "Ana") == UNKNOWN and sp.name(UNKNOWN) == "Unknown"
    mike = profile(sp, voice(rng), 5)
    assert sp.rename(mike, "Unknown") == mike and sp.name(mike) == "Speaker 1"
    sp.merge(mike, UNKNOWN)
    sp.merge(UNKNOWN, mike)
    assert sp.resolve(mike) == mike and sp.resolve(UNKNOWN) == UNKNOWN
    assert sp.named("Unknown") == UNKNOWN  # a typed note or a caption with no name goes in the bucket


def test_a_clip_too_short_to_judge_follows_whoever_was_talking():
    rng = np.random.default_rng(17)
    a = voice(rng)
    sp = Speakers()
    assert sp.assign(None, 0.5) == UNKNOWN  # before anyone has been heard properly
    mike = profile(sp, a, 5)
    assert sp.assign(None, 0.5, previous=mike) == mike
    held = sp.assign(said(voice(rng), rng, 3.0), 3.0)
    assert sp.resolve(sp.assign(None, 0.5, previous=held)) == UNKNOWN
    assert sp.assign(None, 0.5) == mike  # nobody to carry on from: the only voice we know


def test_saying_who_said_a_line_teaches_that_speakers_voice(tmp_path):
    rng = np.random.default_rng(19)
    a, b = voice(rng), voice(rng)
    s = Session(tmp_path)
    emb = said(a, rng, 3.0)
    line = s.add_line(1.0, "mic", s.speakers.assign(emb, 3.0), "mm, agreed", emb=emb)
    assert s.speakers.resolve(line["speaker"]) == UNKNOWN
    mike = s.speakers.named("Mike")
    hub, ws = make_hub(tmp_path, s)

    async def go():
        await hub.handle(ws, {"type": "reassign", "line": line["id"], "name": "Mike"})
        assert s.speakers.resolve(line["speaker"]) == mike and line["manual"] is True
        assert s.speakers.items[mike]["n"] == 1 and not s.speakers.pending  # his voice is known from that one line
        for _ in range(PROMOTE_AT):
            s.speakers.assign(said(b, rng, 0.7), 3.0)
        assert s.speakers.resolve(line["speaker"]) == mike  # a promotion later on doesn't take the line back
        await hub.handle(ws, {"type": "reassign", "line": line["id"], "name": ""})
        assert line["speaker"] == UNKNOWN and s.speaker_notes() == [UNKNOWN_NOTE]

    asyncio.run(go())


def test_a_line_given_to_a_speaker_by_hand_stays_with_them(tmp_path):
    rng = np.random.default_rng(41)
    a = voice(rng)
    s = Session(tmp_path)
    grown = profile(s.speakers, a, 20)
    small = profile(s.speakers, like(a, rng, 0.45), 1)  # the same person, heard faintly once
    line = s.add_line(1.0, "mic", grown, "shall we start")
    hub, ws = make_hub(tmp_path, s)

    async def go():
        await hub.handle(ws, {"type": "reassign", "line": line["id"], "name": s.speakers.name(small)})
        s.speakers.assign(said(a, rng, 0.7), 3.0)  # the firmer profile would otherwise take that small one in
        assert s.speakers.resolve(line["speaker"]) == small and line["manual"] is True

    asyncio.run(go())


def test_claude_can_name_a_speaker_but_not_the_unknown_bucket(tmp_path):
    rng = np.random.default_rng(31)
    a = voice(rng)
    s = Session(tmp_path)
    s.add_line(1.0, "mic", s.speakers.assign(said(a, rng, 3.0), 3.0), "mm, agreed")
    mike = profile(s.speakers, a, 5)
    s.add_line(2.0, "mic", mike, "shall we start")
    hub, ws = make_hub(tmp_path, s)

    async def go():
        names = [{"from": "Unknown", "to": "Dave"}, {"from": "Speaker 1", "to": "Mike"}]
        hub.held = (s, len(s.lines), {"ops": [], "speaker_names": names}, 1.0, s.doc)
        await hub._maybe_release_held()
        assert s.speakers.name(UNKNOWN) == "Unknown" and s.speakers.name(mike) == "Mike"
        assert sorted(s.speaker_notes()) == ["Mike", UNKNOWN_NOTE]

    asyncio.run(go())


def test_speech_lines_save_their_embedding_but_the_page_never_sees_it(tmp_path):
    rng = np.random.default_rng(23)
    s = Session(tmp_path)
    emb = said(voice(rng), rng, 0.7)
    line = s.add_line(1.0, "mic", s.speakers.assign(emb, 3.0), "shall we start", emb=emb)
    typed = s.add_line(2.0, "typed", s.speakers.named("You (typed)"), "make Actions a table", kind="typed")
    s.save()
    saved = json.loads((s.dir / "session.json").read_text(encoding="utf-8"))
    assert saved["lines"][0]["emb"] == [round(float(x), 4) for x in emb] and "emb" not in saved["lines"][1]
    assert "emb" not in typed and "emb" not in s.public_line(line)
    back = Session.load(tmp_path, s.id)
    assert back.speakers.resolve(line["speaker"]) == UNKNOWN and len(back.speakers.pending) == 1


def test_a_session_saved_before_unknown_existed_still_opens(tmp_path):
    rng = np.random.default_rng(29)
    old = {"id": "2026-01-01_100000", "mode": "meeting", "doc": "# Budget\n", "processed": 0, "next_line": 3,
           "lines": [{"id": 1, "t": 0, "added": 0, "source": "mic", "speaker": "S1", "text": "hello", "kind": "speech"},
                     {"id": 2, "t": 1, "added": 0, "source": "mic", "speaker": "S2", "text": "mm", "kind": "speech"}],
           "speakers": {"next": 3, "alias": {},
                        "items": {"S1": {"name": "Ana", "auto": False, "n": 4, "sum": [round(float(x), 5) for x in voice(rng) * 4]},
                                  "S2": {"name": "Unknown", "auto": False, "n": 1, "sum": None}}}}
    (tmp_path / old["id"]).mkdir()
    (tmp_path / old["id"] / "session.json").write_text(json.dumps(old), encoding="utf-8")
    s = Session.load(tmp_path, old["id"])
    assert s.speakers.resolve("S2") == UNKNOWN and s.speakers.name("S2") == "Unknown"
    assert s.speaker_notes() == ["Ana", UNKNOWN_NOTE]
    assert s.public()["speakers"][UNKNOWN]["unknown"] is True
    assert s.public_line(s.lines[1])["speaker"] == UNKNOWN
