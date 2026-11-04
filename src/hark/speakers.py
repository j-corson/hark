"""Online speaker clustering from voice embeddings.

Each utterance gets a voice embedding and joins the closest known speaker if it is
similar enough. An utterance that matches nobody is held in Unknown rather than
starting a speaker of its own: with one microphone in a room, many of a person's
utterances score below the threshold against their own voice, and a speaker per
utterance is worse than no name at all.

The words used here:

  Voice profile  a speaker's running sum of normalised embeddings, and the count behind it.
  Small speaker  an auto speaker whose voice profile is built from SMALL utterances or fewer.
  Unknown        the one bucket per session for utterances whose voice is not yet
                 identified. Not a person, and possibly several people.
  Promotion      turning a group of similar Unknown utterances into a new auto speaker.

A held utterance gets an id of its own (``U7``) pointing at Unknown through `alias`, so
promoting it later relabels its line everywhere that line is shown.
"""

from __future__ import annotations

import numpy as np

PALETTE = ["#2f6fdf", "#d9622b", "#2a9d6f", "#a347c6", "#c4405a", "#9a7b1c", "#1f8fa3", "#6b6f80"]
UNKNOWN = "U"  # id of the Unknown bucket; held utterances are U1, U2, ...
UNKNOWN_NAME = "Unknown"
UNKNOWN_NOTE = "Unknown (unidentified voices, possibly several people)"
UNKNOWN_COLOR = "#8a8f98"
PROMOTE_AT = 3  # held utterances that must match each other before they become a speaker
SMALL = 2  # a voice profile built from this many utterances or fewer is still provisional


def _bucket() -> dict:
    return {"name": UNKNOWN_NAME, "auto": False, "sum": None, "n": 0, "pinned": True}


class Speakers:
    def __init__(self, threshold: float = 0.40, min_new_seconds: float = 1.5):
        self.threshold = threshold
        self.min_new_seconds = min_new_seconds
        self.items: dict[str, dict] = {UNKNOWN: _bucket()}  # id -> {name, auto, sum(np.ndarray|None), n, pinned}
        self.alias: dict[str, str] = {}  # merged-away or held utterance id -> where it belongs now
        self.pending: dict[str, np.ndarray] = {}  # held utterance id -> its normalised embedding
        self.relabels = 0  # bumped whenever lines already said change speaker, so the page can be refreshed
        self._next = 1
        self._next_held = 1

    # -- identity -----------------------------------------------------------
    def resolve(self, sid: str) -> str:
        while sid in self.alias:
            sid = self.alias[sid]
        return sid

    def name(self, sid: str) -> str:
        sp = self.items.get(self.resolve(sid))
        return sp["name"] if sp else UNKNOWN_NAME

    def _new(self) -> str:
        sid = f"S{self._next}"
        self.items[sid] = {"name": f"Speaker {self._next}", "auto": True, "sum": None, "n": 0, "pinned": False}
        self._next += 1
        return sid

    def named(self, name: str) -> str:
        """Id of the speaker called `name`, creating one if needed (used for typed notes)."""
        for sid, sp in self.items.items():
            if sp["name"].lower() == name.lower():
                return sid
        sid = self._new()
        self.items[sid].update(name=name, auto=False)
        return sid

    def _centre(self, sid: str) -> np.ndarray:
        s = self.items[sid]["sum"]
        return s / (np.linalg.norm(s) + 1e-9)

    def _claimed(self, sid: str) -> bool:
        """True if the user gave this speaker a name, or gave it a line by hand."""
        sp = self.items[sid]
        return not sp["auto"] or sp["pinned"]

    # -- assignment ---------------------------------------------------------
    def assign(self, emb: np.ndarray | None, seconds: float, previous: str | None = None) -> str:
        if previous:
            previous = self.resolve(previous)
        voiced = [sid for sid, sp in self.items.items() if sp["sum"] is not None]
        if emb is None:
            # Too short to judge the voice: assume the same person kept talking.
            if previous in self.items:
                return previous
            return voiced[-1] if voiced else UNKNOWN
        emb = emb / (np.linalg.norm(emb) + 1e-9)
        best, best_sim = None, -1.0
        for sid in voiced:
            sim = float(self._centre(sid) @ emb)
            if sim > best_sim:
                best, best_sim = sid, sim
        if best is not None and best_sim >= self.threshold:
            sp = self.items[best]
            sp["sum"] = sp["sum"] + emb
            sp["n"] += 1
            return self._settle(best)
        if best is not None and seconds < self.min_new_seconds:
            return best  # too short to be worth holding; leave the profile alone
        return self._hold(emb)

    def _hold(self, emb: np.ndarray) -> str:
        """Park an utterance that matches nobody in Unknown, where it can still be promoted or claimed."""
        tid = f"U{self._next_held}"
        self._next_held += 1
        self.pending[tid] = emb
        self.alias[tid] = UNKNOWN
        self._promote(tid)
        return tid

    def _promote(self, tid: str) -> None:
        """Make a speaker of PROMOTE_AT held utterances that match this one and their shared average.

        Only a group holding the newest utterance can have just become one, so that is the only group looked for.
        """
        group = [t for t, e in self.pending.items() if float(e @ self.pending[tid]) >= self.threshold]
        if len(group) < PROMOTE_AT:
            return
        total = sum(self.pending[t] for t in group)
        centre = total / (np.linalg.norm(total) + 1e-9)
        group = [t for t in group if float(self.pending[t] @ centre) >= self.threshold]
        if len(group) < PROMOTE_AT:
            return
        sid = self._new()
        self.items[sid].update(sum=sum(self.pending[t] for t in group), n=len(group))
        for t in group:
            del self.pending[t]
            self.alias[t] = sid
        self.relabels += 1
        self._settle(sid)

    def _settle(self, sid: str) -> str:
        """A voice profile has just grown: take in whatever now matches it, and fold it away if it was a split."""
        self._recheck(sid)
        return self._absorb(sid)

    def _recheck(self, sid: str) -> None:
        """Small speakers and held utterances that match this firmer profile belong to it."""
        c = self._centre(sid)
        for other, sp in list(self.items.items()):
            if other == sid or sp["sum"] is None or not sp["auto"] or sp["pinned"] or sp["n"] > SMALL:
                continue
            if float(c @ self._centre(other)) >= self.threshold:
                self.merge(other, sid)
        c = self._centre(sid)  # those are part of it now, so hold the held utterances up to its new shape
        for tid, emb in list(self.pending.items()):
            if float(c @ emb) >= self.threshold:
                del self.pending[tid]
                self.alias[tid] = sid
                self.items[sid]["sum"] = self.items[sid]["sum"] + emb
                self.items[sid]["n"] += 1
                self.relabels += 1

    def _absorb(self, sid: str) -> str:
        """As a voice profile firms up, fold in any other speaker that now clearly matches it."""
        a = self._centre(sid)
        for other, sp in list(self.items.items()):
            if other == sid or sp["sum"] is None:
                continue
            if float(a @ self._centre(other)) >= self.threshold + 0.1:
                keep, drop = (sid, other) if self.items[sid]["n"] >= sp["n"] else (other, sid)
                if self._claimed(drop) and not self._claimed(keep):
                    keep, drop = drop, keep  # never lose a name, or a line, the user gave
                elif self._claimed(drop):
                    continue  # both are the user's work: leave them alone
                self.merge(drop, keep)
                return self._absorb(keep)
        return sid

    # -- edits from the UI --------------------------------------------------
    def rename(self, sid: str, name: str) -> str:
        """Rename a speaker. Renaming to an existing name merges the two."""
        sid = self.resolve(sid)
        name = name.strip()[:40]
        if not name or sid not in self.items:
            return sid
        if sid == UNKNOWN or name.lower() == UNKNOWN_NAME.lower():
            return sid  # Unknown is not a person: it can't be renamed, and nobody can be called it
        for other, sp in self.items.items():
            if other != sid and sp["name"].lower() == name.lower():
                self.merge(sid, other)
                return other
        self.items[sid].update(name=name, auto=False)
        return sid

    def claim(self, sid: str, was: str | None = None) -> str:
        """Someone said who a held utterance was: take it out of Unknown and add its voice to that speaker.

        The voice is the one Unknown was already holding for that utterance; a line with nothing
        held for it (rebuilt, or handed straight from one speaker to another) teaches no voice.
        """
        sid = self.resolve(sid)
        if sid == UNKNOWN or sid not in self.items:
            return sid  # nobody took it: leave the voice held, so it can still be promoted later
        emb = self.pending.pop(was, None)
        self.items[sid]["pinned"] = True
        if emb is None:
            return sid
        sp = self.items[sid]
        sp["sum"] = emb.copy() if sp["sum"] is None else sp["sum"] + emb
        sp["n"] += 1
        return self._settle(sid)

    def merge(self, src: str, dst: str) -> None:
        src, dst = self.resolve(src), self.resolve(dst)
        if src == dst or src not in self.items or dst not in self.items or UNKNOWN in (src, dst):
            return
        a, b = self.items.pop(src), self.items[dst]
        if a["sum"] is not None:
            b["sum"] = a["sum"] if b["sum"] is None else b["sum"] + a["sum"]
            b["n"] += a["n"]
        b["pinned"] = b["pinned"] or a["pinned"]
        self.alias[src] = dst
        self.relabels += 1

    # -- persistence / UI ---------------------------------------------------
    def public(self) -> dict:
        out = {}
        for i, (sid, sp) in enumerate(sorted(self.items.items(), key=lambda kv: kv[0] == UNKNOWN)):
            if sid == UNKNOWN:
                out[sid] = {"name": sp["name"], "auto": False, "unknown": True, "color": UNKNOWN_COLOR}
                continue
            num = int(sid[1:]) if sid[1:].isdigit() else i
            out[sid] = {"name": sp["name"], "auto": sp["auto"], "color": PALETTE[(num - 1) % len(PALETTE)]}
        return out

    def to_json(self) -> dict:
        return {
            "next": self._next,
            "next_held": self._next_held,
            "alias": self.alias,
            "items": {sid: {"name": sp["name"], "auto": sp["auto"], "n": sp["n"], "pinned": sp["pinned"], "sum": None if sp["sum"] is None else sp["sum"].round(5).tolist()} for sid, sp in self.items.items()},
            "pending": {tid: [round(float(x), 4) for x in emb] for tid, emb in self.pending.items()},
        }

    def load_json(self, d: dict) -> None:
        self._next = d.get("next", 1)
        self._next_held = d.get("next_held", 1)
        self.alias = d.get("alias", {})
        self.items = {sid: {"name": sp["name"], "auto": sp["auto"], "n": sp.get("n", 0), "pinned": bool(sp.get("pinned")), "sum": None if sp.get("sum") is None else np.array(sp["sum"], dtype=np.float32)} for sid, sp in d.get("items", {}).items()}
        self.pending = {tid: np.array(e, dtype=np.float32) for tid, e in (d.get("pending") or {}).items()}
        self.items[UNKNOWN] = _bucket()
        # A session saved before Unknown existed may hold a speaker someone called "Unknown": that is the bucket now.
        for sid, sp in list(self.items.items()):
            if sid != UNKNOWN and sp["name"].lower() == UNKNOWN_NAME.lower():
                del self.items[sid]
                self.alias[sid] = UNKNOWN
