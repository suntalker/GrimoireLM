"""The model's entire reach into the game. Six tools, flat arguments, strict validation.

Safety properties:
  * the model cannot choose WHO acts (the player's character is fixed in the adapter)
  * every argument is validated against a pydantic schema; bad calls come back as error data
  * unknown tool names are rejected; nothing here evaluates or executes model text
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Type

from pydantic import BaseModel, Field, ValidationError, model_validator

from soft import SoftModel

import narrative
from config import Config
from engine_adapter import ARMOR_SLUGS, COMPANION_PRESETS, FORGE_CHASSIS, EngineAdapter, _clean_custom
from memory import LoreStore, SessionState

SKILLS = Literal[
    "acrobatics", "animal_handling", "arcana", "athletics", "deception", "history", "insight",
    "intimidation", "investigation", "medicine", "nature", "perception", "performance",
    "persuasion", "religion", "sleight_of_hand", "stealth", "survival",
]


class NoArgs(SoftModel):
    pass


DAMAGE_TYPES = Literal["acid", "bludgeoning", "cold", "fire", "force", "lightning", "necrotic",
                       "piercing", "poison", "psychic", "radiant", "slashing", "thunder"]


class CustomMonster(SoftModel):
    """A creature you invent. Stats are FIXED once the fight starts; the engine rolls all damage."""
    name: str = Field(min_length=1, max_length=40)
    hp: int = Field(ge=1, le=80)
    ac: int = Field(ge=8, le=18)
    attack_bonus: int = Field(ge=-2, le=7, description="to-hit bonus; +3 to +5 is typical for level 1")
    damage_dice: str = Field(pattern=r"^[1-4]d(4|6|8|10|12)([+-][0-9]{1,2})?$", description="damage per hit, e.g. '1d8+2'")
    damage_type: DAMAGE_TYPES
    dexterity: int = Field(10, ge=6, le=20, description="affects initiative")


class StartEncounterArgs(SoftModel):
    monsters: list[str] = Field(default_factory=list, max_length=8,
                                description="SRD monster template slugs, e.g. ['goblin-warrior','commoner']")
    custom_monsters: list[CustomMonster] = Field(default_factory=list, max_length=6,
                                                 description="Creatures you invent to fit the story, with full stats")
    foes: list[str] = Field(default_factory=list, max_length=4,
                            description="names of tracked NPCs that have a stored stat block (uses the stored numbers and wounds)")

    @model_validator(mode="after")
    def _at_least_one(self):
        if not self.monsters and not self.custom_monsters and not self.foes:
            raise ValueError("Provide at least one monster template, custom monster, or tracked foe.")
        return self


class GrantItemArgs(SoftModel):
    name: str = Field(min_length=1, max_length=40)
    description: str = Field("", max_length=240, description="one line of flavor")
    gear: Literal[("",) + FORGE_CHASSIS + ARMOR_SLUGS] = Field(
        "", description="set ONLY for standard gear found as loot (cheap weapons/armor): it is really equipped")


class AdvanceTimeArgs(SoftModel):
    amount: int = Field(ge=1, le=48)
    unit: Literal["hours", "days"] = "hours"

    @model_validator(mode="after")
    def _limit_days(self):
        if self.unit == "days" and self.amount > 14:
            raise ValueError("At most 14 days can pass at once.")
        return self


class CreateEntityArgs(SoftModel):
    kind: Literal["npc", "subplot", "faction"]
    name: str = Field(min_length=1, max_length=60)
    core: str = Field(min_length=1, max_length=240,
                      description="FIXED forever: personality traits (npc), ideology (faction), or premise (subplot)")
    objectives: list[str] = Field(default_factory=list, max_length=3)
    current_state: str = Field("", max_length=160)
    future_plans: str = Field("", max_length=200)
    reputation: str = Field("", max_length=120, description="how they are known (npc/faction)")
    initial_score: int = Field(0, ge=-30, le=30, description="starting attitude toward the player (npc/faction)")


class InteractArgs(SoftModel):
    entity: str = Field(min_length=1, max_length=60, description="id or name of the NPC, faction, or subplot")
    event: str = Field(min_length=1, max_length=200, description="what happened between them and the player, one sentence")
    relationship_delta: int = Field(0, ge=-15, le=15, description="how the entity feels about it; the engine caps swings per day")


class UpdateEntityArgs(SoftModel):
    entity: str = Field(min_length=1, max_length=60)
    current_state: str = Field("", max_length=160)
    future_plans: str = Field("", max_length=200)
    objectives: list[str] = Field(default_factory=list, max_length=3)
    status: Literal["", "active", "dormant", "dead", "disbanded", "resolved"] = Field(
        "", description="npc ends as 'dead', faction as 'disbanded', subplot as 'resolved'. Permanent.")
    reason: str = Field("", max_length=160, description="REQUIRED with dead/disbanded/resolved: how it ended; also for any revival")
    relation_with: str = Field("", max_length=60, description="another entity this one's attitude changes toward")
    relation_delta: int = Field(0, ge=-10, le=10)


class GoldArgs(SoftModel):
    amount: int = Field(ge=-100, le=200, description="gp gained (positive: loot, reward) or paid (negative: bribe, tip, fee)")
    reason: str = Field("", max_length=100)


class RemoveItemArgs(SoftModel):
    name: str = Field(min_length=1, max_length=40, description="an item in scene.pc.inventory")


class BuyItemArgs(SoftModel):
    item: str = Field(min_length=1, max_length=40, description="what is being bought")
    weapon: Literal[("",) + FORGE_CHASSIS] = Field("", description="a standard weapon (engine sets the price)")
    armor: Literal[("",) + ARMOR_SLUGS] = Field("", description="standard armor or shield (engine sets the price, equips it)")
    price_gp: int = Field(1, ge=1, le=100, description="price for other goods (ignored when weapon/armor is set)")
    description: str = Field("", max_length=240)


class RiderArg(SoftModel):
    dice: str = Field(pattern=r"^[12]d(4|6|8)$", description="extra damage on every hit, e.g. '1d6'")
    damage_type: DAMAGE_TYPES


class PowerArg(SoftModel):
    name: str = Field(min_length=1, max_length=30, description="what the power is called, e.g. 'Thunderclap'")
    ability: Literal["str", "dex", "con", "int", "wis", "cha"] = Field(description="the target's saving throw")
    damage_dice: str = Field(pattern=r"^[1-4]d(4|6|8)$")
    damage_type: DAMAGE_TYPES
    on_save: Literal["half", "none"] = "half"
    charges: int = Field(ge=1, le=3, description="uses before the next long rest")


class SpellPick(SoftModel):
    spell: str = Field(max_length=40, description="a spell slug from find_spells")
    name: str = Field("", max_length=30, description="what this spell is CALLED in this setting (Plasma Darts, Séance Bolt...)")


class PartyArgs(SoftModel):
    action: Literal["recruit", "activate", "deactivate", "dismiss"]
    name: str = Field(min_length=1, max_length=40)
    class_slug: Literal[""] | Literal[tuple(COMPANION_PRESETS)] = Field(
        "", description="recruit only: the mechanical chassis (fighter, rogue, barbarian, monk, wizard, cleric, druid, bard, sorcerer)")
    species_slug: Literal["human", "elf", "dwarf", "halfling"] = "human"
    title: str = Field("", max_length=40, description="setting flavor, e.g. 'Aether Engineer', 'Spiritualist Medium'")
    spells: list[SpellPick] = Field(default_factory=list, max_length=8,
                                    description="casters only: chosen from find_spells; rename each to fit the setting")
    powers: list[PowerArg] = Field(default_factory=list, max_length=2,
                                   description="invented abilities (gadgets, rituals, tech) with a save, damage and limited charges")


class FindSpellsArgs(SoftModel):
    kind: Literal["", "damage", "heal", "attack"] = ""
    max_level: int = Field(1, ge=0, le=3, description="highest spell level the companion can cast")
    damage_type: Literal["", "acid", "cold", "fire", "force", "lightning", "necrotic", "poison", "psychic", "radiant", "thunder"] = ""
    query: str = Field("", max_length=30)


class ForgeArmorArgs(SoftModel):
    name: str = Field(min_length=1, max_length=40, description="evocative proper name")
    lore: str = Field("", max_length=240, description="1-2 sentences of history that fit the story")
    chassis: Literal[ARMOR_SLUGS] = Field(description="the real armor (or 'shield') it is built on; sets base AC")
    tier: Literal["uncommon", "rare", "legendary"] = Field("uncommon", description="rare/legendary only after a boss reward")
    magical_bonus: int = Field(0, ge=0, le=3, description="added to armor class")
    resistances: list[DAMAGE_TYPES] = Field(default_factory=list, max_length=2,
                                            description="damage types the wearer takes half damage from")
    power: list[PowerArg] = Field(default_factory=list, max_length=1, description="optional signature power (0 or 1)")


class ForgeWeaponArgs(SoftModel):
    name: str = Field(min_length=1, max_length=40, description="evocative proper name")
    lore: str = Field("", max_length=240, description="1-2 sentences of history that fit the story")
    chassis: Literal[FORGE_CHASSIS] = Field(description="the real weapon it is built on; sets base damage and properties")
    tier: Literal["uncommon", "rare", "legendary"] = Field("uncommon", description="rare/legendary only after a boss reward")
    magical_bonus: int = Field(0, ge=0, le=3)
    riders: list[RiderArg] = Field(default_factory=list, max_length=2)
    power: list[PowerArg] = Field(default_factory=list, max_length=1, description="optional signature power (0 or 1)")


class SubmitActionArgs(SoftModel):
    action: Literal["attack", "move_toward", "cast_spell", "use_power", "flee", "dodge", "dash", "disengage", "help", "hide", "end_turn"]
    target_id: str = Field("", description="Enemy id from the scene, e.g. 'mon:1' (for attack / move_toward / spells)")
    weapon_id: str = Field("", description="Weapon slug from pc.weapons (for use_power: the weapon whose power to fire)")
    spell_id: str = Field("", description="Spell slug from pc.spells_known (cast_spell only)")
    slot_level: int = Field(0, ge=0, le=9)


class SkillCheckArgs(SoftModel):
    skill: SKILLS
    dc: int = Field(ge=5, le=30, description="Difficulty: 10 easy, 15 medium, 20 hard, 25 very hard")
    advantage: Literal["normal", "advantage", "disadvantage"] = "normal"


class LoreArgs(SoftModel):
    query: str = Field(max_length=200, description="Names or topics to look up in the setting notes")


class FactArgs(SoftModel):
    fact: str = Field(max_length=300, description="One new fact about the world that is now canon")


@dataclass
class ToolContext:
    adapter: EngineAdapter
    lore: LoreStore
    state: SessionState
    flags: dict = field(default_factory=dict)   # e.g. combat_ended -> forces a summary this turn
    cfg: Config = field(default_factory=Config)


@dataclass
class Tool:
    name: str
    description: str
    args: Type[BaseModel]
    fn: Callable[[ToolContext, BaseModel], dict]


def _advance(ctx: ToolContext, minutes: int) -> dict:
    days = narrative.advance(ctx.state, minutes)
    ctx.flags["time_advanced"] = True
    return {"ok": True, "clock": narrative.clock_text(ctx.state), "days_crossed": days,
            "week": narrative.week_of_chunk(ctx.state.day, ctx.cfg)}


def _rest(ctx: ToolContext) -> dict:
    res = ctx.adapter.rest()
    if res.get("ok"):
        res.update(_advance(ctx, 8 * 60))
    return res


def _start_encounter(ctx: ToolContext, a) -> dict:
    tracked, bad = narrative.foe_blocks(ctx.state, list(a.foes))
    if bad:
        return bad
    custom = []
    for c in a.custom_monsters:
        d = c.model_dump()
        e = narrative.find(ctx.state, d["name"])
        if e and e["kind"] == "npc" and narrative._norm(e["name"]) == narrative._norm(d["name"]) and e["status"] not in narrative.TERMINAL:
            if not e.get("stats"):                  # first fight: these numbers become the NPC's fixed stat block
                clean, problem = _clean_custom(d)
                if problem:
                    return {"ok": False, "error": problem}
                narrative.attach_stats(ctx.state, e["id"], {"hp_max": clean["hp"], "ac": clean["ac"],
                                       "attack_bonus": clean["attack_bonus"], "damage_dice": clean["damage_dice"],
                                       "damage_type": clean["damage_type"], "dexterity": clean["dexterity"]})
            if any(t["name"] == e["name"] for t in tracked):
                continue
            blocks, bad = narrative.foe_blocks(ctx.state, [e["id"]])
            if bad:
                return bad
            tracked += blocks                        # stored numbers win over whatever the model wrote this time
            continue
        custom.append(d)
    res = ctx.adapter.start_encounter(list(a.monsters), custom, tracked)
    if res.get("ok") and tracked:
        res["stored_stat_blocks"] = [f"{t['name']} ({t['hp_start']}/{t['hp_max']} HP)" for t in tracked]
    return _after_engine(ctx, res)


def _party(ctx: ToolContext, a) -> dict:
    e = narrative.find(ctx.state, a.name)
    if e and e["status"] in narrative.TERMINAL and a.action in ("recruit", "activate"):
        return {"ok": False, "error": f"{e['name']} is {e['status']} and cannot join the party."}
    res = ctx.adapter.party(a.action, a.name, a.class_slug, a.species_slug, a.title,
                            [sp.model_dump() for sp in a.spells], [p.model_dump() for p in a.powers])
    if res.get("ok") and a.action == "recruit":
        if e is None:                          # companions are tracked people too: relationship, memories, watchdog
            made = narrative.create_entity(ctx.state, ctx.cfg, "npc", a.name,
                                           f"{a.title or a.class_slug.title()}: a companion, loyal to the player.",
                                           objectives=["Fight beside the player"], current_state="Travelling with the party.",
                                           reputation="Companion", score=20)
            res["tracked_as"] = made.get("id")
        else:
            res["tracked_as"] = e["id"]
    return res


def _after_engine(ctx: ToolContext, result: dict) -> dict:
    if result.get("combat_over"):
        ctx.flags["combat_ended"] = True
        survivors = narrative.record_foe_results(ctx.state, result.pop("foe_hp", {}))
        if survivors:                              # tracked NPCs that lived through it keep their wounds
            result["survivors"] = survivors
        died = narrative.mark_fallen(ctx.state, result.pop("fallen_names", []))
        died += narrative.mark_fallen(ctx.state, result.pop("fallen_allies", []), cause="Fell in battle beside the player.")
        if died:                                  # a tracked NPC fell in combat: the tracker updates itself
            result["tracked_deaths"] = died
            result["note"] = ", ".join(died) + " died and is permanently removed from the story's living cast."
    return result


TOOLS: dict[str, Tool] = {t.name: t for t in [
    Tool("get_scene",
         "Get the authoritative current state: your character, enemies (status words + distance), whose turn it is. "
         "Call this whenever you are unsure.",
         NoArgs, lambda ctx, a: ctx.adapter.snapshot()),
    Tool("start_encounter",
         "Begin a fight. Use SRD templates in 'monsters', invent creatures in 'custom_monsters' with explicit stats "
         "(hp, ac, attack_bonus, damage_dice, damage_type), and/or list tracked NPCs with stored stat blocks in 'foes'. "
         "A custom monster named after a tracked NPC uses (or first creates) that NPC's stored stats. The engine rejects "
         "over-budget creatures, then rolls initiative and plays monster turns using EXACTLY those stats.",
         StartEncounterArgs, lambda ctx, a: _start_encounter(ctx, a)),
    Tool("submit_action",
         "Perform the player's action in combat. The engine resolves rolls and damage and also plays the monsters' "
         "turns afterwards. Returns what happened. 'move_toward' walks the player toward an enemy; 'end_turn' passes.",
         SubmitActionArgs,
         lambda ctx, a: _after_engine(ctx, ctx.adapter.submit_action(
             a.action, a.target_id, a.weapon_id, a.spell_id, a.slot_level))),
    Tool("skill_check",
         "Roll a skill check for the player's character against a DC. Use for any uncertain non-combat action. "
         "Never invent the result yourself.",
         SkillCheckArgs, lambda ctx, a: ctx.adapter.skill_check(a.skill, a.dc, a.advantage)),
    Tool("lookup_lore",
         "Search the setting notes and established canon for names, places, or factions.",
         LoreArgs, lambda ctx, a: {"ok": True, "results": ctx.lore.search(a.query, canon=ctx.state.canon),
                                   "entities": [narrative.profile_text(ctx.state, i)
                                                for i in narrative.mentioned(ctx.state, a.query, limit=2)]}),
    Tool("record_fact",
         "Save a NEW established fact (a name, a promise, a discovery) so it stays consistent later.",
         FactArgs, lambda ctx, a: {"ok": True, "saved": ctx.state.add_canon(a.fact)}),
    Tool("grant_item",
         "Give the player something they find for free: a plain item (key, map, letter) with no stats, or cheap standard "
         "gear via gear=<weapon/armor> which is genuinely equipped. Use forge_weapon for unique magic weapons.",
         GrantItemArgs, lambda ctx, a: ctx.adapter.grant_item(a.name, a.description, a.gear)),
    Tool("change_gold",
         "Change the player's gold. The ONLY way gold moves: loot or rewards (positive) and payments, bribes, tips "
         "(negative). Rejected if the player cannot afford it. Never narrate gold changing hands without calling this.",
         GoldArgs, lambda ctx, a: ctx.adapter.change_gold(a.amount, a.reason)),
    Tool("remove_item",
         "Remove an item from the player's inventory when they hand it over, use it up, or lose it. "
         "Call this whenever the story takes an item away.",
         RemoveItemArgs, lambda ctx, a: ctx.adapter.remove_item(a.name)),
    Tool("buy_item",
         "The player buys something. Checks funds, deducts gold, and grants the item in one step. Standard weapons and "
         "armor MUST use weapon=<type> or armor=<type>: the engine sets the price, equips it, and recomputes armor class "
         "(new body armor replaces the old). Other goods need price_gp >= 1. Never use forge_weapon for shop purchases.",
         BuyItemArgs, lambda ctx, a: ctx.adapter.buy_item(a.item, a.price_gp, a.description, a.weapon, a.armor)),
    Tool("forge_armor",
         "Create UNIQUE armor (or a shield) the player now wears: a real chassis (base AC) + magical AC bonus + up to 2 "
         "damage resistances + optional signature power with charges. Engine-enforced power budget by tier; stats fixed "
         "forever; it replaces the worn armor and may not lower AC. 'rare' needs a boss reward (same reward as forge_weapon: "
         "pick one). Theme it to the story (a frost wyrm's scales give cold resistance).",
         ForgeArmorArgs,
         lambda ctx, a: ctx.adapter.forge_armor(
             a.name, a.lore, a.chassis, a.tier, a.magical_bonus, list(a.resistances),
             a.power[0].model_dump() if a.power else None)),
    Tool("forge_weapon",
         "Create a UNIQUE weapon the player now owns: a real chassis (base damage) + up to 2 rider damage entries + "
         "optional signature power with charges. The engine enforces a power budget by tier and fixes the stats "
         "forever. 'rare' needs a boss reward (the engine tells you when a boss fell). Theme the affixes to the story.",
         ForgeWeaponArgs,
         lambda ctx, a: ctx.adapter.forge_weapon(
             a.name, a.lore, a.chassis, a.tier, a.magical_bonus,
             [r.model_dump() for r in a.riders], a.power[0].model_dump() if a.power else None)),
    Tool("advance_time",
         "Let in-game time pass: travel, waiting, downtime, a long task. The engine keeps the calendar; use this for "
         "anything longer than a normal scene. (A normal turn already adds a few minutes automatically.)",
         AdvanceTimeArgs, lambda ctx, a: _advance(ctx, a.amount * (60 if a.unit == "hours" else 1440))),
    Tool("create_entity",
         "Register a recurring NPC, faction, or subplot so it stays consistent and is never forgotten. 'core' is its FIXED "
         "personality/ideology/premise. Do this the first time a named person or group becomes important.",
         CreateEntityArgs,
         lambda ctx, a: narrative.create_entity(ctx.state, ctx.cfg, a.kind, a.name, a.core, a.objectives, a.current_state,
                                                a.future_plans, a.reputation, a.initial_score)),
    Tool("interact",
         "Record that the player dealt with a tracked NPC, faction, or subplot. Stores a memory for them, resets their "
         "inactivity clock, and applies a relationship change the engine may cap (attitudes shift slowly; a hostile NPC "
         "does not forgive in one scene). Call it whenever the player meaningfully engages them.",
         InteractArgs, lambda ctx, a: narrative.interact(ctx.state, ctx.cfg, a.entity, a.event, a.relationship_delta)),
    Tool("update_entity",
         "Update the MUTABLE parts of a tracked entity: current_state, future_plans, objectives, subplot status, or its "
         "attitude toward another entity (faction relations). The fixed personality/ideology/premise cannot change. "
         "ALSO how a tracked character leaves the story: status='dead' (with reason) whenever an NPC dies, however it "
         "happens; 'disbanded' for factions; 'resolved' for finished plots. The dead stay dead.",
         UpdateEntityArgs,
         lambda ctx, a: narrative.update_entity(ctx.state, a.entity, a.current_state, a.future_plans, a.objectives,
                                                a.status, a.relation_with, a.relation_delta, a.reason)),
    Tool("find_spells",
         "Browse the engine's VERIFIED spell catalog when designing a caster companion. Pick spells, then rename them to "
         "fit the setting in party(recruit, spells=[{spell, name}]).",
         FindSpellsArgs, lambda ctx, a: ctx.adapter.find_spells(a.kind, a.max_level, a.damage_type, a.query)),
    Tool("party",
         "Manage companions who fight beside the player. recruit = design a new companion for THIS setting: pick a class "
         "(the mechanical chassis), a flavor title, and for casters spells from find_spells (rename them), and/or invent up to "
         "2 powers (gadgets, rituals, tech). activate/deactivate = tag them in or out of the ACTIVE party; dismiss = they "
         "leave. ONLY ACTIVE companions are placed in combat and nobody can join mid-fight, so set the party BEFORE "
         "start_encounter. The engine plays their turns.",
         PartyArgs, lambda ctx, a: _party(ctx, a)),
    Tool("rest",
         "Take a long rest (only when not in combat): restores HP, spell slots and power charges, and passes 8 hours.",
         NoArgs, lambda ctx, a: _rest(ctx)),
]}


def _clean(schema: Any) -> Any:
    """Strip pydantic's noisy 'title' keys; local chat templates handle plain schemas best."""
    if isinstance(schema, dict):
        return {k: _clean(v) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [_clean(v) for v in schema]
    return schema


def _inline_refs(schema: dict) -> dict:
    """Inline $ref/$defs: several local servers choke on references in tool schemas."""
    defs = schema.pop("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


def openai_tool_schemas() -> list[dict]:
    return [{
        "type": "function",
        "function": {"name": t.name, "description": t.description,
                     "parameters": _clean(_inline_refs(t.args.model_json_schema()))},
    } for t in TOOLS.values()]


def dispatch(name: str, raw_args: Any, ctx: ToolContext) -> dict:
    tool = TOOLS.get(name)
    if tool is None:
        return {"ok": False, "error": f"Unknown tool '{name}'.", "available": sorted(TOOLS)}
    try:
        data = raw_args if isinstance(raw_args, dict) else json.loads(raw_args or "{}")
        args = tool.args.model_validate(data)
    except (json.JSONDecodeError, ValidationError, TypeError) as ex:
        return {"ok": False, "error": f"Bad arguments for {name}: {str(ex)[:250]}"}
    try:
        return tool.fn(ctx, args)
    except Exception as ex:  # a tool bug must never crash the game loop
        return {"ok": False, "error": f"Tool failure: {type(ex).__name__}: {str(ex)[:200]}"}
