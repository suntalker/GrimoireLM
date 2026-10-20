"""Hierarchical narrative layer: clock, entity profiles, relationships, inactivity watchdog, monthly scheduler.

Everything is deterministic engine code except create_manifest / plan_first / run_transition, which make ONE
structured LLM call each and always fail soft (a fallback plan is built by the engine).

Design goal: spend as little context window as possible. The model sees only
  - the permanent manifest (a few lines)
  - the clock and THIS WEEK's beat (not the whole plan)
  - the last couple of compressed chunk records (older ones are merged into one)
  - profiles of the 1-3 entities actually relevant right now, each with only its own memories
"""
from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, Field

from soft import SoftModel

from config import Config

# ====================================================================== LLM output schemas
class Manifest(SoftModel):
    theme: str = Field(max_length=160)
    goal: str = Field(max_length=200)
    antagonist_name: str = Field(max_length=60)
    antagonist_description: str = Field(max_length=200, description="personality and methods, a few traits")


class WeekBeat(SoftModel):
    week: int = Field(ge=1, le=4)
    beat: str = Field(max_length=260, description="1-2 sentences: what happens or is set in motion this week")
    entities: list[str] = Field(default_factory=list, max_length=4, description="entity ids featured")


class Reintroduction(SoftModel):
    entity_id: str = Field(max_length=60)
    week: int = Field(ge=1, le=4)
    how: str = Field(max_length=200, description="a concrete way to bring them back into the story")


class MonthlyPlan(SoftModel):
    focus: str = Field(max_length=140, description="the month's focus, tied to the campaign goal")
    weeks: list[WeekBeat] = Field(min_length=4, max_length=4)
    reintroductions: list[Reintroduction] = Field(default_factory=list, max_length=12)


class ChunkClose(SoftModel):
    record: str = Field(max_length=700, description="4-7 sentence tight record of the finished month. No HP, dice, or counts.")
    key_facts: list[str] = Field(default_factory=list, max_length=6, description="durable canon facts, one short sentence each")


class ChunkTransition(SoftModel):
    closing: ChunkClose
    plan: MonthlyPlan


class FirstPlan(SoftModel):
    plan: MonthlyPlan


class Compressed(SoftModel):
    record: str = Field(max_length=700)


# ====================================================================== clock
DAY_MINUTES = 1440
PHASES = ((5 * 60, "Morning"), (12 * 60, "Afternoon"), (17 * 60, "Evening"), (21 * 60, "Night"))


def phase_name(minute: int) -> str:
    name = "Night"                      # 00:00-04:59 is still night
    for start, label in PHASES:
        if minute >= start:
            name = label
    return name


def clock_text(state) -> str:
    return f"Day {state.day}, {phase_name(state.minute)} {state.minute // 60:02d}:{state.minute % 60:02d}"


def advance(state, minutes: int) -> int:
    """Move the clock forward. Returns how many day boundaries were crossed."""
    total = state.minute + max(0, int(minutes))
    state.day += total // DAY_MINUTES
    state.minute = total % DAY_MINUTES
    return total // DAY_MINUTES


def chunk_index(day: int, cfg: Config) -> int:
    return (day - 1) // cfg.chunk_days


def week_of_chunk(day: int, cfg: Config) -> int:
    return min(4, ((day - 1) % cfg.chunk_days) // 7 + 1)


def status_line(state, cfg: Config, extra: str = "") -> str:
    icon = "\u263e" if phase_name(state.minute) == "Night" else "\u2600"
    base = f"[{icon} {clock_text(state)} | month {chunk_index(state.day, cfg) + 1}, week {week_of_chunk(state.day, cfg)}/4"
    return base + (f" | {extra}" if extra else "") + "]"


# ====================================================================== relationships
def tier(score: int) -> str:
    if score <= -75:
        return "Sworn Enemy"
    if score <= -25:
        return "Hostile"
    if score <= -6:
        return "Unfriendly"
    if score <= 5:
        return "Neutral"
    if score <= 24:
        return "Friendly"
    if score <= 74:
        return "Trusted"
    return "Devoted"


def _daily_caps(score: int) -> tuple[int, int]:
    """(max gain, max loss) per in-game day. Grudges fade slowly; deep loyalty does not collapse in an afternoon."""
    gain = 3 if score <= -25 else (6 if score <= -6 else 10)
    loss = 5 if score >= 75 else (8 if score >= 25 else 15)
    return gain, loss


def _shift(entity: dict, delta: int, day: int) -> dict:
    """Apply a relationship change with whiplash protection. Returns what actually happened."""
    rel = entity["relationship"]
    today = entity.setdefault("today", {"day": day, "gained": 0, "lost": 0})
    if today["day"] != day:
        today.update(day=day, gained=0, lost=0)
    before, requested = rel["score"], int(delta)
    gain_cap, loss_cap = _daily_caps(before)
    if requested > 0:
        applied = max(0, min(requested, gain_cap - today["gained"]))
        today["gained"] += applied
    elif requested < 0:
        applied = -max(0, min(-requested, loss_cap - today["lost"]))
        today["lost"] += -applied
    else:
        applied = 0
    rel["score"] = max(-100, min(100, before + applied))
    out = {"requested_delta": requested, "applied_delta": applied, "capped": applied != requested,
           "score": rel["score"], "tier": tier(rel["score"])}
    if out["capped"]:
        out["note"] = (f"{entity['name']} is {tier(before)}; attitudes shift slowly. Only {applied:+d} applied today. "
                       "Narrate at most a small change in manner.")
    if tier(before) != out["tier"]:
        out["tier_changed_from"] = tier(before)
    return out


# ====================================================================== entities
KINDS = ("npc", "subplot", "faction")
LIMITS = {"npc": 40, "subplot": 10, "faction": 12}        # counted among LIVING entities only
TERMINAL = ("dead", "disbanded", "resolved")             # finished for good: no watchdog, no scheduling, no scenes
STATUSES = ("active", "dormant") + TERMINAL
TERMINAL_FOR = {"npc": "dead", "faction": "disbanded", "subplot": "resolved"}
MAX_MEMORIES = 12


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "entity"


def find(state, key: str) -> Optional[dict]:
    """Resolve an id, a name, or an alias (models are sloppy about which they pass)."""
    if key in state.entities:
        return state.entities[key]
    k = _norm(key)
    if not k:
        return None
    exact = [e for e in state.entities.values()
             if k in (_norm(e["name"]), _norm(e["id"])) or k in {_norm(a) for a in e.get("aliases", [])}]
    if len(exact) == 1:
        return exact[0]
    partial = [e for e in state.entities.values() if k in _norm(e["name"]) or _norm(e["name"]) in k]
    return partial[0] if len(partial) == 1 else None


def create_entity(state, cfg: Config, kind: str, name: str, core: str, objectives: list[str] | tuple = (),
                  current_state: str = "", future_plans: str = "", reputation: str = "", score: int = 0,
                  stats: Optional[dict] = None) -> dict:
    """Register an NPC / subplot / faction. `core` is the IMMUTABLE anchor: personality (npc), ideology (faction), premise (subplot)."""
    if kind not in KINDS:
        return {"ok": False, "error": f"Unknown kind '{kind}'. Use one of {KINDS}."}
    name = " ".join(str(name).split())[:60]
    core = " ".join(str(core).split())[:240]
    if not name or not core:
        return {"ok": False, "error": "An entity needs a name and a core description (personality / ideology / premise)."}
    if sum(1 for e in state.entities.values() if e["kind"] == kind and e["status"] not in TERMINAL) >= LIMITS[kind]:
        return {"ok": False, "error": f"Too many {kind}s already ({LIMITS[kind]}). Update an existing one instead."}
    existing = find(state, name)
    if existing and _norm(existing["name"]) == _norm(name):
        gone = f" It is {existing['status']} (since day {existing.get('ended_day', '?')}) and stays that way." \
            if existing["status"] in TERMINAL else ""
        return {"ok": False, "error": f"'{name}' already exists as {existing['id']}.{gone} Use interact or update_entity.",
                "existing_id": existing["id"]}
    base, eid, n = _slug(name), "", 2
    eid = base
    while eid in state.entities:
        eid, n = f"{base}-{n}", n + 1
    words = name.split()
    aliases = [words[0]] if len(words) > 1 and len(words[0]) >= 4 and words[0].lower() not in ("the", "sir", "lady", "lord") else []
    e = {
        "id": eid, "kind": kind, "name": name, "aliases": aliases, "core": core,
        "objectives": [" ".join(str(o).split())[:120] for o in list(objectives)[:3]],
        "current_state": str(current_state)[:160], "future_plans": str(future_plans)[:200],
        "reputation": str(reputation)[:120], "memories": [], "created_day": state.day,
        "last_interaction_day": state.day, "status": "active", "relations": {},
    }
    if kind in ("npc", "faction"):
        e["relationship"] = {"score": max(-30, min(30, int(score)))}
    state.entities[eid] = e
    out = {"ok": True, "id": eid, "name": name, "kind": kind}
    if stats:
        out["stat_block"] = attach_stats(state, eid, stats).get("stat_block", "rejected (only NPCs have stat blocks)")
    return out


def interact(state, cfg: Config, key: str, event: str, delta: int = 0) -> dict:
    """Record that the player meaningfully dealt with an entity: refreshes the watchdog, stores a memory,
    and applies a relationship change that the engine caps (no emotional whiplash)."""
    e = find(state, key)
    if e is None:
        return {"ok": False, "error": f"No entity '{key}'.", "known": sorted(state.entities)[:40]}
    if e["status"] in TERMINAL:
        return {"ok": False, "error": f"{e['name']} is {e['status']} (since day {e.get('ended_day', '?')}: "
                                      f"{e.get('epitaph', 'no details')}). They cannot take part in scenes. Only if the story "
                                      "truly revives them, call update_entity(status='active', reason=...)."}
    quiet = state.day - e["last_interaction_day"]
    e["last_interaction_day"] = state.day
    e["memories"].append({"day": state.day, "text": " ".join(str(event).split())[:200]})
    del e["memories"][:-MAX_MEMORIES]
    out = {"ok": True, "entity": e["name"], "id": e["id"], "quiet_days_before": quiet}
    if "relationship" in e:
        out.update(_shift(e, delta, state.day))
    state.focus = [e["id"]] + [f for f in state.focus if f != e["id"]][:1]
    if e["id"] in state.alerted:
        state.alerted.remove(e["id"])
    return out


def _purge_from_plan(state, eid: str) -> None:
    plan = state.plan or {}
    plan["reintroductions"] = [r for r in plan.get("reintroductions", []) if r["entity_id"] != eid]
    for w in plan.get("weeks", []):
        w["entities"] = [x for x in w.get("entities", []) if x != eid]


def _end(state, e: dict, status: str, reason: str) -> None:
    """Retire an entity for good: it stops being tracked, scheduled, alerted or injected as a living presence."""
    e["status"], e["ended_day"], e["epitaph"] = status, state.day, reason[:160]
    e["future_plans"], e["objectives"] = "", []
    del e["memories"][:-3]                                   # keep a few memories; the rest only costs context
    state.focus = [f for f in state.focus if f != e["id"]]
    if e["id"] in state.alerted:
        state.alerted.remove(e["id"])
    _purge_from_plan(state, e["id"])


def mark_fallen(state, names: list[str], cause: str = "Slain in combat by the player.") -> list[str]:
    """Combat bridge: a defeated monster whose name EXACTLY matches a living tracked NPC means that NPC is dead."""
    died = []
    for n in names:
        k = _norm(n)
        hit = [e for e in state.entities.values()
               if e["kind"] == "npc" and e["status"] not in TERMINAL and k in (_norm(e["name"]), _norm(e["id"]))]
        if len(hit) == 1:
            _end(state, hit[0], "dead", cause)
            died.append(hit[0]["name"])
    return died


def update_entity(state, key: str, current_state: str = "", future_plans: str = "", objectives: Optional[list] = None,
                  status: str = "", relation_with: str = "", relation_delta: int = 0, reason: str = "") -> dict:
    """Change the MUTABLE parts only. The core anchor (personality / ideology / premise) can never be edited."""
    e = find(state, key)
    if e is None:
        return {"ok": False, "error": f"No entity '{key}'.", "known": sorted(state.entities)[:40]}
    changed = []
    reason = " ".join(str(reason).split())
    if status:
        if status not in STATUSES:
            return {"ok": False, "error": f"status must be one of {STATUSES}."}
        if status in TERMINAL:
            if status != TERMINAL_FOR[e["kind"]]:
                return {"ok": False, "error": f"A {e['kind']} ends as '{TERMINAL_FOR[e['kind']]}', not '{status}'."}
            if e["status"] in TERMINAL:
                return {"ok": False, "error": f"{e['name']} is already {e['status']}."}
            if len(reason) < 3:
                return {"ok": False, "error": f"Say how or why in `reason` (e.g. how {e['name']} {status.replace('dead', 'died')})."}
            _end(state, e, status, reason)
            return {"ok": True, "entity": e["name"], "changed": ["status"], "status": status,
                    "note": f"{e['name']} is permanently {status}. They can no longer appear, speak, or be tracked."}
        if e["status"] in TERMINAL:                          # reviving the dead must be deliberate and on the record
            if len(reason) < 10:
                return {"ok": False, "error": f"{e['name']} is {e['status']}. Reviving them needs a concrete in-story `reason` "
                                              "(a ritual, a spell, a twist the player witnessed)."}
            e["status"] = status
            e.pop("ended_day", None)
            e.pop("epitaph", None)
            e["last_interaction_day"] = state.day
            e["memories"].append({"day": state.day, "text": f"Returned: {reason}"[:200]})
            changed.append("status(revived)")
        else:
            e["status"] = status
            changed.append("status")
    if current_state:
        e["current_state"] = str(current_state)[:160]
        changed.append("current_state")
    if future_plans:
        e["future_plans"] = str(future_plans)[:200]
        changed.append("future_plans")
    if objectives:
        e["objectives"] = [" ".join(str(o).split())[:120] for o in objectives[:3]]
        changed.append("objectives")
    out: dict = {"ok": True, "entity": e["name"], "changed": changed}
    if relation_with and relation_delta:
        other = find(state, relation_with)
        if other is None or other["id"] == e["id"]:
            return {"ok": False, "error": f"Unknown relation target '{relation_with}'."}
        today = e.setdefault("rel_today", {})
        used = today.get(other["id"], {"day": state.day, "moved": 0})
        if used["day"] != state.day:
            used = {"day": state.day, "moved": 0}
        room = max(0, 10 - used["moved"])                       # a relation moves at most 10 points per day
        applied = max(-room, min(room, int(relation_delta)))
        used["moved"] += abs(applied)
        today[other["id"]] = used
        score = max(-100, min(100, e["relations"].get(other["id"], 0) + applied))
        e["relations"][other["id"]] = score
        out.update(relation=f"{e['name']} -> {other['name']}: {score:+d} ({tier(score)})", relation_applied=applied)
    if not changed and "relation" not in out:
        return {"ok": False, "error": "Nothing to change. Provide at least one field."}
    return out


STAT_KEYS = ("hp_max", "ac", "attack_bonus", "damage_dice", "damage_type", "dexterity")


def attach_stats(state, key: str, stats: dict) -> dict:
    """Store an NPC's combat stat block in Python state (never in the model's context). Fixed once set."""
    e = find(state, key)
    if e is None:
        return {"ok": False, "error": f"No entity '{key}'."}
    if e["kind"] != "npc":
        return {"ok": False, "error": "Only NPCs have stat blocks."}
    if e.get("stats"):
        return {"ok": False, "error": f"{e['name']} already has a stored stat block; it is fixed so they stay consistent."}
    e["stats"] = {**{k: stats[k] for k in STAT_KEYS}, "hp_current": stats["hp_max"], "last_fight_day": 0}
    return {"ok": True, "entity": e["name"], "stat_block": "stored"}


def foe_blocks(state, keys: list[str]) -> tuple[list[dict], Optional[dict]]:
    """Resolve tracked NPCs into combat entries. Their remembered wounds carry over until a full day has passed."""
    out = []
    for k in keys:
        e = find(state, k)
        if e is None:
            return [], {"ok": False, "error": f"No tracked entity '{k}'.",
                        "with_stat_blocks": [x["name"] for x in state.entities.values() if x.get("stats")]}
        if e["status"] in TERMINAL:
            return [], {"ok": False, "error": f"{e['name']} is {e['status']} and cannot fight."}
        st = e.get("stats")
        if not st:
            return [], {"ok": False, "error": f"{e['name']} has no stat block yet. The first time they fight, start_encounter with a custom "
                                               "monster NAMED after them: those numbers become their fixed stat block."}
        rested = state.day > st.get("last_fight_day", 0)
        out.append({"name": e["name"], **{k2: st[k2] for k2 in STAT_KEYS},
                    "hp_start": st["hp_max"] if rested else max(1, st["hp_current"])})
    return out, None


def record_foe_results(state, foe_hp: dict) -> list[str]:
    """After a fight: remember how hurt each tracked NPC is. Returns the survivors (for the DM to narrate their fate)."""
    survivors = []
    for name, hp in foe_hp.items():
        hit = [e for e in state.entities.values() if e.get("stats") and _norm(e["name"]) == _norm(name)]
        if len(hit) != 1:
            continue
        e = hit[0]
        e["stats"]["hp_current"] = max(0, int(hp))
        e["stats"]["last_fight_day"] = state.day
        if hp > 0 and e["status"] not in TERMINAL:
            survivors.append(f"{e['name']} ({int(hp)}/{e['stats']['hp_max']} HP left)")
    return survivors


def stats_report(state, key: str) -> str:
    e = find(state, key)
    if e is None:
        return f"No tracked entity '{key}'."
    st = e.get("stats")
    if not st:
        return f"{e['name']} ({e['kind']}) has no stored stat block."
    rest = "recovers fully after a day without fighting"
    gone = f"  [{e['status'].upper()}]" if e["status"] in TERMINAL else ""
    return (f"{e['name']} (npc){gone}\n  HP {st['hp_current']}/{st['hp_max']} ({rest})   AC {st['ac']}   "
            f"Attack +{st['attack_bonus']}   Damage {st['damage_dice']} {st['damage_type']}   DEX {st['dexterity']}\n"
            f"  Last fought: {'never' if not st.get('last_fight_day') else 'day ' + str(st['last_fight_day'])}")


def touch_mentions(state, text: str) -> list[str]:
    """Safety net: story presence refreshes the watchdog even if the model forgot to call interact."""
    ids = mentioned(state, text, limit=12)
    for i in ids:
        e = state.entities[i]
        if e["status"] in TERMINAL:
            continue
        e["last_interaction_day"] = state.day
        if i in state.alerted:
            state.alerted.remove(i)
    return ids


def mentioned(state, text: str, limit: int = 3) -> list[str]:
    t = " " + _norm(text) + " "
    hits = []
    for e in state.entities.values():
        names = [_norm(e["name"])] + [_norm(a) for a in e.get("aliases", [])]
        if any(n and f" {n} " in t for n in names):
            hits.append(e)
    hits.sort(key=lambda e: -e["last_interaction_day"])
    return [e["id"] for e in hits[:limit]]


def profile_text(state, key_or_entity, max_memories: int = 4) -> str:
    """Compact behavioral anchor + ONLY this entity's own memories. Injected only when the entity is relevant."""
    e = key_or_entity if isinstance(key_or_entity, dict) else find(state, key_or_entity)
    if e is None:
        return ""
    if e["status"] in TERMINAL:
        tail = (" memories: " + " ".join(f"[d{m['day']}] {m['text']}" for m in e["memories"][-2:])) if e["memories"] else ""
        return (f"{e['kind'].upper()} {e['name']} [{e['id']}] | {e['status'].upper()} since day {e.get('ended_day', '?')}: "
                f"{e.get('epitaph', '')} | GONE FOR GOOD: cannot act, speak or appear; at most a memory, corpse, grave or legacy.{tail}")
    head = f"{e['kind'].upper()} {e['name']} [{e['id']}]"
    if "relationship" in e:
        sc = e["relationship"]["score"]
        head += f" | toward player: {tier(sc)} ({sc:+d})"
    label = {"npc": "personality", "faction": "ideology", "subplot": "premise"}[e["kind"]]
    parts = [head, f"{label} (fixed): {e['core']}"]
    if e["objectives"]:
        parts.append("wants: " + "; ".join(e["objectives"]))
    if e["current_state"]:
        parts.append("now: " + e["current_state"])
    if e["future_plans"]:
        parts.append("plans: " + e["future_plans"])
    if e["reputation"]:
        parts.append("reputation: " + e["reputation"])
    if e["kind"] == "subplot":
        parts.append(f"status: {e['status']}")
    if e.get("stats"):
        parts.append("stat block: stored in the engine (fight them with start_encounter foes=[name])")
    if e.get("relations"):
        parts.append("relations: " + ", ".join(
            f"{state.entities[o]['name'] if o in state.entities else o} {s:+d} ({tier(s)})" for o, s in e["relations"].items()))
    if e["memories"]:
        parts.append("memories: " + " ".join(f"[d{m['day']}] {m['text']}" for m in e["memories"][-max_memories:]))
    return " | ".join(parts)


# ====================================================================== inactivity watchdog
def watchdog(state, cfg: Config) -> list[dict]:
    """Entities ignored for >= cfg.watchdog_days. Resolved subplots are exempt."""
    out = []
    for e in state.entities.values():
        if e["status"] in TERMINAL:
            continue
        quiet = state.day - e["last_interaction_day"]
        if quiet >= cfg.watchdog_days:
            out.append({"id": e["id"], "name": e["name"], "kind": e["kind"], "days": quiet})
    return sorted(out, key=lambda a: -a["days"])


def _scheduled_ids(state) -> set[str]:
    return {r["entity_id"] for r in (state.plan or {}).get("reintroductions", [])}


def format_entities(state, cfg: Config) -> str:
    if not state.entities:
        return "(no tracked entities yet)"
    rows = []
    for e in sorted(state.entities.values(), key=lambda e: (e["kind"], -(state.day - e["last_interaction_day"]))):
        quiet = state.day - e["last_interaction_day"]
        gone = e["status"] in TERMINAL
        flag = "  !! WATCHDOG" if quiet >= cfg.watchdog_days and not gone else ""
        rel = (f" {e['status'].upper()} (d{e.get('ended_day', '?')})" if gone else
               f" {tier(e['relationship']['score'])} ({e['relationship']['score']:+d})" if "relationship" in e else f" {e['status']}")
        rows.append(f"{e['kind']:8} {e['name']:28}{rel:22} quiet {quiet:>3}d{flag}{'  [stats]' if e.get('stats') else ''}" if not gone else f"{e['kind']:8} {e['name']:28}{rel:22}{(' - ' + e.get('epitaph', '')) if e.get('epitaph') else ''}")
    return "\n".join(rows)


# ====================================================================== prompt blocks (context-lean)
def prompt_blocks(state, cfg: Config, player_text: str) -> list[str]:
    blocks = []
    m = state.manifest
    if m:
        blocks.append(f"CAMPAIGN (permanent): theme: {m['theme']} | goal: {m['goal']} | antagonist: "
                      f"{m['antagonist_name']} ({m['antagonist_description']})")
    wk = week_of_chunk(state.day, cfg)
    blocks.append(f"TIME: {clock_text(state)} (month {chunk_index(state.day, cfg) + 1}, week {wk}/4)")
    plan = state.plan or {}
    if plan:
        beat = next((w for w in plan.get("weeks", []) if w["week"] == wk), None)
        lines = [f"THIS MONTH'S FOCUS: {plan.get('focus', '')}"]
        if beat:
            names = [state.entities[i]["name"] for i in beat.get("entities", []) if i in state.entities]
            lines.append(f"THIS WEEK'S BEAT (steer toward it naturally, never railroad): {beat['beat']}"
                         + (f" Featured: {', '.join(names)}." if names else ""))
        for r in plan.get("reintroductions", []):
            if r["week"] == wk and r["entity_id"] in state.entities:
                lines.append(f"SCHEDULED REINTRODUCTION: {state.entities[r['entity_id']]['name']} - {r['how']}")
        blocks.append("\n".join(lines))
    unscheduled = [a for a in watchdog(state, cfg) if a["id"] not in _scheduled_ids(state)]
    if unscheduled:
        blocks.append("CRITICAL WATCHDOG ALERT (ignored too long; work them back into the story soon): " +
                      "; ".join(f"{a['name']} ({a['kind']}, {a['days']} days)" for a in unscheduled[:4]))
    gone = [e for e in state.entities.values() if e["status"] in ("dead", "disbanded")]
    if gone:
        blocks.append("THE DEAD AND DISBANDED (they stay gone; never have them act, speak or return alive): " +
                      ", ".join(f"{e['name']} ({e['status']})" for e in gone[-8:]))
    hist = state.history
    if hist:
        shown = hist[-cfg.history_in_prompt:]
        if hist[0].get("merged") and hist[0] not in shown:
            shown = [hist[0]] + shown
        blocks.append("HISTORY (compressed):\n" + "\n".join(f"- days {h['days']}: {h['record']}" for h in shown))
    ids = mentioned(state, player_text, limit=cfg.profiles_in_prompt)
    for f in state.focus:
        if f in state.entities and f not in ids and len(ids) < cfg.profiles_in_prompt:
            ids.append(f)
    if ids:
        blocks.append("PEOPLE & FACTIONS IN FOCUS (stay true to these; do not contradict the fixed traits or the relationship tier):\n"
                      + "\n".join(profile_text(state, i) for i in ids))
    return blocks


# ====================================================================== LLM planner
PLAN_SYSTEM = (
    "You are the story planner for a long-running fantasy RPG campaign. Reply with ONLY a JSON object matching the schema. "
    "The plan covers the next {days} in-game days as 4 weekly beats that advance the campaign goal and the main antagonist's "
    "schemes, build on the history, and react to what the player actually did. A beat is 1-2 sentences of what happens or is "
    "set in motion (no scripted dialogue). Use entity ids exactly as given. Never contradict entity profiles or history. "
    "Every entity under WATCHDOG MUST appear in `reintroductions` with a concrete way and week. "
    "Entities marked DEAD, DISBANDED or RESOLVED are finished for good: never schedule, feature or resurrect them."
)
CLOSE_NOTE = (" Also fill `closing.record`: a tight 4-7 sentence record of the month that just ended (decisions, consequences, "
              "relationship shifts, unresolved threads; NO hit points, dice or counts) and `closing.key_facts` (durable canon).")
COMPRESS_SYSTEM = ("Merge these campaign records into ONE tight record (max 700 characters) keeping names, major decisions, "
                   "consequences and unresolved threads. Reply with ONLY a JSON object.")
MANIFEST_SYSTEM = ("Create a campaign manifest for a fantasy RPG. Reply with ONLY a JSON object. Keep the player's wishes "
                   "verbatim where given; invent the rest to fit. The antagonist must be someone the hero can meet and oppose.")
GENERIC_BEATS = ("The antagonist's influence becomes visible and the hero is drawn in.",
                 "A setback or complication tied to the campaign goal.",
                 "New information about the antagonist's methods surfaces.",
                 "A decision point: confront, bargain, or retreat.")


def _entity_digest(state, cfg: Config) -> str:
    lines = []
    for e in list(state.entities.values())[:60]:
        rel = e["status"].upper() if e["status"] in TERMINAL else (
            f"{tier(e['relationship']['score'])}" if "relationship" in e else e["status"])
        lines.append(f"{e['id']} | {e['kind']} | {e['name']} | {rel} | quiet {state.day - e['last_interaction_day']}d | "
                     f"{e['current_state'][:70]}")
    return "\n".join(lines) or "(none)"


def _plan_messages(state, cfg: Config, closing: bool, alerts: list[dict]) -> list[dict]:
    hist = "\n".join(f"- days {h['days']}: {h['record']}" for h in state.history) or "(none yet)"
    turns = "\n".join(f"[day {t.get('day', '?')}] P: {t['player'][:120]} | DM: {t['dm'][:200]}" for t in state.pending()[-12:])
    wd = "\n".join(f"- {a['id']} ({a['name']}, {a['kind']}): ignored {a['days']} days" for a in alerts) or "(none)"
    user = (f"CAMPAIGN:\n{state.manifest}\n\nHISTORY (oldest first):\n{hist}\n\nTHE MONTH JUST ENDED (recap): "
            f"{state.summary or '(none)'}\nRECENT TURNS:\n{turns or '(none)'}\n\nENTITIES:\n{_entity_digest(state, cfg)}\n\n"
            f"WATCHDOG (must be reintroduced):\n{wd}\n\nToday is day {state.day}. Plan days {state.day}-{state.day + cfg.chunk_days - 1}.")
    sys = PLAN_SYSTEM.format(days=cfg.chunk_days) + (CLOSE_NOTE if closing else "")
    return [{"role": "system", "content": sys}, {"role": "user", "content": user}]


def _fallback_plan(state, alerts: list[dict]) -> dict:
    goal = (state.manifest or {}).get("goal", "the hero's goal")
    return {"focus": f"Advance: {goal}"[:140],
            "weeks": [{"week": i + 1, "beat": GENERIC_BEATS[i], "entities": []} for i in range(4)],
            "reintroductions": []}


def _finalize_plan(plan: Optional[MonthlyPlan], state, alerts: list[dict]) -> dict:
    """Engine-side repair: valid ids only, four numbered weeks, and EVERY watchdog alert reintroduced."""
    live = {i for i, e in state.entities.items() if e["status"] not in TERMINAL}
    d = _fallback_plan(state, alerts) if plan is None else {
        "focus": plan.focus,
        "weeks": [{"week": w.week, "beat": w.beat, "entities": [e for e in w.entities if e in live]}
                  for w in sorted(plan.weeks, key=lambda w: w.week)],
        "reintroductions": [r.model_dump() for r in plan.reintroductions if r.entity_id in live],
    }
    weeks = {w["week"]: w for w in d["weeks"]}
    d["weeks"] = [weeks.get(i) or {"week": i, "beat": GENERIC_BEATS[i - 1], "entities": []} for i in range(1, 5)]
    have = {r["entity_id"] for r in d["reintroductions"]}
    for n, a in enumerate(a for a in alerts if a["id"] not in have):
        d["reintroductions"].append({"entity_id": a["id"], "week": (n % 4) + 1,
                                     "how": f"Bring {a['name']} back into the story naturally; they have been ignored for {a['days']} days."})
    return d


def _call_planner(state, llm, cfg: Config, closing: bool, alerts: list[dict]):
    model = ChunkTransition if closing else FirstPlan
    msgs = _plan_messages(state, cfg, closing, alerts)
    out = llm.structured(msgs, model, effort=cfg.planner_effort)
    if out is not None and alerts:
        got = {r.entity_id for r in out.plan.reintroductions}
        missing = [a["id"] for a in alerts if a["id"] not in got]
        if missing:                                   # one explicit re-ask before the engine repairs it
            msgs = msgs + [{"role": "user", "content": "Your plan omitted required reintroductions for: " + ", ".join(missing) +
                            ". Return the full JSON again including them."}]
            out = llm.structured(msgs, model, effort=cfg.planner_effort) or out
    return out


def plan_first(state, llm, cfg: Config) -> None:
    alerts = watchdog(state, cfg)
    out = _call_planner(state, llm, cfg, False, alerts)
    state.plan = {"chunk": chunk_index(state.day, cfg), "start_day": state.day,
                  **_finalize_plan(out.plan if out else None, state, alerts)}
    state.chunk = chunk_index(state.day, cfg)


def run_transition(state, llm, cfg: Config) -> dict:
    """Close the finished month: episodic record -> history, wipe raw turns, plan the next 28 days."""
    alerts = watchdog(state, cfg)
    old_chunk = state.chunk
    start = old_chunk * cfg.chunk_days + 1
    out = _call_planner(state, llm, cfg, True, alerts)
    if out is not None:
        record, facts, plan = out.closing.record, out.closing.key_facts, out.plan
    else:                                             # fail soft: keep the rolling recap as the record
        record = (state.summary or "A quiet month.")[:700]
        facts, plan = [], None
    state.history.append({"chunk": old_chunk, "days": f"{start}-{(old_chunk + 1) * cfg.chunk_days}", "record": record})
    for f in facts:
        state.add_canon(f)
    _compress_history(state, llm, cfg)
    state.log = []                                    # raw turns are wiped (the JSONL on disk keeps them)
    state.summarized_upto = 0
    state.summary = ""
    state.open_threads = []
    state.chunk = chunk_index(state.day, cfg)
    state.plan = {"chunk": state.chunk, "start_day": state.chunk * cfg.chunk_days + 1, **_finalize_plan(plan, state, alerts)}
    return {"closed_chunk": old_chunk, "alerts": [a["name"] for a in alerts], "llm_ok": out is not None}


def _compress_history(state, llm, cfg: Config) -> None:
    over = len(state.history) - cfg.max_history_chunks
    if over <= 0:
        return
    old = state.history[: over + 1]
    text = "\n".join(f"- (days {h['days']}) {h['record']}" for h in old)
    res = llm.structured([{"role": "system", "content": COMPRESS_SYSTEM}, {"role": "user", "content": text}],
                         Compressed, effort=cfg.summary_effort)
    record = res.record if res else " ".join(h["record"][:150] for h in old)[:700]
    first, last = old[0]["days"].split("-")[0], old[-1]["days"].split("-")[-1]
    state.history = [{"chunk": old[0]["chunk"], "days": f"{first}-{last}", "record": record, "merged": True}] + state.history[over + 1:]


# ====================================================================== campaign start
def create_manifest(llm, cfg: Config, theme: str = "", goal: str = "", antagonist: str = "") -> dict:
    """Fill any blanks with ONE structured call; fall back to plain defaults. The result is permanent."""
    given = {"theme": theme.strip(), "goal": goal.strip(), "antagonist": antagonist.strip()}
    if all(given.values()):
        return {"theme": given["theme"][:160], "goal": given["goal"][:200], "antagonist_name": given["antagonist"][:60],
                "antagonist_description": "A cunning and dangerous adversary."}
    msgs = [{"role": "system", "content": MANIFEST_SYSTEM},
            {"role": "user", "content": f"Player wishes (blank = invent): {given}"}]
    out = llm.structured(msgs, Manifest, effort=cfg.planner_effort)
    if out is not None:
        m = out.model_dump()
        if given["theme"]:
            m["theme"] = given["theme"][:160]
        if given["goal"]:
            m["goal"] = given["goal"][:200]
        if given["antagonist"]:
            m["antagonist_name"] = given["antagonist"][:60]
        return m
    return {"theme": given["theme"] or "A land on the edge of ruin", "goal": given["goal"] or "Stop the rising darkness",
            "antagonist_name": given["antagonist"] or "The Shadow King",
            "antagonist_description": "A patient, ruthless schemer."}


def seed_campaign(state, llm, cfg: Config, manifest: dict) -> None:
    """New campaign: store the manifest, track the antagonist and the main quest, make the first monthly plan."""
    state.manifest = dict(manifest)
    state.day, state.minute, state.chunk = 1, cfg.start_hour * 60, 0
    if not state.entities:
        made = create_entity(state, cfg, "npc", manifest["antagonist_name"], manifest["antagonist_description"],
                             objectives=[f"Defeat the hero and secure: {manifest['goal'][:80]}"],
                             current_state="Scheming from the shadows.", reputation="Feared", score=-30)
        if made.get("ok"):
            state.entities[made["id"]]["relationship"]["score"] = -50   # the engine may seed a deeper enmity than the DM can

        create_entity(state, cfg, "subplot", "The Main Quest", manifest["goal"], objectives=[manifest["goal"][:120]],
                      current_state="Just beginning.")
    if not state.plan:
        plan_first(state, llm, cfg)
