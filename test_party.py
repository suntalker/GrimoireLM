"""Offline tests for group battles. Run:  python test_party.py"""
import shutil
from types import SimpleNamespace as NS

import dnd5e_engine as E

import narrative
from engine_adapter import PC_ID, EngineAdapter
from test_narrative import call, make_game


def foe_id(game):
    return next((e["id"] for e in game.adapter.snapshot().get("enemies", []) if e["status"] != "dead"), "")


def fight_to_end(game, max_steps=80, pc_passes=False):
    """The player acts through the tools; companions and monsters are played by the engine. Returns (events, final result)."""
    events, final = [], {}
    for _ in range(max_steps):
        if game.adapter.handle is None:
            break
        res = call(game, "submit_action", action="end_turn") if pc_passes else call(
            game, "submit_action", action="attack", target_id=foe_id(game))
        assert res["ok"], res
        events += res.get("events", [])
        if res.get("combat_over"):
            final = res
            break
    return events, final


def main():
    tmp, cfg, llm, g = make_game(max_active_companions=2)
    ad, st = g.adapter, g.state

    # ---------------- roster management and the active/inactive tag
    r = call(g, "party", action="recruit", name="Mira", class_slug="fighter")
    assert r["ok"] and r["status"] == "active" and r["tracked_as"] == "mira", r
    assert call(g, "party", action="recruit", name="Fenn", class_slug="rogue", species_slug="halfling")["status"] == "active"
    r = call(g, "party", action="recruit", name="Brug", class_slug="barbarian", species_slug="dwarf")
    assert r["ok"] and r["status"] == "inactive" and "full" in r["note"], r                  # cap of 2 active
    assert not call(g, "party", action="recruit", name="Pal", class_slug="paladin")["ok"]   # not a supported chassis
    assert not call(g, "party", action="recruit", name="Mira", class_slug="fighter")["ok"]  # already in the roster
    assert not call(g, "party", action="recruit", name="Odd", class_slug="monk", species_slug="orc")["ok"]
    assert not call(g, "party", action="activate", name="Nobody")["ok"]
    assert not call(g, "party", action="activate", name="Brug")["ok"], "active party is full"
    assert call(g, "party", action="deactivate", name="Fenn")["status"] == "inactive"
    assert call(g, "party", action="activate", name="Brug")["status"] == "active"
    assert {e["id"] for e in st.entities.values()} >= {"mira", "fenn", "brug"}, "companions are tracked people too"
    assert st.entities["mira"]["relationship"]["score"] == 20
    party = ad.snapshot()["pc"]["party"]
    assert {(m["name"], m["status"]) for m in party} == {("Mira", "active"), ("Fenn", "inactive"), ("Brug", "active")}, party
    print("roster OK:", [(m["name"], m["class"], m["status"], m["hp"]) for m in party])

    # ---------------- only ACTIVE companions are placed in combat, and the engine plays their turns
    r = call(g, "start_encounter", monsters=["goblin-warrior"] * 3)
    assert r["ok"], r
    live = E.get_live(ad.handle)
    assert live.party_ids == {PC_ID, "ally:mira", "ally:brug"}, live.party_ids               # Fenn stays behind
    scene = ad.snapshot()
    assert {a["name"] for a in scene["allies"]} == {"Mira", "Brug"} and scene["your_turn"], scene
    assert all(a["status"] in ("unhurt", "lightly wounded", "bloodied", "near death") for a in scene["allies"])
    assert not call(g, "party", action="activate", name="Fenn")["ok"], "no roster changes mid-fight"
    assert not {"party", "start_encounter"} & {t["function"]["name"] for t in g._tools_now()}
    events, final = fight_to_end(g)
    text = " | ".join(events)
    assert "Mira attacks" in text and "Brug attacks" in text, "companions must act on their own"
    assert "Mira moves from" in text or "Brug moves from" in text, "companions walk up to the enemy"
    assert final.get("combat_over") and final["result"] == "victory", final
    assert {p["name"] for p in final["party"]} == {"Mira", "Fenn", "Brug"}
    roster = ad.roster
    assert roster["ally:fenn"]["hp_current"] is None, "an inactive companion takes no part and no damage"
    print("group combat OK:", final["result"], [(p["name"], p["hp"]) for p in final["party"]])

    # damage persists between fights; rest restores everyone
    roster["ally:mira"]["hp_current"] = 3
    call(g, "rest")
    assert all(m["hp_current"] is None for m in ad.roster.values())

    # ---------------- a companion who falls updates the narrative tracker
    died = None
    for attempt in range(12):
        call(g, "rest")
        ad.roster["ally:brug"]["hp_current"] = 1                                          # fragile: goblins will find him
        assert call(g, "start_encounter", monsters=["goblin-warrior"] * 3)["ok"]
        events, final = fight_to_end(g, max_steps=60, pc_passes=True)
        if ad.roster["ally:brug"]["status"] == "dead":
            died = final
            break
    assert died is not None, "Brug (1 HP) should have fallen in one of 12 fights"
    assert "Brug" in died["tracked_deaths"], died
    brug = st.entities["brug"]
    assert brug["status"] == "dead" and "beside the player" in brug["epitaph"], brug
    assert ad.roster["ally:brug"]["status"] == "dead" and ad.roster["ally:brug"]["hp_current"] == 0
    assert not call(g, "party", action="activate", name="Brug")["ok"]
    assert not call(g, "party", action="recruit", name="Brug", class_slug="fighter")["ok"], "the dead cannot be re-recruited"
    call(g, "rest")
    assert ad.roster["ally:brug"]["status"] == "dead"
    print("fallen companion OK:", brug["epitaph"])

    # ---------------- dismiss, persistence, and the PC-death rule
    assert call(g, "party", action="dismiss", name="Fenn")["status"] == "left" and "ally:fenn" not in ad.roster
    saved = ad.export_state()
    a2 = EngineAdapter(cfg)
    a2.restore(saved)
    E.configure_lib_loader(ad.loader)
    assert a2.roster == ad.roster and a2.roster["ally:mira"]["status"] == "active"
    gone = NS(ended=False, encounter_ids={"mon:1"}, dead_ids={PC_ID}, party_ids={PC_ID, "ally:mira"})
    assert EngineAdapter._combat_over(gone), "if the player character falls, the fight is over even if allies stand"
    live_pair = NS(ended=False, encounter_ids={"mon:1"}, dead_ids={"ally:mira"}, party_ids={PC_ID, "ally:mira"})
    assert not EngineAdapter._combat_over(live_pair), "losing a companion alone does not end the fight"
    print("persistence/defeat rule OK")

    # ---------------- every companion class builds and fights (monk fights unarmed)
    tmp2, cfg2, llm2, g2 = make_game(max_active_companions=3)
    for name, cls in (("Aria", "fighter"), ("Kes", "rogue"), ("Tuk", "monk")):
        assert call(g2, "party", action="recruit", name=name, class_slug=cls)["ok"]
    assert call(g2, "start_encounter", monsters=["goblin-warrior"] * 4)["ok"]
    events, final = fight_to_end(g2)
    text = " | ".join(events)
    assert all(f"{n} attacks" in text for n in ("Aria", "Kes", "Tuk")), text[:400]
    print("all classes OK")

    for d in (tmp, tmp2):
        shutil.rmtree(d, ignore_errors=True)
    print("ALL PARTY TESTS PASSED")


if __name__ == "__main__":
    main()
