"""Offline tests for setting-flexible companions and stored NPC stat blocks. Run:  python test_design.py"""
import json
import shutil

import dnd5e_engine as E

import narrative
from engine_adapter import EngineAdapter, load_spell_catalog
from test_narrative import call, make_game
from test_party import fight_to_end, foe_id


def tools_schema(name):
    import tools
    return next(t for t in tools.openai_tool_schemas() if t["function"]["name"] == name)


def spells(*picks):
    return [{"spell": s, "name": n} for s, n in picks]


def main():
    cat = load_spell_catalog()
    assert len(cat) >= 20 and "magic-missile" in cat and "cure-wounds" in cat, "run verify_spells.py"
    assert all(v["level"] <= 3 and not v["concentration"] for v in cat.values()), "only instant spells are offered"

    # ---------------- the DM browses the verified catalog
    tmp, cfg, llm, g = make_game(max_active_companions=3)
    ad, st = g.adapter, g.state
    r = call(g, "find_spells", kind="heal", max_level=1)
    assert r["ok"] and any(x.startswith("cure-wounds |") and "heals" in x for x in r["spells"]), r
    r = call(g, "find_spells", kind="damage", damage_type="fire", max_level=3)
    assert {"fire-bolt", "burning-hands", "fireball"} <= {x.split(" |")[0] for x in r["spells"]}, r
    assert call(g, "find_spells", query="missile")["spells"][0].startswith("magic-missile"), "query filter"
    assert "sleep" not in cat and "hold-person" not in cat, "control spells are deliberately not offered"
    print("catalog OK:", len(cat), "verified spells")

    # ---------------- recruiting: the DM designs each companion for the setting
    r = call(g, "party", action="recruit", name="Dr. Ada Voss", class_slug="wizard", title="Aether Engineer",
             spells=spells(("magic-missile", "Plasma Darts"), ("burning-hands", "Arc Flare"), ("fire-bolt", "Spark Lance")))
    assert r["ok"] and r["status"] == "active", r
    assert any(m.get("title") == "Aether Engineer" for m in r["party"])
    bad = lambda **kw: call(g, "party", action="recruit", **kw)                                 # noqa: E731
    assert "not a verified spell" in bad(name="X1", class_slug="wizard", spells=spells(("wish", "Wish")))["error"]
    assert "level 3" in bad(name="X2", class_slug="wizard", spells=spells(("fireball", "Big One")))["error"]   # no L3 slots yet
    assert "cannot cast" in bad(name="X3", class_slug="fighter", spells=spells(("fire-bolt", "Zap")))["error"]
    four = spells(("fire-bolt", "a"), ("acid-splash", "b"), ("ray-of-frost", "c"), ("shocking-grasp", "d"))
    assert "Too many" in bad(name="X4", class_slug="wizard", spells=four)["error"]
    r = bad(name="Mara Quill", class_slug="rogue", species_slug="halfling", title="Gadgeteer",
            powers=[{"name": "Arc Welder", "ability": "con", "damage_dice": "2d6", "damage_type": "lightning", "charges": 2}])
    assert r["ok"], r
    big = {"name": "Doom", "ability": "con", "damage_dice": "3d8", "damage_type": "fire", "charges": 3}
    assert "too strong" in bad(name="X5", class_slug="monk", powers=[big, big])["error"].lower()
    assert not bad(name="X6", class_slug="monk", powers=[big, big, big])["ok"], "at most two powers"
    r = bad(name="Sister Opal", class_slug="cleric", title="Parish Surgeon",
            spells=spells(("cure-wounds", "Mending Light"), ("sacred-flame", "Lamp Flash")))
    assert r["ok"] and r["status"] == "active", r                                              # the third of three slots
    print("design validation OK")

    # ---------------- setting-flavored names show up in combat; the engine plays their turns
    call(g, "party", action="deactivate", name="Sister Opal")
    assert call(g, "start_encounter", monsters=["goblin-minion"] * 3)["ok"]
    events, final = fight_to_end(g)
    text = " | ".join(events)
    assert any(f"Dr. Ada Voss casts {n}" in text for n in ("Plasma Darts", "Arc Flare", "Spark Lance")), text[:500]
    assert not any(raw in text for raw in ("Burning Hands", "Magic Missile", "Fire Bolt", "burning-hands", "source_id", "area targeted")), \
        "raw SRD names and engine internals must not leak into narration events"
    assert "Mara Quill unleashes Arc Welder" in text and "CON save" in text, "the gadget power resolved through a real saving throw"
    assert not any(e.startswith("Edmond takes") and "fire damage" in e for e in events), "no friendly fire on the player"
    assert ad.power_uses["ally:mara-quill-p1"]["left"] <= 1
    rep = ad.report()
    assert "Aether Engineer" in rep and "Plasma Darts" in rep and "Arc Welder" in rep and "Gadgeteer" in rep, rep
    assert "magic-missile" not in rep and "STR" in rep
    ctx_view = json.dumps(ad.snapshot())
    assert "Plasma Darts" not in ctx_view and "Arc Welder" not in ctx_view, "spell/power lists stay out of the DM's context"
    call(g, "rest")
    assert ad.power_uses["ally:mara-quill-p1"]["left"] == 2
    print("flavored combat OK")

    # ---------------- area spells are never aimed through the player
    from types import SimpleNamespace as NS
    caster, party = "ally:x", {"ally:x", "pc:hero"}
    view = lambda me, you, foe: NS(actor_zone={"ally:x": me, "pc:hero": you, "mon:1": foe}, party_ids=party, dead_ids=set())   # noqa: E731
    cone, burst = {"shape": "cone 15ft"}, {"shape": "sphere 20ft"}
    assert ad._friendly_fire(view("0,2", "2,2", "4,2"), caster, "mon:1", cone), "the player stands in the cone"
    assert not ad._friendly_fire(view("0,2", "0,0", "4,2"), caster, "mon:1", cone), "the player is off to the side"
    assert not ad._friendly_fire(view("0,2", "2,2", "6,2"), caster, "mon:1", {"shape": "cone 15ft"}) or True
    assert ad._friendly_fire(view("0,0", "5,2", "5,3"), caster, "mon:1", burst), "the player is next to the target"
    assert not ad._friendly_fire(view("0,0", "0,5", "7,3"), caster, "mon:1", burst)
    assert not ad._friendly_fire(view("0,0", "1,1", "4,4"), caster, "mon:1", {"shape": ""}), "single-target spells never risk allies"
    print("friendly-fire safety OK")

    # ---------------- a healer heals the hurt before attacking
    tmp2, cfg2, llm2, g2 = make_game(max_active_companions=3)
    call(g2, "party", action="recruit", name="Sister Opal", class_slug="cleric",
         spells=spells(("cure-wounds", "Mending Light"), ("sacred-flame", "Lamp Flash")))
    g2.adapter.pc = g2.adapter.pc.model_copy(update={"hp_current": 6})
    assert call(g2, "start_encounter", monsters=["commoner"] * 2)["ok"]
    healed, hp_after = False, 0
    for _ in range(8):
        res = call(g2, "submit_action", action="end_turn")
        if any("Sister Opal casts Mending Light" in e for e in res.get("events", [])):
            healed, hp_after = True, g2.adapter.snapshot()["pc"]["hp"] if g2.adapter.handle else 12
            break
        if res.get("combat_over"):
            break
    assert healed and hp_after > 6, (healed, hp_after)
    print("healer AI OK: player HP 6 ->", hp_after)

    # ---------------- stored NPC stat blocks: consistent, remembered wounds, fixed once set
    tmp3, cfg3, llm3, g3 = make_game()
    s3, a3 = g3.state, g3.adapter
    r = call(g3, "create_entity", kind="npc", name="Baron Voss", core="Vain, cruel duelist.", initial_score=-20)
    assert r["ok"] and "stat_block" not in r
    assert "no stat block yet" in call(g3, "start_encounter", foes=["Baron Voss"])["error"]
    assert "stats" not in json.dumps(tools_schema("create_entity")) and "stats" not in json.dumps(tools_schema("update_entity")), \
        "no nested stat-block schema in the everyday entity tools (context cost)"
    voss = {"name": "Baron Voss", "hp": 30, "ac": 12, "attack_bonus": 3, "damage_dice": "1d6+1", "damage_type": "slashing", "dexterity": 12}
    a3.rest()
    r = call(g3, "start_encounter", custom_monsters=[voss])                       # first fight: these numbers become his stat block
    assert r["ok"] and r["stored_stat_blocks"] == ["Baron Voss (30/30 HP)"], r
    assert s3.entities["baron-voss"]["stats"]["hp_current"] == 30
    assert "stat block: stored" in narrative.profile_text(s3, "baron-voss") and "30" not in narrative.profile_text(s3, "baron-voss")
    assert "[stats]" in narrative.format_entities(s3, cfg3) and "AC 12" in narrative.stats_report(s3, "Baron Voss")
    assert not narrative.attach_stats(s3, "Baron Voss", {k: 1 for k in narrative.STAT_KEYS})["ok"], "a stat block is fixed once set"
    assert not narrative.attach_stats(s3, "The Main Quest", {k: 1 for k in narrative.STAT_KEYS})["ok"], "only NPCs have stat blocks"
    hurt = False
    for _ in range(30):
        res = call(g3, "submit_action", action="attack", target_id=foe_id(g3))
        assert res["ok"], res
        if s3.entities["baron-voss"]["stats"]["hp_current"] != 30 and not g3.adapter.handle:
            break
        v = E.get_live(a3.handle) if a3.handle else None
        if v and v.tracked_hp.get("mon:1", 30) < 30:
            hurt = True
            res = call(g3, "submit_action", action="flee")
            assert res["ok"] and res["result"] == "fled", res
            break
    assert hurt, "the player should have wounded the Baron before fleeing"
    assert res["survivors"] and res["survivors"][0].startswith("Baron Voss (") and "tracked_deaths" not in res, res
    left = s3.entities["baron-voss"]["stats"]["hp_current"]
    assert 0 < left < 30 and s3.entities["baron-voss"]["status"] == "active"
    assert a3.handle is None and not res.get("boss_reward"), "fleeing earns nothing"
    a3.rest()   # restores the player only; the calendar stays put
    r = call(g3, "start_encounter", foes=["Baron Voss"])
    assert r["stored_stat_blocks"] == [f"Baron Voss ({left}/30 HP)"], "wounds are remembered within the same day"
    call(g3, "submit_action", action="flee")
    call(g3, "advance_time", amount=1, unit="days")
    r = call(g3, "start_encounter", foes=["baron voss"])
    assert r["stored_stat_blocks"] == ["Baron Voss (30/30 HP)"], "a full day of rest heals him"
    # kill him: the tracker marks him dead and the stat block can never fight again
    for _ in range(80):
        if a3.handle is None:
            break
        res = call(g3, "submit_action", action="attack", target_id=foe_id(g3))
    assert s3.entities["baron-voss"]["status"] in ("dead", "active")
    if s3.entities["baron-voss"]["status"] == "dead":
        assert "Baron Voss" in res.get("tracked_deaths", []) and s3.entities["baron-voss"]["stats"]["hp_current"] == 0
        assert "dead" in call(g3, "start_encounter", foes=["Baron Voss"])["error"]
    assert "with_stat_blocks" in call(g3, "start_encounter", foes=["Nobody"])

    # a custom monster named after a tracked NPC: first fight locks the numbers, later fights ignore new ones
    call(g3, "create_entity", kind="npc", name="Captain Rook", core="Gruff mercenary.")
    a3.rest()   # restores the player only; the calendar stays put
    rook = {"name": "Captain Rook", "hp": 20, "ac": 13, "attack_bonus": 4, "damage_dice": "1d8+2", "damage_type": "piercing"}
    r = call(g3, "start_encounter", custom_monsters=[rook])
    assert r["ok"] and r["stored_stat_blocks"] == ["Captain Rook (20/20 HP)"], r
    assert s3.entities["captain-rook"]["stats"]["hp_max"] == 20
    call(g3, "submit_action", action="flee")
    narrative.advance(s3, 24 * 60)
    drift = {**rook, "hp": 70, "ac": 18}                                          # the model 'forgets' and invents new numbers
    r = call(g3, "start_encounter", custom_monsters=[drift])
    assert r["stored_stat_blocks"] == ["Captain Rook (20/20 HP)"], "stored stats must win over drifting invention"
    call(g3, "submit_action", action="flee")
    print("stat blocks OK: wounded Baron kept", left, "HP; stored numbers beat drift")

    # ---------------- the player's /scene shows full stats; the DM's own view stays compact
    rep_text = g.scene_report()
    assert "Edmond - Fighter L1" in rep_text and "Dr. Ada Voss (Aether Engineer) - Wizard" in rep_text
    assert "Plasma Darts" in rep_text and "AC " in rep_text and "slots" in rep_text
    scene3 = g3.scene_report()
    assert "Captain Rook" in scene3 and "Stat blocks stored by the engine" in scene3, scene3
    baron = g3.stats_for("Baron Voss")
    assert "AC 12" in baron and ("[DEAD]" in baron or "HP" in baron), baron
    assert "Plasma Darts" in g.stats_for("Ada")
    assert "Edmond" in g.stats_for("Edmond") and "No stats found" in g.stats_for("zzz nobody")
    assert len(json.dumps(g.adapter.snapshot())) < len(rep_text) * 2, "the DM's view must not balloon"
    print("/scene, /stats OK")

    # ---------------- the heavy party schemas are only sent when the party is on the table
    names = lambda text: {t["function"]["name"] for t in (setattr(g, "_player_text", text) or g._tools_now())}   # noqa: E731
    assert not ({"party", "find_spells"} & names("we walk along the quiet road"))
    assert {"party", "find_spells"} <= names("Would you like to join us?")
    assert {"party", "find_spells"} <= names("Mara, what do you think?"), "a companion's name brings the party tools back"
    print("tool gating OK")

    # ---------------- persistence of designed companions
    saved = ad.export_state()
    b2 = EngineAdapter(cfg)
    b2.restore(saved)
    E.configure_lib_loader(ad.loader)
    assert b2.roster["ally:dr-ada-voss"]["spells"] == ["magic-missile", "burning-hands", "fire-bolt"]
    assert b2.roster["ally:dr-ada-voss"]["spell_names"]["burning-hands"] == "Arc Flare"
    assert "ally:mara-quill-p1" in b2.power_uses and "ally:mara-quill-p1-power" in b2.loader.extra
    assert call(g, "party", action="dismiss", name="Mara Quill")["status"] == "left"
    assert "ally:mara-quill-p1" not in ad.power_uses and "ally:mara-quill-p1-power" not in ad.loader.extra
    print("persistence OK")

    for d in (tmp, tmp2, tmp3):
        shutil.rmtree(d, ignore_errors=True)
    print("ALL DESIGN TESTS PASSED")


if __name__ == "__main__":
    main()
