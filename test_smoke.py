"""Offline smoke test: a scripted fake model drives the real engine, tools, and summary loop.

Run:  python test_smoke.py
"""
import json
import shutil
import tempfile
from types import SimpleNamespace as NS

from config import Config
from game import Game
from engine_adapter import EngineAdapter
from memory import LoreStore, SessionState, SessionStore, StorySummary
from tools import ToolContext, dispatch


def tool_call(name, args, i=[0]):
    i[0] += 1
    return NS(content="", tool_calls=[NS(id=f"call{i[0]}", function=NS(name=name, arguments=json.dumps(args)))])


class FakeLLM:
    """Acts like a (very dumb) DM: picks one tool from the player's words, then narrates the result."""

    def __init__(self):
        self.summary_calls = 0
        self.calls = 0

    def chat(self, messages, tools=None, effort=None, schema=None):
        self.calls += 1
        last = messages[-1]
        if last["role"] == "user" and last["content"].startswith("[ENGINE NOTICE]"):
            if "buy_item" in last["content"]:                    # the items/money nudge
                return tool_call("remove_item", {"name": "gold coin"})
            return tool_call("start_encounter", {"monsters": ["commoner"]})
        if last["role"] == "tool":
            return NS(content="The DM narrates: " + last["content"][:60], tool_calls=None)
        text = last["content"].lower()
        if "toss" in text:
            return NS(content="He catches the coin and grins.", tool_calls=None)   # narrated, but no tool call
        if "greatsword" in text and "swing" in text:
            return tool_call("submit_action", {"action": "attack", "target_id": "mon:1", "weapon_id": "greatsword"})
        if "swing mystery" in text:
            return tool_call("submit_action", {"action": "attack", "target_id": "mon:1", "weapon_id": "banana"})
        if "summon imp" in text:
            return tool_call("start_encounter", {
                "monsters": ["goblin-minion"],
                "custom_monsters": [{"name": "Ember Imp", "hp": 18, "ac": 13, "attack_bonus": 4,
                                     "damage_dice": "1d6+2", "damage_type": "fire", "dexterity": 15}]})
        if "punch" in text:
            return NS(content="You swing and miss.", tool_calls=None)   # invented mechanics, no tool call
        if "fight" in text:
            return tool_call("start_encounter", {"monsters": ["goblin-warrior", "goblin-warrior"]})
        if "too strong" in text:
            return tool_call("start_encounter", {"monsters": ["adult-red-dragon"]})
        if "engage" in text:
            scene = next(json.loads(m["content"].split("\n", 1)[1]) for m in reversed(messages)
                         if m["role"] == "system" and m["content"].startswith("CURRENT SCENE"))
            foes = [e for e in scene.get("enemies", []) if e["status"] != "dead"]
            if not foes:
                return NS(content="Nothing left to fight.", tool_calls=None)
            foe = min(foes, key=lambda e: e["distance_ft"])
            if foe["distance_ft"] > 5:
                return tool_call("submit_action", {"action": "move_toward", "target_id": foe["id"]})
            return tool_call("submit_action", {"action": "attack", "target_id": foe["id"]})
        if "approach" in text:
            return tool_call("submit_action", {"action": "move_toward", "target_id": "mon:1"})
        if "attack" in text:
            return tool_call("submit_action", {"action": "attack", "target_id": "mon:1"})
        if "next" in text:
            return tool_call("submit_action", {"action": "attack", "target_id": "mon:2"})
        if "hack" in text:
            return tool_call("exec", {"code": "import os; os.system('rm -rf /')"})
        if "sneak" in text:
            return tool_call("skill_check", {"skill": "stealth", "dc": 12})
        if "baddc" in text:
            return tool_call("skill_check", {"skill": "stealth", "dc": 999})
        if "rest" in text:
            return tool_call("rest", {})
        if "lore" in text:
            return tool_call("lookup_lore", {"query": "Mira blacksmith"})
        if "fact" in text:
            return tool_call("record_fact", {"fact": "Mira the blacksmith distrusts the player."})
        return NS(content="Nothing happens.", tool_calls=None)

    def structured(self, messages, model_cls, effort=None, retries=2):
        self.summary_calls += 1
        return StorySummary(recap=f"Recap #{self.summary_calls}: the hero fought goblins.",
                            open_threads=["Find the goblin camp"],
                            npc_changes=[{"npc": "Mira", "attitude": "wary", "reason": "saw the fight"}],
                            new_facts=[f"Fact {self.summary_calls}: goblins raid the road."])


def main():
    tmp = tempfile.mkdtemp()
    cfg = Config(save_dir=tmp, save_name="t", lore_dir=tmp + "/lore", seed=42,
                 summary_every_turns=4, summary_keep_recent=2)
    llm = FakeLLM()
    adapter = EngineAdapter(cfg)
    adapter.create_pc("Edmond")
    store = SessionStore(cfg)
    game = Game(cfg, llm, adapter, LoreStore(cfg.lore_dir), store, SessionState())

    def play(text):
        n = game.dm_turn(text)
        info = game.after_turn(text, n)
        print(f"> {text:12} -> {n[:70]!r} {info}")
        return n

    # --- safety / validation paths
    r = dispatch("exec", {"code": "x"}, game.ctx);                          assert not r["ok"], r
    r = dispatch("skill_check", {"skill": "stealth", "dc": 999}, game.ctx);  assert not r["ok"], r
    r = dispatch("submit_action", {"action": "attack", "target_id": "mon:1"}, game.ctx); assert not r["ok"], r  # not in combat
    r = dispatch("submit_action", '{"action": "fireball"}', game.ctx);       assert not r["ok"], r
    r = dispatch("start_encounter", {"monsters": ["adult-red-dragon"]}, game.ctx); assert not r["ok"], r
    print("validation OK:", r["error"][:80])

    # --- a full fight
    play("fight!")
    assert adapter.handle is not None
    for _ in range(40):
        if adapter.handle is None:
            break
        play("engage the nearest foe")

    assert adapter.handle is None, "combat should have ended"
    assert game.state.summary, "forced summary should have fired"
    assert game.state.summarized_upto > 0
    print("summary:", game.state.summary, "| upto:", game.state.summarized_upto, "| canon:", game.state.canon)

    # --- non-combat tools
    play("sneak past")
    play("lookup lore")
    play("record a fact")
    play("rest now")
    assert adapter.pc.hp_current == adapter.pc.hp_max

    # --- enforcement: invented mechanics must be rejected and redone through the engine
    play("look around")
    assert game._turn_tools == [] and adapter.handle is None          # harmless chatter is not nudged
    n = play("I punch the shopkeeper")
    assert [t[0] for t in game._turn_tools][0] == "start_encounter", game._turn_tools
    assert adapter.handle is not None, "nudge should have started a real fight"
    assert "swing and miss" not in n
    for _ in range(40):
        if adapter.handle is None:
            break
        play("engage the nearest foe")
    assert adapter.handle is None
    print("enforcement OK:", game.tools_line())

    # --- improvised creatures: validated stats go straight into the engine's own damage math
    big = {"name": "Doom", "hp": 80, "ac": 18, "attack_bonus": 7, "damage_dice": "4d12+6", "damage_type": "force"}
    for bad, why in [
        ({"custom_monsters": [big]}, "over budget"),
        ({"custom_monsters": [{**big, "hp": 500}]}, "hp out of range"),
        ({"custom_monsters": [{**big, "hp": 10, "damage_dice": "999d99"}]}, "bad dice"),
        ({"custom_monsters": [{**big, "hp": 10, "damage_dice": "1d6", "damage_type": "love"}]}, "bad type"),
        ({"custom_monsters": [], "monsters": []}, "empty"),
    ]:
        r = dispatch("start_encounter", bad, game.ctx)
        assert not r["ok"] and adapter.handle is None, (why, r)
    r = adapter.start_encounter([], [{**big, "hp": 80, "damage_dice": "4d12+6"}])   # adapter re-checks on its own
    assert not r["ok"] and "too strong" in r["error"], r
    schema = json.dumps(__import__("tools").openai_tool_schemas())
    assert "$ref" not in schema and "$defs" not in schema
    play("summon imp")
    assert [t[0] for t in game._turn_tools] == ["start_encounter"] and adapter.handle is not None
    names = {e["name"] for e in adapter.snapshot()["enemies"]}
    assert "Ember Imp" in names, names
    for _ in range(40):
        if adapter.handle is None:
            break
        play("engage the nearest foe")
    assert adapter.handle is None
    print("custom monsters OK")

    # --- regression: "I punch the man" from across the room (the failure seen in a real session)
    adapter.rest()
    names = lambda: {t["function"]["name"] for t in game._tools_now()}      # noqa: E731
    assert "submit_action" not in names() and "start_encounter" in names()   # no attacks outside combat
    r = None
    for _ in range(12):                      # keep spawning until the commoner starts far away
        adapter.rest()
        r = adapter.start_encounter(["commoner"])
        assert r["ok"], r
        if r["scene"]["enemies"][0]["distance_ft"] > 5:
            break
        for _ in range(40):                  # adjacent already: finish it and try again
            if adapter.handle is None:
                break
            adapter.submit_action("attack", "", "fists")
    assert adapter.handle is not None and r["scene"]["enemies"][0]["distance_ft"] > 5, "never got a distant enemy"
    assert "submit_action" in names() and "start_encounter" not in names() and "rest" not in names()
    assert "unarmed-strike" in r["scene"]["pc"]["weapons"]
    bad = adapter.submit_action("attack", "mon:1", "banana")
    assert not bad["ok"] and "unarmed-strike" in bad["valid_weapons"], bad          # nonsense weapon rejected
    r = adapter.submit_action("attack", target_id="", weapon_id="fists")            # blank target, 'fists' alias
    assert r["ok"], r
    text = " | ".join(r["events"])
    assert "moves from" in text and "attacks Commoner" in text and "could not be made" not in text, text
    for _ in range(30):                                                             # finish it by target NAME
        if adapter.handle is None:
            break
        r = adapter.submit_action("attack", target_id="the commoner", weapon_id="punch")
        assert r["ok"], r
    assert adapter.handle is None
    play("hack the planet")                                                         # unknown tool -> error is visible
    assert game._turn_tools[0][1] is False and "Unknown tool" in game.tools_line(), game.tools_line()
    print("regression OK:", game.tools_line())

    # --- automated replies: asking for a weapon you don't have costs ONE model call and no story log
    adapter.rest()
    adapter.start_encounter(["goblin-warrior"])
    logs_before, calls_before = len(game.state.log), llm.calls
    n = play("I swing my greatsword at it")
    assert game._auto_reply and n.startswith("You don't have a 'greatsword'") and "longsword" in n, n
    assert llm.calls == calls_before + 1, "should not spend a second model call narrating the rejection"
    assert len(game.state.log) == logs_before, "automated rejections must not enter the story log"
    n = play("swing mystery")                       # model invented 'banana'; the player never said it
    assert not game._auto_reply and game._turn_tools[0][1] is False
    for _ in range(40):
        if adapter.handle is None:
            break
        play("engage the nearest foe")
    assert adapter.handle is None
    print("automated replies OK")

    # --- unique weapons: chassis + riders + power, budget by tier, rare gated behind a boss kill
    adapter.rest()
    def foe_id():
        return next((e["id"] for e in adapter.snapshot().get("enemies", []) if e["status"] != "dead"), "")

    forge = lambda **kw: dispatch("forge_weapon", kw, game.ctx)                                 # noqa: E731
    assert dispatch("grant_item", {"name": "Rusty Key", "description": "Opens the cellar."}, game.ctx)["ok"]
    assert not forge(name="X", chassis="bazooka")["ok"]                                          # not a real chassis
    r = forge(name="Greedy", chassis="longsword", tier="uncommon", magical_bonus=1,
              riders=[{"dice": "2d8", "damage_type": "fire"}])
    assert not r["ok"] and "Too powerful" in r["error"], r                                       # 4 + 9 > 8 points
    r = forge(name="Boss Blade", chassis="longsword", tier="rare", magical_bonus=1)
    assert not r["ok"] and "boss" in r["error"], r                                               # no boss reward yet
    r = forge(name="Ember Blade", lore="Quenched in a dragon's tear.", chassis="longsword", tier="uncommon",
              magical_bonus=1, riders=[{"dice": "1d6", "damage_type": "fire"}])
    assert r["ok"] and r["weapon_id"] == "ember-blade" and r["points"] == "8/8", r
    r = forge(name="Gale Edge", lore="Sings in crosswinds.", chassis="rapier", tier="uncommon",
              power=[{"name": "Thunderclap", "ability": "con", "damage_dice": "2d6", "damage_type": "thunder", "charges": 2}])
    assert r["ok"] and r["has_power"], r
    snap = adapter.snapshot()["pc"]
    assert {"ember-blade", "gale-edge"} <= set(snap["weapons"]) and "Rusty Key" in snap["inventory"], snap
    assert snap["powers"][0]["charges"] == "2/2", snap

    adapter.start_encounter(["goblin-warrior", "goblin-warrior", "goblin-warrior"])
    assert not forge(name="Late", chassis="mace")["ok"]                                          # not mid-fight
    fire = []
    for _ in range(6):                                                                           # rider damage from the engine
        if adapter.handle is None:
            break
        r = adapter.submit_action("attack", foe_id(), "ember blade")
        assert r["ok"], r
        fire += [e for e in r["events"] if "fire damage" in e]
        if fire:
            break
    assert fire, "rider fire damage should come from the engine"
    uses = 0
    for _ in range(40):                                                                          # fire the power until spent
        if adapter.handle is None:
            break
        v = adapter.snapshot()
        foe = next((e for e in v["enemies"] if e["status"] != "dead"), None)
        if foe is None:
            break
        if foe["distance_ft"] > 30:
            adapter.submit_action("move_toward", foe["id"])
            continue
        r = adapter.submit_action("use_power", foe["id"], "gale edge")
        if not r["ok"]:
            assert r.get("auto_always") and "spent" in r["auto_reply"], r
            break
        uses += 1
        assert any("makes a CON save" in e for e in r["events"]), r["events"]
        assert r["power_charges_left"] == f"{2 - uses}/2", r
        if adapter.handle is not None:
            adapter.submit_action("end_turn")
    assert uses == 2, uses
    for _ in range(60):
        if adapter.handle is None:
            break
        adapter.submit_action("attack", foe_id(), "ember blade")
    assert adapter.handle is None
    adapter.rest()
    assert adapter.snapshot()["pc"]["powers"][0]["charges"] == "2/2"                            # rest recharges

    # boss-tier kill earns a reward that unlocks ONE rare weapon
    assert adapter.boss_rewards == 0
    for _ in range(12):
        adapter.rest()
        r = adapter.start_encounter(["spy"])                                                     # CR 1 = boss-tier here
        assert r["ok"], r
        for _ in range(80):
            if adapter.handle is None:
                break
            r = adapter.submit_action("attack", foe_id(), "ember blade")
        if adapter.boss_rewards:
            break
    assert adapter.boss_rewards == 1, "winning a boss-tier fight should grant a reward"
    r = forge(name="Spymaster's Regret", lore="Taken from a dead spy.", chassis="rapier", tier="rare", magical_bonus=2,
              riders=[{"dice": "1d6", "damage_type": "poison"}])
    assert r["ok"] and r["points"] == "12/14", r
    assert adapter.boss_rewards == 0 and not forge(name="Again", chassis="mace", tier="rare")["ok"]

    a3 = EngineAdapter(cfg)
    a3.restore(adapter.export_state())
    assert {"ember-blade", "gale-edge", "spymaster-s-regret"} <= set(a3._weapons())
    assert a3.power_uses["gale-edge"]["max"] == 2 and any(i["name"] == "Rusty Key" for i in a3.inventory)
    print("forge OK:", fire[0])

    # --- money and items are engine state: narrating a payment without a tool is rejected and redone
    adapter.rest()
    assert adapter.gold == 15 and adapter.snapshot()["pc"]["gold"] == 15
    assert dispatch("grant_item", {"name": "Gold Coin", "description": "Stamped with a stag."}, game.ctx)["ok"]
    assert "Gold Coin" in adapter.snapshot()["pc"]["inventory"]
    n = play("I toss him my gold coin")
    assert [t[0] for t in game._turn_tools] == ["remove_item"], game._turn_tools            # nudged into a real removal
    assert "Gold Coin" not in adapter.snapshot()["pc"].get("inventory", []), "the coin must actually leave the inventory"
    r = adapter.remove_item("gold coin")
    assert not r["ok"] and r["auto_reply"].startswith("You don't have a 'gold coin'"), r   # can't hand it over twice

    # buying: engine-owned weapon prices, atomic spend, affordability, plain non-magical gear
    r = adapter.buy_item("a sword", weapon="warhammer")
    assert r["ok"] and r["paid_gp"] == 15 and adapter.gold == 0, r
    assert "warhammer" in adapter._weapons() and not adapter.loader.base.get_weapon("warhammer").magical
    r = adapter.buy_item("an axe", weapon="battleaxe")
    assert not r["ok"] and r["auto_always"] and "can't afford" in r["auto_reply"] and adapter.gold == 0, r
    assert not adapter.buy_item("again", weapon="warhammer")["ok"]                          # already owned
    assert not adapter.change_gold(26)["ok"]                                                # per-call gain cap at level 1
    assert adapter.change_gold(20, "looted a purse")["gold"] == 20
    r = adapter.change_gold(-50, "bribe")
    assert not r["ok"] and r["auto_always"] and adapter.gold == 20, r                       # never negative
    r = adapter.buy_item("Healing Draught", price_gp=5, description="Smells of mint.")
    assert r["ok"] and r["gold_left"] == 15 and "Healing Draught" in adapter.snapshot()["pc"]["inventory"], r
    assert not adapter.remove_item("Ember Blade")["ok"]                                     # unique weapons can't be discarded
    assert adapter.remove_item("healing")["ok"]                                             # partial name match
    names_now = {t["function"]["name"] for t in game._tools_now()}
    assert {"buy_item", "change_gold", "remove_item"} <= names_now
    adapter.start_encounter(["goblin-warrior"])
    names_fight = {t["function"]["name"] for t in game._tools_now()}
    assert not ({"buy_item", "change_gold", "forge_weapon"} & names_fight)                  # no shopping mid-fight
    for _ in range(60):
        if adapter.handle is None:
            break
        adapter.submit_action("attack", foe_id(), "warhammer")
    assert adapter.handle is None
    a4 = EngineAdapter(cfg)
    a4.restore(adapter.export_state())
    assert a4.gold == adapter.gold and "warhammer" in a4._weapons()
    print("money OK: gold", adapter.gold)

    # --- armor is real equipment: engine prices, swaps body armor, recomputes AC, and actually cuts damage taken
    import dnd5e_engine as E
    snap = adapter.snapshot()["pc"]
    assert set(snap["armor"]) == {"chain-mail", "shield"} and snap["ac"] == 18, snap
    r = dispatch("buy_item", {"item": "Free Armor", "price_gp": 0}, game.ctx)
    assert not r["ok"], "price 0 must be rejected by the schema"                              # the bug seen in a real session
    r = adapter.buy_item("gleaming chain mail", price_gp=1)
    assert not r["ok"] and "armor='chain-mail'" in r["error"], r                              # standard gear can't dodge pricing
    r = adapter.grant_item("Plate Armor")
    assert not r["ok"] and "gear='plate-armor'" in r["error"], r
    gold0 = adapter.gold = 2000
    r = adapter.buy_item("leather", armor="leather-armor")
    assert not r["ok"] and "wouldn't improve" in r["auto_reply"] and adapter.gold == gold0, r  # a downgrade is refused
    r = adapter.buy_item("plate", armor="plate-armor")
    assert r["ok"] and r["paid_gp"] == 1500 and adapter.gold == 500 and r["ac"] == 20 and r["replaced"] == "chain-mail", r
    snap = adapter.snapshot()["pc"]
    assert "plate-armor" in snap["armor"] and "chain-mail" not in snap["armor"] and snap["ac"] == 20, snap
    r = adapter.grant_item("Guard's Plate", gear="plate-armor")
    assert not r["ok"] and "worth too much" in r["error"], r                                  # expensive gear is not free loot
    assert adapter.grant_item("Guard's Mace", gear="mace")["ok"] and "mace" in adapter._weapons()
    r = adapter.buy_item("plate", armor="plate-armor")
    assert not r["ok"] and r["auto_always"], r                                                # already owned

    def hits_taken(equipment):
        local = EngineAdapter(Config(save_dir=tmp, seed=7))
        local.create_pc("Edmond", equipment=equipment)
        local.pc = local.pc.model_copy(update={"hp_current": 900, "hp_max": 900})
        local.start_encounter(["goblin-warrior"] * 4)
        n = 0
        for _ in range(20):
            res = local.submit_action("end_turn")
            n += sum(1 for e in res.get("events", []) if " attacks Edmond" in e and "HIT" in e)
        return n, local.pc.ac

    try:
        (low, ac_low), (high, ac_high) = hits_taken(("longsword",)), hits_taken(("longsword", "plate-armor", "shield"))
    finally:
        E.configure_lib_loader(adapter.loader)                                               # restore this game's loader
    assert ac_low == 11 and ac_high == 20 and low > high + 8, (low, ac_low, high, ac_high)
    a5 = EngineAdapter(cfg)
    a5.restore(adapter.export_state())
    E.configure_lib_loader(adapter.loader)
    assert a5.pc.ac == 20 and "plate-armor" in a5.pc.equipment and "chain-mail" not in a5.pc.equipment
    print(f"armor OK: goblins hit {low}x at AC {ac_low} vs {high}x at AC {ac_high}")

    # --- unique armor: chassis AC + magical bonus + resistances (+ power), same tiers and boss reward as weapons
    forge_a = lambda **kw: dispatch("forge_armor", kw, game.ctx)                            # noqa: E731
    assert adapter.pc.ac == 20 and adapter.boss_rewards == 0
    before = (len(adapter.inventory), len(adapter.custom_armor), dict(adapter.power_uses), list(adapter.pc.equipment))
    r = forge_a(name="Weak Mail", chassis="chain-mail", tier="uncommon", magical_bonus=1, resistances=["fire"])
    assert not r["ok"] and "lower AC" in r["error"], r                                     # 19 < 20: refused
    assert before == (len(adapter.inventory), len(adapter.custom_armor), dict(adapter.power_uses), list(adapter.pc.equipment)), \
        "a refused forge must leave no trace"
    assert not forge_a(name="Greedy Plate", chassis="plate-armor", tier="uncommon", magical_bonus=1,
                       resistances=["slashing"])["ok"]                                       # 4 + 8 > 8
    assert "boss" in forge_a(name="Boss Plate", chassis="plate-armor", tier="rare")["error"]
    assert not forge_a(name="Nope", chassis="bazooka")["ok"]
    r = forge_a(name="Emberward Plate", lore="Tempered in a volcano's mouth.", chassis="plate-armor", tier="uncommon",
                magical_bonus=1, resistances=["fire"])
    assert r["ok"] and r["points"] == "8/8" and r["ac"] == 21 and r["replaced"] == "plate-armor", r
    snap = adapter.snapshot()["pc"]
    assert snap["ac"] == 21 and "emberward-plate" in snap["armor"] and "plate-armor" not in snap["armor"], snap
    assert snap["resistances"] == ["fire"], snap
    assert not adapter.remove_item("Emberward Plate")["ok"]                                  # equipment, not a trinket

    adapter.pc = adapter.pc.model_copy(update={"hp_current": 900, "hp_max": 900})            # survive the damage sample
    imp = {"name": "Cinder Imp", "hp": 40, "ac": 10, "attack_bonus": 7, "damage_dice": "2d6+3", "damage_type": "fire"}
    assert adapter.start_encounter([], [imp])["ok"]
    dmg = []
    for _ in range(80):
        res = adapter.submit_action("end_turn")
        dmg += [int(e.split("takes ")[1].split()[0]) for e in res.get("events", [])
                if e.startswith("Edmond takes") and "fire damage" in e]
    assert len(dmg) >= 6 and sum(dmg) / len(dmg) < 7.5, dmg                                  # 2d6+3 averages 10; resisted ~5
    for _ in range(120):
        if adapter.handle is None:
            break
        adapter.submit_action("attack", foe_id(), "ember blade")
    assert adapter.handle is None and adapter.boss_rewards == 1, "a threat-400 foe counts as a boss"
    print(f"resistance OK: avg fire hit {sum(dmg) / len(dmg):.1f} over {len(dmg)} hits (unresisted ~10)")

    adapter.rest()
    r = forge_a(name="Frostbound Aegis", lore="Pried from a wyrm's hoard.", chassis="shield", tier="rare",
                magical_bonus=1, resistances=["cold"],
                power=[{"name": "Rime Burst", "ability": "con", "damage_dice": "2d6", "damage_type": "cold", "charges": 1}])
    assert r["ok"] and r["points"] == "14/14" and r["has_power"] and adapter.boss_rewards == 0, r
    assert not forge_a(name="Again", chassis="shield", tier="rare")["ok"]                    # the reward is spent
    assert any(pw["name"] == "Rime Burst" and pw["charges"] == "1/1" for pw in adapter.snapshot()["pc"]["powers"])
    assert set(adapter.pc.damage_resistances) == {"fire", "cold"} or "cold" in adapter.pc.damage_resistances
    adapter.start_encounter(["goblin-warrior", "goblin-warrior"])
    fired = 0
    for _ in range(40):
        if adapter.handle is None:
            break
        foe = next((e for e in adapter.snapshot()["enemies"] if e["status"] != "dead"), None)
        if foe is None:
            break
        if foe["distance_ft"] > 30:
            adapter.submit_action("move_toward", foe["id"])
            continue
        r = adapter.submit_action("use_power", foe["id"], "frostbound-aegis")
        if not r["ok"]:
            assert r.get("auto_always") and "spent" in r["auto_reply"], r
            break
        fired += 1
        assert r["power_charges_left"] == "0/1", r
        if adapter.handle is not None:
            adapter.submit_action("end_turn")
    assert fired == 1, fired
    for _ in range(80):
        if adapter.handle is None:
            break
        adapter.submit_action("attack", foe_id(), "ember blade")
    a6 = EngineAdapter(cfg)
    a6.restore(adapter.export_state())
    E.configure_lib_loader(adapter.loader)
    assert a6.pc.ac == adapter.pc.ac and {"fire", "cold"} <= set(a6.pc.damage_resistances), (a6.pc.ac, a6.pc.damage_resistances)
    assert len(a6.custom_armor) == 2 and "frostbound-aegis" in a6.power_uses
    print("armor forge OK: AC", adapter.pc.ac, "resistances", adapter.pc.damage_resistances)

    # --- persistence round trip
    game.state.engine = adapter.export_state()
    store.save(game.state)
    reloaded = SessionStore(cfg).load()
    assert reloaded.turn == game.state.turn and reloaded.summary == game.state.summary
    a2 = EngineAdapter(cfg)
    a2.restore(reloaded.engine)
    assert a2.pc.name == "Edmond" and a2.pc.hp_current == adapter.pc.hp_current
    assert sum(1 for _ in open(store.log_path)) == game.state.turn
    print("persistence OK; turns:", game.state.turn, "| summaries:", llm.summary_calls)
    shutil.rmtree(tmp)
    print("ALL SMOKE TESTS PASSED")


if __name__ == "__main__":
    main()
