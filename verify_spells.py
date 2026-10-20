"""Build spells_verified.json: the catalog of spells the DM may hand to a caster companion.

For every combat-relevant SRD spell (level 0-3) this casts it for real inside the engine and keeps it only if the cast
actually does something (an attack roll, damage, a saving throw, or healing). Re-run after upgrading dnd5e-engine:

    python verify_spells.py
"""
from __future__ import annotations

import glob
import json
import os
import sys

import dnd5e_engine as E
import dnd5e_srd_data as D

from config import Config
from engine_adapter import PC_ID, EngineAdapter

ROOT = os.path.join(os.path.dirname(D.__file__), "canonical", "spells")
COMBAT_KINDS = {"attack", "damage", "save", "heal"}
# Instant, single-turn spells only. Concentration spells, buffs, summons and control effects are left out on purpose:
# the companion AI cannot manage them and the engine cannot apply custom conditions reliably.
ODDBALLS = {"alter-self", "meld-into-stone", "phantasmal-force", "glyph-of-warding", "conjure-animals", "divine-smite",
            "ensnaring-strike", "arcane-vigor", "prayer-of-healing", "revivify", "false-life", "heroism", "shillelagh", "searing-smite"}
MAX_LEVEL = 3
OUT = os.path.join(os.path.dirname(__file__), "spells_verified.json")


def describe(d: dict) -> dict:
    """Pull the facts the DM and the AI need out of the raw spell JSON."""
    acts = d["activities"]
    kinds = sorted({a["kind"] for a in acts} & COMBAT_KINDS)
    types, dice, saves, heal_dice, shape = [], "", [], "", ""
    for a in acts:
        dmg = (a.get("damage") or {}).get("parts") or []
        for p in dmg:
            types += p.get("types", [])
            if not dice and p.get("number"):
                dice = f"{p['number']}d{p['denomination']}" + (f"+{p['bonus']}" if p.get("bonus") not in ("", None, "0") else "")
        if a["kind"] == "save":
            saves += a["save"]["ability"]
        h = a.get("healing")
        if h and h.get("number"):
            heal_dice = f"{h['number']}d{h['denomination']}"
        tpl = (a.get("target") or {}).get("template") or {}
        if tpl.get("type") and not shape:
            shape = f"{tpl['type']} {tpl.get('size', '')}ft".strip()
    r = d.get("range") or {}
    unit, val = r.get("units"), r.get("value")
    if unit == "ft" and val:
        reach = int(val)
    elif unit == "touch":
        reach = 5
    else:                                     # 'self': cones/spheres start at the caster
        sizes = [int(a["target"]["template"]["size"]) for a in acts
                 if (a.get("target") or {}).get("template", {}).get("size", "").isdigit()]
        reach = max(sizes) if sizes else 0
    return {"name": d["name"], "level": d["level"], "kinds": kinds, "damage_types": sorted(set(types)),
            "damage": dice, "heal": heal_dice, "save": sorted(set(saves)), "reach_ft": reach, "shape": shape,
            "bonus_action": (d.get("casting_time") or {}).get("unit") == "bonus",
            "concentration": bool(d.get("concentration"))}


def try_cast(slug: str, info: dict) -> tuple[bool, str]:
    a = EngineAdapter(Config(seed=1))
    a.create_pc("T", class_slug="wizard", level=5, equipment=("quarterstaff",),
                ability_scores={"str": 8, "dex": 14, "con": 14, "int": 18, "wis": 12, "cha": 10})
    heals = "heal" in info["kinds"]
    a.pc = a.pc.model_copy(update={"hp_current": 100 if heals else 500, "hp_max": 500, "spells_known": [slug],
                                   "spell_abilities": {slug: "int"}})
    r = a.start_encounter(["goblin-warrior"] * 3)
    if not r.get("ok"):
        return False, "encounter failed"
    v = E.get_live(a.handle)
    if v.initiative[v.current_turn_index].entity_id != PC_ID:
        a._settle()
    v = E.get_live(a.handle)
    foe = sorted(v.encounter_ids - v.dead_ids)[0]
    dest = a._approach_dest(v, PC_ID, foe)                       # walk up so touch/self-origin spells can reach
    if dest:
        a._act(PC_ID, E.PlayerIntent(intent_type="move", target_zone_id=dest))
    target = PC_ID if heals else foe
    before_hp = E.get_live(a.handle).tracked_hp.get(PC_ID, 0)
    before_slots = dict(E.get_live(a.handle).spell_slots_by_entity.get(PC_ID, {}))
    events = a._act(PC_ID, E.PlayerIntent(intent_type="cast_spell", spell_id=slug, target_id=target,
                                          slot_level=info["level"] or None))
    if events is None:
        return False, "rejected by engine"
    after = E.get_live(a.handle) if a.handle else None
    text = " | ".join(events)
    foe_names = {a.names.get(f, f) for f in v.encounter_ids}
    on_foe = [e for e in events if any(n in e for n in foe_names)]
    effect = []
    if any(e.startswith("T attacks ") for e in events):
        effect.append("attack")
    if any(" damage" in e and "takes" in e for e in on_foe):
        effect.append("damage")
    if any(" save " in e for e in on_foe):
        effect.append("save")
    if heals and after and after.tracked_hp.get(PC_ID, 0) > before_hp:
        effect.append("heal")
    want_heal = heals and not (info["damage_types"] or "attack" in info["kinds"])
    if want_heal and "heal" not in effect:
        return False, "no healing happened"
    if not want_heal and not ({"attack", "damage"} & set(effect) or ("save" in effect and info["damage_types"])):
        return False, "no effect on the enemy"
    if info["level"] and after is not None and after.spell_slots_by_entity.get(PC_ID, {}) == before_slots:
        return False, "slot not spent"
    return True, "+".join(effect)


def main() -> None:
    kept, failed = {}, {}
    for path in sorted(glob.glob(ROOT + "/*.json")):
        d = json.load(open(path))
        if d["level"] > MAX_LEVEL:
            continue
        info = describe(d)
        if not info["kinds"] or d.get("concentration") or d["slug"] in ODDBALLS:
            continue
        if (d.get("casting_time") or {}).get("unit") not in ("action", "bonus"):
            continue
        if not (info["damage_types"] or info["heal"] or "attack" in info["kinds"]):
            continue                                  # save-only control spells: not offered (conditions unreliable)
        slug = d["slug"]
        try:
            ok, why = try_cast(slug, info)
        except Exception as ex:
            ok, why = False, f"{type(ex).__name__}: {str(ex)[:60]}"
        (kept if ok else failed)[slug] = {**info, "verified": why} if ok else why
        print(("OK  " if ok else "FAIL"), f"{slug:28} L{info['level']} {info['kinds']} -> {why}", file=sys.stderr)
    json.dump(kept, open(OUT, "w"), indent=1, sort_keys=True)
    print(f"\n{len(kept)} spells verified, {len(failed)} rejected -> {OUT}")


if __name__ == "__main__":
    main()
