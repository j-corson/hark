"""Online speaker clustering from voice embeddings.

Each utterance gets a voice embedding. It joins the closest known speaker if
it is similar enough, otherwise it starts a new one. Over-splitting is the
safer failure: merging two speakers in the UI is one rename away.
"""

from __future__ import annotations

import numpy as np

PALETTE = ["#2f6fdf", "#d9622b", "#2a9d6f", "#a347c6", "#c4405a", "#9a7b1c", "#1f8fa3", "#6b6f80"]


class Speakers:
    def __init__(self, threshold: float = 0.40, min_new_seconds: float = 1.5):
        self.threshold = threshold
        self.min_new_seconds = min_new_seconds
        self.items: dict[str, dict] = {}  # id -> {name, auto, sum(np.ndarray|None), n}
        self.alias: dict[str, str] = {}  # merged-away id -> surviving id
        self._next = 1

    # -- identity -----------------------------------------------------------
    def resolve(self, sid: str) -> str:
        while sid in self.alias:
            sid = self.alias[sid]
        return sid

    def name(self, sid: str) -> str:
        sp = self.items.get(self.resolve(sid))
        return sp["name"] if sp else "Unknown"

    def _new(self, emb=None) -> str:
        sid = f"S{self._next}"
        self.items[sid] = {"name": f"Speaker {self._next}", "auto": True, "sum": None if emb is None else emb.copy(), "n": 0 if emb is None else 1}
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

    # -- assignment ---------------------------------------------------------
    def assign(self, emb: np.ndarray | None, seconds: float, previous: str | None = None) -> str:
        if previous:
            previous = self.resolve(previous)
        voiced = [(sid, sp) for sid, sp in self.items.items() if sp["sum"] is not None]
        if emb is None:
            # Too short to judge the voice: assume the same person kept talking.
            if previous in self.items:
                return previous
            return voiced[-1][0] if voiced else self._new()
        emb = emb / (np.linalg.norm(emb) + 1e-9)
        best, best_sim = None, -1.0
        for sid, sp in voiced:
            c = sp["sum"] / (np.linalg.norm(sp["sum"]) + 1e-9)
            sim = float(c @ emb)
            if sim > best_sim:
                best, best_sim = sid, sim
        if best is not None and (best_sim >= self.threshold or seconds < self.min_new_seconds):
            sp = self.items[best]
            if best_sim >= self.threshold:
                sp["sum"] = sp["sum"] + emb
                sp["n"] += 1
                return self._absorb(best)
            return best
        return self._new(emb)

    def _absorb(self, sid: str) -> str:
        """As a voice profile firms up, fold in any other speaker that now clearly matches it."""
        a = self.items[sid]["sum"]
        a = a / (np.linalg.norm(a) + 1e-9)
        for other, sp in list(self.items.items()):
            if other == sid or sp["sum"] is None:
                continue
            b = sp["sum"] / (np.linalg.norm(sp["sum"]) + 1e-9)
            if float(a @ b) >= self.threshold + 0.1:
                keep, drop = (sid, other) if self.items[sid]["n"] >= sp["n"] else (other, sid)
                if not self.items[drop]["auto"] and self.items[keep]["auto"]:
                    keep, drop = drop, keep  # never lose a name the user gave
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
        for other, sp in self.items.items():
            if other != sid and sp["name"].lower() == name.lower():
                self.merge(sid, other)
                return other
        self.items[sid].update(name=name, auto=False)
        return sid

    def merge(self, src: str, dst: str) -> None:
        src, dst = self.resolve(src), self.resolve(dst)
        if src == dst or src not in self.items or dst not in self.items:
            return
        a, b = self.items.pop(src), self.items[dst]
        if a["sum"] is not None:
            b["sum"] = a["sum"] if b["sum"] is None else b["sum"] + a["sum"]
            b["n"] += a["n"]
        self.alias[src] = dst

    # -- persistence / UI ---------------------------------------------------
    def public(self) -> dict:
        out = {}
        for i, (sid, sp) in enumerate(self.items.items()):
            num = int(sid[1:]) if sid[1:].isdigit() else i
            out[sid] = {"name": sp["name"], "auto": sp["auto"], "color": PALETTE[(num - 1) % len(PALETTE)]}
        return out

    def to_json(self) -> dict:
        return {
            "next": self._next,
            "alias": self.alias,
            "items": {sid: {"name": sp["name"], "auto": sp["auto"], "n": sp["n"], "sum": None if sp["sum"] is None else sp["sum"].round(5).tolist()} for sid, sp in self.items.items()},
        }

    def load_json(self, d: dict) -> None:
        self._next = d.get("next", 1)
        self.alias = d.get("alias", {})
        self.items = {sid: {"name": sp["name"], "auto": sp["auto"], "n": sp.get("n", 0), "sum": None if sp.get("sum") is None else np.array(sp["sum"], dtype=np.float32)} for sid, sp in d.get("items", {}).items()}
