"""Session state, lore retrieval, and the rolling summarizer.

The summarize/save policy lives here and is driven by game.py after EVERY turn. The model
never decides whether to summarize; it only fills in the summary when the loop asks.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from soft import SoftModel

from config import Config


# ---------------------------------------------------------------- summary schema
class NpcNote(SoftModel):
    npc: str = Field(max_length=60)
    attitude: str = Field(max_length=60)
    reason: str = Field(default="", max_length=200)


class StorySummary(SoftModel):
    recap: str = Field(description="3-6 sentences, past tense, story only. No HP, rolls, or item counts.", max_length=1200)
    open_threads: list[str] = Field(default_factory=list, description="Unresolved quests, promises, mysteries.", max_length=8)
    npc_changes: list[NpcNote] = Field(default_factory=list, max_length=8)
    new_facts: list[str] = Field(default_factory=list, description="Canon established this stretch, one short sentence each.", max_length=8)


SUMMARY_SYSTEM = (
    "You compress an RPG session log into durable story memory. Cover ONLY story: what happened, "
    "who knows what, relationships that changed, unresolved threads. NEVER include hit points, dice "
    "rolls, damage numbers, or inventory counts (the game engine tracks those). Keep names exactly "
    "as written. Merge the previous recap with the new turns into ONE updated recap. "
    "Reply with ONLY a JSON object matching the schema."
)


# ---------------------------------------------------------------- session state
@dataclass
class SessionState:
    summary: str = ""
    open_threads: list[str] = field(default_factory=list)
    npc_notes: dict[str, dict] = field(default_factory=dict)
    canon: list[str] = field(default_factory=list)
    log: list[dict] = field(default_factory=list)   # {"turn","player","dm","events"}
    summarized_upto: int = 0                         # log[:n] is already folded into `summary`
    turn: int = 0
    engine: dict = field(default_factory=dict)       # adapter.export_state()
    # --- hierarchical narrative layer (see narrative.py) ---
    manifest: dict = field(default_factory=dict)     # permanent: theme, goal, antagonist
    day: int = 1
    minute: int = 8 * 60                             # minute of the in-game day
    chunk: int = 0                                   # index of the current 28-day chunk
    plan: dict = field(default_factory=dict)         # current monthly plan: focus, 4 weekly beats, reintroductions
    history: list[dict] = field(default_factory=list)  # one compressed record per finished chunk
    entities: dict[str, dict] = field(default_factory=dict)  # npcs, subplots, factions
    focus: list[str] = field(default_factory=list)   # entities the conversation is currently about
    alerted: list[str] = field(default_factory=list)  # watchdog alerts already announced

    def pending(self) -> list[dict]:
        return self.log[self.summarized_upto:]

    def add_canon(self, fact: str) -> bool:
        fact = " ".join(fact.split())[:300]
        if fact and fact.lower() not in {c.lower() for c in self.canon}:
            self.canon.append(fact)
            return True
        return False


class SessionStore:
    """Atomic JSON snapshot plus an append-only JSONL log (the raw record; summaries are lossy)."""

    def __init__(self, cfg: Config):
        self.dir = Path(cfg.save_dir) / cfg.save_name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.log_path = self.dir / "log.jsonl"
        self.manifest_path = self.dir / "manifest.json"   # permanent campaign file; edit it by hand if you like

    def load(self) -> Optional[SessionState]:
        if not self.state_path.exists():
            return None
        data = json.loads(self.state_path.read_text())
        return SessionState(**{k: v for k, v in data.items() if k in SessionState.__dataclass_fields__})

    def save(self, state: SessionState) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(state), indent=1))
        os.replace(tmp, self.state_path)  # atomic: a crash can't leave a half-written save

    def load_manifest(self) -> Optional[dict]:
        return json.loads(self.manifest_path.read_text()) if self.manifest_path.exists() else None

    def save_manifest(self, manifest: dict) -> None:
        self.manifest_path.write_text(json.dumps(manifest, indent=1))

    def append_log(self, entry: dict) -> None:
        with self.log_path.open("a") as f:
            f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------- summarizer
def est_tokens(obj) -> int:
    return len(json.dumps(obj)) // 4 if not isinstance(obj, str) else len(obj) // 4


def should_summarize(state: SessionState, cfg: Config, force: bool = False) -> bool:
    pending = state.pending()
    if len(pending) <= cfg.summary_keep_recent:
        return False
    if force:
        return True
    return len(pending) >= cfg.summary_every_turns or est_tokens(pending) >= cfg.summary_token_threshold


def summarize(state: SessionState, llm, cfg: Config) -> bool:
    """Fold the older pending turns into the recap. Fails soft: on any error the old summary stays."""
    pending = state.pending()
    keep = cfg.summary_keep_recent
    to_fold = pending[:-keep] if keep else pending
    if not to_fold:
        return False
    turns_text = "\n".join(
        f"[turn {t['turn']}] Player: {t['player']}\n  Outcome notes: {'; '.join(t.get('events', [])[:8])}\n  DM: {t['dm']}"
        for t in to_fold
    )
    msgs = [
        {"role": "system", "content": SUMMARY_SYSTEM},
        {"role": "user", "content": (
            f"PREVIOUS RECAP:\n{state.summary or '(none)'}\n\nOPEN THREADS SO FAR:\n"
            f"{json.dumps(state.open_threads)}\n\nNEW TURNS TO FOLD IN:\n{turns_text}")},
    ]
    result = llm.structured(msgs, StorySummary, effort=cfg.summary_effort)
    if result is None:
        return False
    state.summary = result.recap
    state.open_threads = result.open_threads
    for note in result.npc_changes:
        state.npc_notes[note.npc] = {"attitude": note.attitude, "reason": note.reason}
    for fact in result.new_facts:
        state.add_canon(fact)
    state.summarized_upto += len(to_fold)
    return True


# ---------------------------------------------------------------- lore retrieval (keyword/tag)
_STOP = set("the a an and or of to in on at is it he she they you i me my we our with for from that this "
            "what who where when how about tell ask say says said does did do are was were be been".split())


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", text.lower()) if len(w) > 2 and w not in _STOP}


class LoreStore:
    """One markdown file per entity. First line '# Title', optional 'tags: a, b', then the body."""

    def __init__(self, folder: str | Path):
        self.docs: list[dict] = []
        p = Path(folder)
        if p.exists():
            for f in sorted(p.glob("*.md")):
                lines = f.read_text().splitlines()
                title = lines[0].lstrip("# ").strip() if lines else f.stem
                tags = ""
                body_start = 1
                if len(lines) > 1 and lines[1].lower().startswith("tags:"):
                    tags, body_start = lines[1][5:], 2
                self.docs.append({"title": title, "tags": tags, "body": "\n".join(lines[body_start:]).strip()})

    def search(self, query: str, k: int = 3, max_chars: int = 1200, canon: list[str] | tuple = ()) -> list[str]:
        q = _tokens(query)
        if not q:
            return []
        scored = []
        for d in self.docs:
            s = 3 * len(q & _tokens(d["title"])) + 2 * len(q & _tokens(d["tags"])) + len(q & _tokens(d["body"]))
            if s:
                scored.append((s, f"{d['title']}: {d['body'][:500]}"))
        for fact in canon:
            s = len(q & _tokens(fact))
            if s:
                scored.append((s, f"(canon) {fact}"))
        scored.sort(key=lambda x: -x[0])
        out, used = [], 0
        for _, text in scored[:k]:
            if used + len(text) > max_chars:
                break
            out.append(text)
            used += len(text)
        return out
