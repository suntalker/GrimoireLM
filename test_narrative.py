"""Offline tests for the hierarchical narrative layer. Run:  python test_narrative.py"""
import json
import re
import shutil
import tempfile
from types import SimpleNamespace as NS

import narrative
from config import Config
from engine_adapter import EngineAdapter
from game import Game
from memory import LoreStore, SessionState, SessionStore, StorySummary
from tools import dispatch


class FakeLLM:
    """Plays the narrator AND the planner. `fail` makes every planner call fail; `omit` hides reintroductions."""

    def __init__(self):
        self.fail, self.omit, self.planner_calls, self.n = False, False, 0, 0

    def chat(self, messages, tools=None, effort=None, schema=None):
        last = messages[-1]
        return NS(content="The scene continues quietly.", tool_calls=None) if last["role"] != "tool" else NS(content="Done.", tool_calls=None)

    def structured(self, messages, model_cls, effort=None, retries=2):
        name = model_cls.__name__
        if name == "StorySummary":
            return StorySummary(recap="Things happened this month.", open_threads=[], new_facts=[])
        if name in ("ChunkTransition", "FirstPlan"):
            self.planner_calls += 1
            if self.fail:
                return None
            self.n += 1
            body = next(m["content"] for m in messages if "WATCHDOG (must be reintroduced):" in m["content"])
            ids = re.findall(r"^- (\S+) \(", body.split("WATCHDOG (must be reintroduced):")[1], re.M)
            reasked = any("omitted required reintroductions" in m["content"] for m in messages)
            reintro = [] if (self.omit and not reasked) or self.omit else [
                narrative.Reintroduction(entity_id=i, week=1 + k % 4, how="Return naturally.") for k, i in enumerate(ids)]
            plan = narrative.MonthlyPlan(
                focus=f"Month {self.n} focus",
                weeks=[narrative.WeekBeat(week=w, beat=f"Beat {w} of plan {self.n}", entities=[]) for w in (1, 2, 3, 4)],
                reintroductions=reintro)
            if name == "FirstPlan":
                return narrative.FirstPlan(plan=plan)
            return narrative.ChunkTransition(closing=narrative.ChunkClose(record=f"Record of month {self.n}.", key_facts=[f"Fact {self.n}"]), plan=plan)
        if name == "Compressed":
            return narrative.Compressed(record="Merged early history.")
        if name == "Manifest":
            return None if self.fail else narrative.Manifest(theme="Ashen kingdom", goal="Relight the beacons",
                                                             antagonist_name="Vorlag", antagonist_description="Patient and cruel.")
        return None


def make_game(**cfg_kw):
    tmp = tempfile.mkdtemp()
    cfg = Config(save_dir=tmp, save_name="n", lore_dir=tmp + "/lore", seed=3, **cfg_kw)
    llm = FakeLLM()
    adapter = EngineAdapter(cfg)
    adapter.create_pc("Edmond")
    state = SessionState()
    game = Game(cfg, llm, adapter, LoreStore(cfg.lore_dir), SessionStore(cfg), state)
    narrative.seed_campaign(state, llm, cfg, narrative.create_manifest(llm, cfg))
    return tmp, cfg, llm, game


def call(game, tool, **args):
    return dispatch(tool, args, game.ctx)


def play(game, text="we continue"):
    narration = game.dm_turn(text)
    return game.after_turn(text, narration)


def memory_block(game, text="hi"):
    """The engine-built context block (system message #2), without the static DM rules."""
    return [m for m in game.build_messages(text) if m["role"] == "system"][1]["content"]


def jump_to_next_chunk(game):
    for _ in range(2):
        assert call(game, "advance_time", amount=14, unit="days")["ok"]


def main():
    tmp, cfg, llm, game = make_game()
    s = game.state

    # ---------------- manifest, seeding, first plan
    assert s.manifest["antagonist_name"] == "Vorlag" and s.day == 1 and s.minute == 8 * 60
    assert {e["name"] for e in s.entities.values()} == {"Vorlag", "The Main Quest"}
    assert s.entities["vorlag"]["relationship"]["score"] == -50
    assert [w["week"] for w in s.plan["weeks"]] == [1, 2, 3, 4] and s.plan["focus"] == "Month 1 focus"
    print("manifest/seed OK:", narrative.status_line(s, cfg))

    # ---------------- clock
    assert "Morning 08:00" in narrative.clock_text(s)
    info = play(game)
    assert s.minute == 8 * 60 + 15, "a normal turn adds minutes_per_turn"
    r = call(game, "advance_time", amount=3, unit="days")
    assert r["ok"] and s.day == 4 and "Day 4" in r["clock"]
    play(game)
    assert s.minute == 8 * 60 + 15, "advance_time suppresses the default tick that turn"
    play(game)
    assert s.minute == 8 * 60 + 30, "the next ordinary turn ticks again"
    assert not call(game, "advance_time", amount=15, unit="days")["ok"]                 # per-call cap
    s.minute = 22 * 60
    assert narrative.phase_name(s.minute) == "Night" and narrative.phase_name(3 * 60) == "Night"
    assert narrative.phase_name(6 * 60) == "Morning" and narrative.phase_name(13 * 60) == "Afternoon"
    assert narrative.phase_name(18 * 60) == "Evening"
    day0 = s.day
    call(game, "rest")
    assert s.day == day0 + 1 and s.minute == 6 * 60, (s.day, s.minute)                 # rest = 8 hours, past midnight
    s.day, s.minute = 1, 8 * 60
    print("clock OK")

    # ---------------- entities and relationship tiers
    assert narrative.tier(-25) == "Hostile" and narrative.tier(-24) == "Unfriendly" and narrative.tier(0) == "Neutral"
    assert narrative.tier(75) == "Devoted" and narrative.tier(-75) == "Sworn Enemy"
    r = call(game, "create_entity", kind="npc", name="Mira the Blacksmith", core="Gruff, fair, distrusts thieves.",
             objectives=["Keep the forge alive"], current_state="Hammering at her anvil.", initial_score=0)
    assert r["ok"] and r["id"] == "mira-the-blacksmith"
    assert not call(game, "create_entity", kind="npc", name="mira the blacksmith", core="x")["ok"]       # duplicate
    assert narrative.find(s, "Mira") is narrative.find(s, "mira-the-blacksmith")                          # alias lookup
    call(game, "create_entity", kind="npc", name="Garrick Stone", core="Cheerful drunk.", initial_score=-30)
    call(game, "create_entity", kind="faction", name="The Crimson Court", core="Believes in rule by bloodline.",
         objectives=["Reclaim the throne"], initial_score=-10)
    call(game, "create_entity", kind="faction", name="The Free Companies", core="Mercenaries loyal to coin.")

    r = call(game, "interact", entity="Mira", event="Bought a lantern politely.", relationship_delta=10)
    assert r["applied_delta"] == 10 and r["tier"] == "Friendly" and not r["capped"], r
    r = call(game, "interact", entity="Mira", event="Chatted again.", relationship_delta=10)
    assert r["applied_delta"] == 0 and r["capped"] and "note" in r, r                                      # daily gain cap
    narrative.advance(s, 24 * 60)
    r = call(game, "interact", entity="Mira", event="Next day.", relationship_delta=10)
    assert r["applied_delta"] == 10 and r["score"] == 20, r                                                # cap resets daily

    r = call(game, "interact", entity="garrick-stone", event="Apologised for the insult.", relationship_delta=15)
    assert r["applied_delta"] == 3 and r["score"] == -27 and r["tier"] == "Hostile", r                      # slow forgiveness
    r = call(game, "interact", entity="Garrick", event="Pressed the point.", relationship_delta=15)
    assert r["applied_delta"] == 0 and r["capped"], r
    narrative.advance(s, 24 * 60)
    r = call(game, "interact", entity="Garrick", event="Kept trying.", relationship_delta=15)
    assert r["score"] == -24 and r["tier"] == "Unfriendly" and r["tier_changed_from"] == "Hostile", r

    s.entities["mira-the-blacksmith"]["relationship"]["score"] = 80                                        # loyalty resists whiplash
    r = call(game, "interact", entity="Mira", event="Insulted her work.", relationship_delta=-15)
    assert r["applied_delta"] == -5 and r["tier"] == "Devoted", r
    r = call(game, "interact", entity="Mira", event="Insulted her again.", relationship_delta=-15)
    assert r["applied_delta"] == 0 and r["capped"], r
    assert narrative.interact(s, cfg, "nobody", "x")["ok"] is False
    print("entities/relationships OK")

    # ---------------- fixed anchors, mutable state, faction relations
    mira = s.entities["mira-the-blacksmith"]
    core_before = mira["core"]
    r = call(game, "update_entity", entity="Mira", current_state="Mourning her husband.", future_plans="Close the forge.")
    assert r["ok"] and mira["current_state"] == "Mourning her husband." and mira["core"] == core_before
    assert not call(game, "update_entity", entity="Mira")["ok"]                                            # nothing to change
    assert "core" not in json.dumps(__import__("tools").TOOLS["update_entity"].args.model_json_schema())  # not even offered
    r = call(game, "update_entity", entity="Crimson", relation_with="Free Companies", relation_delta=10)
    assert r["ok"] and "+10" in r["relation"], r
    r = call(game, "update_entity", entity="Crimson", relation_with="Free Companies", relation_delta=10)
    assert r["relation_applied"] == 0, r                                                                    # 10 points/day max
    assert "Free Companies +10" in narrative.profile_text(s, "crimson-court")
    print("anchors/factions OK")

    # ---------------- localized memories: only relevant profiles are injected
    call(game, "interact", entity="Garrick", event="Garrick spilled ale on the player.", relationship_delta=0)
    s.focus = []
    msgs = game.build_messages("I walk over to Mira and nod")
    memory = "\n".join(m["content"] for m in msgs if m["role"] == "system")
    assert "NPC Mira the Blacksmith" in memory and "Mourning her husband" in memory
    assert "spilled ale" not in memory and "Garrick Stone [" not in memory, "unrelated NPCs must not leak into the prompt"
    assert "Crimson Court [" not in memory
    assert memory.count("[mira-the-blacksmith]") == 1
    call(game, "interact", entity="Garrick", event="Garrick offered a drink.", relationship_delta=0)
    assert s.focus[0] == "garrick-stone"
    msgs = game.build_messages("hello")                                                                     # focus carries the conversation
    assert "Garrick Stone [" in "\n".join(m["content"] for m in msgs if m["role"] == "system")
    # only THIS week's beat, not the whole plan
    blob = "\n".join(m["content"] for m in msgs if m["role"] == "system")
    assert "Beat 1 of plan 1" in blob and "Beat 2 of plan 1" not in blob and "Beat 4 of plan 1" not in blob
    assert "CAMPAIGN (permanent)" in blob and "Vorlag" in blob
    print("localized memory OK")

    # ---------------- watchdog
    s.day, s.minute = 30, 8 * 60
    s.chunk = 0
    for e in s.entities.values():
        e["last_interaction_day"] = 29
    s.entities["the-crimson-court"]["last_interaction_day"] = 4                                             # 26 days quiet
    alerts = narrative.watchdog(s, cfg)
    assert [a["id"] for a in alerts] == ["the-crimson-court"] and alerts[0]["days"] == 26, alerts
    assert "WATCHDOG" in narrative.format_entities(s, cfg)
    blob = memory_block(game, "hmm")
    assert "CRITICAL WATCHDOG ALERT" in blob and "Crimson Court" in blob                                    # unscheduled -> shown to the DM
    assert "CRITICAL WATCHDOG" not in memory_block(g_fresh := make_game()[3]), "no alert when nothing is ignored"
    narrative.touch_mentions(s, "Rumors of the Crimson Court reach the inn.")                              # safety net refreshes it
    assert narrative.watchdog(s, cfg) == []
    s.entities["the-crimson-court"]["last_interaction_day"] = 4
    r = call(game, "interact", entity="Crimson Court", event="Spoke to an envoy.")
    assert r["quiet_days_before"] == 26 and narrative.watchdog(s, cfg) == []
    print("watchdog OK")

    # ---------------- monthly transition: compress, wipe raw turns, replan, force reintroductions
    tmp2, cfg2, llm2, g2 = make_game()
    s2 = g2.state
    call(g2, "create_entity", kind="npc", name="Old Tom", core="Forgetful fisherman.")
    for i in range(5):
        play(g2, f"turn {i}")
    logged = len(s2.log)
    assert logged == 5 and s2.chunk == 0
    jump_to_next_chunk(g2)                      # day 29: Vorlag, The Main Quest and Old Tom all untouched for 28 days
    info = g2.after_turn("a quiet morning", "Nothing much.")
    assert info["transitions"] == 1 and info["transition"]["llm_ok"], info
    assert s2.log == [] and s2.summary == "" and s2.summarized_upto == 0 and s2.chunk == 1
    assert len(s2.history) == 1 and s2.history[0]["record"] == "Record of month 2." and s2.history[0]["days"] == "1-28"
    assert s2.plan["start_day"] == 29 and s2.plan["focus"] == "Month 2 focus"
    ids = {r["entity_id"] for r in s2.plan["reintroductions"]}
    assert {"vorlag", "the-main-quest", "old-tom"} <= ids, ids
    assert "Fact 2" in s2.canon
    raw = [json.loads(l) for l in open(g2.store.log_path)]
    assert len(raw) == 6, "the raw JSONL record on disk is never wiped"
    blob = memory_block(g2)
    assert "HISTORY (compressed)" in blob and "Record of month 2." in blob and "CRITICAL WATCHDOG" not in blob   # scheduled, not critical
    assert "SCHEDULED REINTRODUCTION" in blob or s2.plan["reintroductions"][0]["week"] != narrative.week_of_chunk(s2.day, cfg2)
    reloaded = SessionStore(cfg2).load()
    assert reloaded.chunk == 1 and reloaded.history and "old-tom" in reloaded.entities and reloaded.plan["start_day"] == 29
    print("transition OK:", s2.history[0]["record"], "| reintroduced:", sorted(ids))

    # planner that ignores the watchdog is repaired by the engine; a dead planner gets a default plan
    tmp3, cfg3, llm3, g3 = make_game()
    llm3.omit = True
    jump_to_next_chunk(g3)
    g3.after_turn("x", "y")
    assert {"vorlag", "the-main-quest"} <= {r["entity_id"] for r in g3.state.plan["reintroductions"]}
    assert all("ignored for" in r["how"] for r in g3.state.plan["reintroductions"])
    llm3.fail = True
    jump_to_next_chunk(g3)
    info = g3.after_turn("x", "y")
    assert info["transition"]["llm_ok"] is False and len(g3.state.plan["weeks"]) == 4 and g3.state.plan["reintroductions"]
    print("repair/fallback OK")

    # ---------------- history compression keeps the record tight
    tmp4, cfg4, llm4, g4 = make_game(max_history_chunks=3)
    for _ in range(6):
        jump_to_next_chunk(g4)
        g4.after_turn("x", "y")
    h = g4.state.history
    assert len(h) == 3 and h[0].get("merged") and h[0]["record"] == "Merged early history." and h[0]["days"].startswith("1-")
    blob = "\n".join(m["content"] for m in g4.build_messages("hi") if m["role"] == "system")
    assert blob.count("- days ") <= cfg4.history_in_prompt + 1
    print("history compression OK:", [x["days"] for x in h])

    # ---------------- the point of it all: prompt size stays flat across months of play
    tmp5, cfg5, llm5, g5 = make_game()
    for n in range(4):
        call(g5, "create_entity", kind="npc", name=f"Villager Number{n}", core="Plain folk.")

    def prompt_chars():
        return sum(len(m["content"]) for m in g5.build_messages("we keep exploring"))

    for _ in range(12):
        play(g5)
    early = prompt_chars()
    for i in range(160):
        play(g5, f"explore {i}")
        if i % 10 == 9:
            call(g5, "advance_time", amount=7, unit="days")
    late = prompt_chars()
    assert g5.state.chunk >= 3 and len(g5.state.history) >= 3, (g5.state.chunk, len(g5.state.history))
    assert late < early * 1.6 + 800, f"prompt grew from {early} to {late} chars"
    assert len(g5.state.pending()) <= cfg5.summary_every_turns + 2, len(g5.state.pending())   # only these go into the prompt verbatim
    print(f"context bounded OK: {early} chars early vs {late} chars after {g5.state.turn} turns / day {g5.state.day} "
          f"({g5.state.chunk + 1} months, {len(g5.state.history)} history records)")

    # ---------------- death is permanent: no watchdog, planner or scene can bring the dead back
    tmpd, cfgd, llmd, gd = make_game()
    sd = gd.state
    call(gd, "create_entity", kind="npc", name="Palpatine", core="Scheming senator.", objectives=["Rule everything"], initial_score=-20)
    call(gd, "create_entity", kind="faction", name="The Old Guard", core="Defend tradition.")
    call(gd, "interact", entity="Palpatine", event="Threatened the player.", relationship_delta=-5)
    sd.plan["reintroductions"].append({"entity_id": "palpatine", "week": 2, "how": "return"})
    sd.plan["weeks"][1]["entities"] = ["palpatine"]
    assert not call(gd, "update_entity", entity="Palpatine", status="dead")["ok"], "death needs a reason"
    assert not call(gd, "update_entity", entity="Palpatine", status="disbanded", reason="no way")["ok"]   # wrong ending for an NPC
    assert not call(gd, "update_entity", entity="The Old Guard", status="dead", reason="all fell")["ok"]  # wrong ending for a faction
    r = call(gd, "update_entity", entity="Palpatine", status="dead", reason="Fell down a reactor shaft.")
    assert r["ok"] and "permanently dead" in r["note"], r
    e = sd.entities["palpatine"]
    assert e["status"] == "dead" and e["ended_day"] == sd.day and e["future_plans"] == "" and e["objectives"] == [] and len(e["memories"]) <= 3
    assert all(x["entity_id"] != "palpatine" for x in sd.plan["reintroductions"]) and "palpatine" not in sd.plan["weeks"][1]["entities"]
    assert "palpatine" not in sd.focus
    r = call(gd, "interact", entity="Palpatine", event="Had a chat.")
    assert not r["ok"] and "dead" in r["error"] and "cannot take part" in r["error"], r
    r = call(gd, "create_entity", kind="npc", name="Palpatine", core="A clone, surely.")
    assert not r["ok"] and "stays that way" in r["error"], r                                            # no 'new' Palpatine either
    n_before = len(e["memories"])
    narrative.touch_mentions(sd, "People whisper that Palpatine is gone.")
    assert e["last_interaction_day"] == 1 and len(e["memories"]) == n_before                               # mentions don't touch the dead

    narrative.advance(sd, 60 * 24 * 40)                                                                    # 40 days of silence
    assert "palpatine" not in [a["id"] for a in narrative.watchdog(sd, cfgd)], "the watchdog must never flag the dead"
    assert "vorlag" in [a["id"] for a in narrative.watchdog(sd, cfgd)]                                     # the living are still flagged
    memb = memory_block(gd, "what about Palpatine")
    assert "THE DEAD AND DISBANDED" in memb and "Palpatine (dead)" in memb and "GONE FOR GOOD" in memb
    assert "CRITICAL WATCHDOG" in memb and "Palpatine (npc" not in memb.split("CRITICAL WATCHDOG")[1].split("\n")[0]
    bad = narrative.MonthlyPlan(focus="f", weeks=[narrative.WeekBeat(week=w, beat="b", entities=["palpatine"]) for w in (1, 2, 3, 4)],
                                reintroductions=[narrative.Reintroduction(entity_id="palpatine", week=1, how="Somehow returned.")])
    fixed = narrative._finalize_plan(bad, sd, [])
    assert fixed["reintroductions"] == [] and all("palpatine" not in w["entities"] for w in fixed["weeks"]), fixed   # planner can't resurrect him
    info = gd.after_turn("a quiet day", "Nothing.")
    assert info["transitions"] == 1
    assert "palpatine" not in {x["entity_id"] for x in sd.plan["reintroductions"]} and "vorlag" in {x["entity_id"] for x in sd.plan["reintroductions"]}
    assert "DEAD" in narrative._entity_digest(sd, cfgd)

    r = call(gd, "update_entity", entity="Palpatine", status="active")
    assert not r["ok"], "reviving needs a reason"
    assert not call(gd, "update_entity", entity="Palpatine", status="active", reason="somehow")["ok"]       # 'somehow' is not enough
    r = call(gd, "update_entity", entity="Palpatine", status="active",
             reason="Brought back by the Ashen Priests' ritual the player witnessed.")
    assert r["ok"] and e["status"] == "active" and e["memories"][-1]["text"].startswith("Returned:"), r
    assert call(gd, "interact", entity="Palpatine", event="He rises, changed.")["ok"]
    assert call(gd, "update_entity", entity="The Old Guard", status="disbanded", reason="Crushed by the Crown.")["ok"]
    assert call(gd, "update_entity", entity="The Main Quest", status="resolved", reason="The beacons were lit.")["ok"]
    assert {"the-old-guard", "the-main-quest"}.isdisjoint(a["id"] for a in narrative.watchdog(sd, cfgd))
    assert "DISBANDED" in narrative.format_entities(sd, cfgd) and "RESOLVED" in narrative.format_entities(sd, cfgd)

    for i in range(narrative.LIMITS["npc"]):                                                               # the dead free up cast slots
        if not call(gd, "create_entity", kind="npc", name=f"Extra Person{i}", core="Filler.")["ok"]:
            break
    assert not call(gd, "create_entity", kind="npc", name="One Too Many", core="x")["ok"]
    assert call(gd, "update_entity", entity="Extra Person0", status="dead", reason="Plague.")["ok"]
    assert call(gd, "create_entity", kind="npc", name="One Too Many", core="x")["ok"]
    print("permanent death OK")

    # combat bridge: killing a monster that is a tracked NPC updates the tracker by itself
    tmpb, cfgb, llmb, gb = make_game()
    call(gb, "rest")
    r = call(gb, "start_encounter", custom_monsters=[{"name": "Vorlag", "hp": 6, "ac": 8, "attack_bonus": -2,
                                                      "damage_dice": "1d4", "damage_type": "slashing"}])
    assert r["ok"], r
    final = {}
    for _ in range(60):
        res = call(gb, "submit_action", action="attack", target_id="mon:1")
        assert res["ok"], res
        if res.get("combat_over"):
            final = res
            break
    assert final.get("tracked_deaths") == ["Vorlag"] and "fallen_names" not in final, final
    vor = gb.state.entities["vorlag"]
    assert vor["status"] == "dead" and "combat" in vor["epitaph"] and "vorlag" not in gb.state.focus
    call(gb, "rest")
    call(gb, "start_encounter", custom_monsters=[{"name": "Vorlag Junior", "hp": 6, "ac": 8, "attack_bonus": -2,
                                                  "damage_dice": "1d4", "damage_type": "slashing"}])
    gb.state.entities["the-main-quest"]["status"] = "active"
    for _ in range(60):
        res = call(gb, "submit_action", action="attack", target_id="mon:1")
        if res.get("combat_over"):
            break
    assert "tracked_deaths" not in res and gb.state.entities["the-main-quest"]["status"] == "active"   # only exact matches count
    print("combat bridge OK")

    # ---------------- tool schemas are only paid for when relevant
    names = lambda text: {t["function"]["name"] for t in (setattr(g5, "_player_text", text) or g5._tools_now())}   # noqa: E731
    assert not ({"forge_weapon", "forge_armor"} & names("we talk about the weather"))
    assert {"forge_weapon", "forge_armor"} <= names("I search the dead bandit's chest")
    g5.adapter.boss_rewards = 1
    assert {"forge_weapon", "forge_armor"} <= names("hello")
    g5.adapter.boss_rewards = 0
    print("tool visibility OK")

    test_soft_limits()

    for d in (tmp, tmp2, tmp3, tmp4, tmp5, tmpd, tmpb):
        shutil.rmtree(d, ignore_errors=True)
    print("ALL NARRATIVE TESTS PASSED")



def test_soft_limits():
    """The truncation bug: a schema maxLength makes a constrained-decoding server cut text mid-sentence."""
    import tools
    from soft import trim_text
    long = ("A colossal, flaming-armed being who has breached the mortal realm through a cracked rift. He seeks to corrupt all life "
            "and claim the Sunstone's power to become an immortal tyrant of fire and shadow. A relentless conqueror who offers false bargains.")
    m = narrative.Manifest.model_validate_json(json.dumps(
        {"theme": "t", "goal": "g", "antagonist_name": "n", "antagonist_description": long}))
    assert len(m.antagonist_description) <= 200 and m.antagonist_description.endswith("."), m.antagonist_description
    assert not m.antagonist_description.endswith(" A"), "must never end on a dangling fragment"
    assert trim_text("short", 50) == "short"
    assert trim_text("one two three four five six seven eight nine ten", 25).endswith("\u2026")
    assert "maxLength" not in json.dumps(narrative.Manifest.model_json_schema())
    assert "maxLength" not in json.dumps(tools.openai_tool_schemas()), "tool schemas must not carry hard string limits"
    assert "complete sentence" in json.dumps(tools.openai_tool_schemas())
    tmp, cfg, llm, game = make_game()
    call(game, "create_entity", kind="npc", name="Rambler", core="Talkative.")
    r = call(game, "interact", entity="Rambler", event=("He told a very long story. " * 30), relationship_delta=1)
    assert r["ok"], r                                                                    # trimmed, not rejected
    mem = game.state.entities["rambler"]["memories"][-1]["text"]
    assert len(mem) <= 200 and mem.endswith("."), mem
    shutil.rmtree(tmp, ignore_errors=True)
    print("soft limits OK")


if __name__ == "__main__":
    main()
