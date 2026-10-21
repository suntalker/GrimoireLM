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


# ============================================================================ overworld / terrain layer
# Everything below is APPENDED for the terrain-generation subsystem (see world.py). Existing tools above
# are untouched; world movement is gated by Python's collision loop, never by the model.
import world as _world  # noqa: E402  (appended section keeps the original import block intact)


class TravelArgs(SoftModel):
    direction: str = Field(min_length=1, max_length=12,
                           description="north, south, east, west, northeast, northwest, southeast, southwest")
    steps: int = Field(1, ge=1, le=100,
                       description="how many tiles the player attempts to walk; Python advances ONE tile at a time and halts instantly at any barrier")


class LookArgs(SoftModel):
    direction: str = Field("", max_length=12, description="leave empty to examine the tile you stand on")
    distance: int = Field(1, ge=1, le=12, description="tiles ahead to scan (raycast)")


class LandmarkArgs(SoftModel):
    name: str = Field(min_length=1, max_length=60, description="a landmark name from your notes (record_fact), e.g. 'Whispering Woods'")


class TerrainAtArgs(SoftModel):
    x: int = Field(description="tile coordinate x")
    y: int = Field(description="tile coordinate y")
    radius: int = Field(0, ge=0, le=6, description="survey ring around the point")


class LiftExclusionArgs(SoftModel):
    name: str = Field("", max_length=60, description="the exclusion zone's landmark name (e.g. 'The Obsidian Citadel'); leave empty to lift by coordinates")
    x: int = Field(-10**6, description="survey center X when lifting by coordinate (ignored if name is given)")
    y: int = Field(-10**6, description="survey center Y when lifting by coordinate (ignored if name is given)")
    radius: int = Field(3, ge=1, le=8, description="Manhattan radius of cloud tiles to dissolve around the center")


WORLD_RULES = """OVERWORLD AND TRAVEL
- The continent was generated ONCE at startup by Python from your layout notes. You CANNOT see the map and must never invent terrain: call world_travel, world_look, or world_terrain before narrating ANY movement, scenery, or obstacle.
- To move the player, call world_travel(direction, steps). Python walks one tile at a time and stops at mountains, rivers, lakes, and cloud exclusion zones; it snaps the player right before the barrier and reports stopped_by and steps_taken. Narrate ONLY that outcome (e.g. stopped_by 'cloud' means an unnatural magical barrier blocks the way - never let the player push through it in prose).
- Before describing what lies ahead use world_look; for a specific place use world_terrain(x, y). Landmarks live in YOUR notes: when the party discovers a place, record_fact its name; later pull exact coordinates with world_landmark (never guess coordinates).
- Cloud zones wall off high-level and endgame areas until the story earns access. Treat every blocked step as objective engine data, not a suggestion."""


def _world_of(ctx: ToolContext):
    """The WorldMap lives on the adapter once game.py boots the continent; None means no overworld yet."""
    return getattr(ctx.adapter, "world", None)


def _travel(ctx: ToolContext, a: TravelArgs) -> dict:
    w = _world_of(ctx)
    if w is None:
        return {"ok": False, "error": "No world map has been generated yet."}
    res = w.move_steps(a.direction, a.steps)
    if res.get("ok"):
        ahead = w.look(res["direction"], 3).get("tiles", [])[1:]   # what the player now faces (objective data)
        res["ahead"] = [{"terrain": t["terrain"], "impassable": t["impassable"],
                         "near_landmark": t["near_landmark"]} for t in ahead]
    return res


def _look(ctx: ToolContext, a: LookArgs) -> dict:
    w = _world_of(ctx)
    if w is None:
        return {"ok": False, "error": "No world map has been generated yet."}
    return w.look(a.direction, a.distance)


def _landmark_tool(ctx: ToolContext, a: LandmarkArgs) -> dict:
    w = _world_of(ctx)
    if w is None:
        return {"ok": False, "error": "No world map has been generated yet."}
    return w.find_landmark(a.name)


def _terrain_at(ctx: ToolContext, a: TerrainAtArgs) -> dict:
    w = _world_of(ctx)
    if w is None:
        return {"ok": False, "error": "No world map has been generated yet."}
    res = w.terrain_report(a.x, a.y, a.radius)
    res["ok"] = True
    res["nearest_landmark"] = w._nearest_landmark(a.x, a.y)
    return res


for _t in [
    Tool("world_travel",
         "Walk the overworld. Python advances ONE tile at a time and refuses to enter mountains, rivers, lakes, "
         "or cloud exclusion zones: on a block the player snaps right before the barrier and the result says "
         "stopped_by plus steps_taken. Trust those numbers; narrate only them.",
         TravelArgs, _travel),
    Tool("world_look",
         "Raycast the terrain ahead (or underfoot when direction is empty): tags, passability, and any nearby "
         "landmark per tile. Use it BEFORE describing scenery so you never invent terrain.",
         LookArgs, _look),
    Tool("world_landmark",
         "Look up a recorded landmark's exact coordinates, tag, distance and bearing from the player, plus the "
         "terrain mix around it. Pull names from your canon notes; never guess coordinates.",
         LandmarkArgs, _landmark_tool),
    Tool("world_terrain",
         "Survey the terrain mix around any coordinate (x, y) within a radius. The only sanctioned way to learn "
         "what a region holds without seeing the map.",
         TerrainAtArgs, _terrain_at),
]:
    TOOLS[_t.name] = _t


# ============================================================================ terrain-transition events (appended)
# The biome-boundary soft stop is a cinematic narrative trigger, not a movement failure: Python flags
# it on the ToolContext so game.py can inject a ONE-TURN directive (and strips it right after).
import narrative as _narr  # noqa: E402  (clock + directives already imported at top; alias keeps this block self-contained)

TRANSITION_NOTE = ("TERRAIN TRANSITION: this was NOT a completed march - the party crossed an ecological "
                   "boundary mid-step. Halt standard travel narration and describe the sweeping visual shift of scenery.")


def _travel_with_clock(ctx: ToolContext, a: TravelArgs) -> dict:
    """world_travel + the in-game clock: each tile costs its biome's marching time (plains 2h 🟩,
    forest 4h 🌲, mountain 1 day 🗻), and biome shifts become flagged transition events."""
    res = _travel(ctx, a)
    if not res.get("ok"):
        return res
    mins = int(res.get("travel_minutes") or 0)
    if mins > 0:
        days = _narr.advance(ctx.state, mins)
        ctx.flags["time_advanced"] = True          # the engine already moved the clock: no per-turn auto-advance too
        if days >= 1 and narrative.phase_name(ctx.state.minute) == "Night":
            ctx.adapter.rest()                     # a full day of marching ends at camp: spell slots/daily powers back
        res["travel_hours"] = round(mins / 60.0, 1)
        res["clock"] = _narr.clock_text(ctx.state)
        res["days_crossed"] = days
        if days:
            res["note"] = f"Days passed during the march: {days}. Honor the new clock when narrating."
    tr = res.get("transition")
    if res.get("stopped_by") == "mountain":        # legacy payloads/older saves may still wall peaks off
        res["stopped_by"] = "terrain_transition"
        res["transition"] = tr = {"from": res.get("current_terrain", "?"), "to": "mountain"}
    if res.get("stopped_by") == "terrain_transition":
        frm, to = (tr or {}).get("from", "?"), (tr or {}).get("to", "?")
        ctx.flags["terrain_transition"] = {"from": frm, "to": to}     # event flag for the orchestrator (game.py)
        res["events"] = [f"Terrain transition: {frm} -> {to}"]
        res["narrative_directive"] = TRANSITION_NOTE
    return res


TOOLS["world_travel"].fn = _travel_with_clock      # wrapper over the original tool; schema/name untouched

WORLD_RULES += """
- Movement costs time BY BIOME (Python moves the clock itself): plains 2 hours/tile, forest 4 hours/tile, mountain 8 hours... a full day/tile. world_travel reports travel_hours and the new clock; never do time math yourself.
- stopped_by 'terrain_transition' is NOT a barrier: the march halted because the party crossed into a new biome, snapping onto its first tile. Treat it as a cinematic trigger - describe the sweeping visual change of scenery, then let the player decide whether to push deeper (another world_travel continues from there)."""


# ---------------------------------------------------------------- one-shot directive plumbing (appended)
def take_transition_directive(ctx: ToolContext) -> str:
    """POP the terrain-transition flag set this turn by world_travel and return the specialized
    one-turn system directive ('Dynamic LLM Context'). game.py calls it right before narration, so
    the constraint exists for exactly ONE turn; the next turn's messages are built fresh without it."""
    tr = ctx.flags.pop("terrain_transition", None)
    if not tr:
        return ""
    return _narr.transition_directive(tr)


# ==================================================== exclusion-zone lifting (appended)
# The barrier itself is Python-owned data; the LLM may only request its removal through THIS
# sanctioned tool, and only when the story has genuinely earned it. The landscape underneath is
# whatever was already painted there (or open plains via the default-fallback rule) -- the model
# can never author revealed terrain from prose, so sequence-breaking stays impossible until the
# DM decides access is earned.
def _lift_exclusion(ctx: ToolContext, a: LiftExclusionArgs) -> dict:
    w = _world_of(ctx)
    if w is None:
        return {"ok": False, "error": "No world map has been generated yet."}
    by_coord = (not a.name.strip()) and a.x > -10**6 and a.y > -10**6
    res = _world.lift_exclusion(w, name=a.name, x=a.x if by_coord else None,
                                y=a.y if by_coord else None, radius=a.radius)
    if res.get("ok") and res.get("cloud_tiles_removed"):
        ctx.flags["exclusion_lifted"] = {"center": res["center"],
                                         "tiles": res["cloud_tiles_removed"]}
        res["events"] = [f"Exclusion barrier lifted at {res['center']}: "
                         f"{res['cloud_tiles_removed']} cloud tiles dissolved"]
        res["narrative_directive"] = (
            "EXCLUSION LIFTED: the magical cloud barrier here has truly fallen. Describe the fog "
            "tearing apart and the REAL landscape it concealed (use new_terrain_mix / "
            "sample_revealed - do not invent other scenery). This region is now travelable; high-"
            "level dangers inside are live.")
    return res


TOOLS["world_lift_exclusion"] = Tool(
    "world_lift_exclusion",
    "Lift one cloud ☁️ exclusion barrier NOW THAT THE STORY HAS EARNED ACCESS (final artifact "
    "claimed, boss gate solved, party appropriately leveled...). Python deletes ONLY the cloud "
    "tiles in range, revealing the real pre-generated landscape underneath, and reports exactly "
    "what was uncovered. Non-cloud obstacles (rivers, lakes) stay impassable. NEVER call this for "
    "a zone the party has not legitimately unlocked - blocked steps are objective engine data.",
    LiftExclusionArgs, _lift_exclusion)

WORLD_RULES += """
- Cloud exclusion zones can be PERMANENTLY LIFTED, but only by YOU calling world_lift_exclusion once the story has truly earned access to that area (e.g. the endgame key item was won). It dissolves only the ☁️ barrier tiles and returns the real terrain revealed underneath - narrate THAT data, never invented scenery. After a lift, world_travel into the region works normally; lifts are irreversible and must never be used to rescue a stuck player or skip progression."""


# ==================================================== skip travel / A* fast travel (appended)
# When the player asks to make for a KNOWN place ("I head toward the nearest village"), Python owns
# the whole journey: an A* search over the sparse grid finds the exact shortest LEGAL route within a
# 6x6 domain around the party, accumulating biome march times (plains 2h, forest 4h, mountain 8h/tile)
# and hard-blocking river/lake/cloud tiles. The clock advances in ONE atomic update; if nightfall is
# crossed the engine auto-camps. The model only narrates the returned metadata - it cannot invent a
# shortcut, phase through a barrier, or fudge the hours.
FAST_SEARCH_RADIUS = getattr(_world, "FAST_SEARCH_RADIUS", 6)


class FastTravelArgs(SoftModel):
    name: str = Field("", max_length=60, description="landmark name from your notes (record_fact), e.g. 'Oakhaven'; leave empty to reach the closest landmark matching tag")
    x: int = Field(-10**6, description="target coordinate X (snapped to the nearest recorded landmark in range; ignored if name is given)")
    y: int = Field(-10**6, description="target coordinate Y (paired with x; ignored if name is given)")
    tag: str = Field("", max_length=16, description="'nearest <tag>' requests: village, town, city, dungeon, forest, mountain, plains... picks the CLOSEST landmark of that kind inside the domain")
    radius: int = Field(0, ge=0, le=80, description=f"domain size override in tiles (default {FAST_SEARCH_RADIUS}; widen ONLY when the story justifies a long organized journey)")


def _fast_travel(ctx: ToolContext, a: FastTravelArgs) -> dict:
    w = _world_of(ctx)
    if w is None:
        return {"ok": False, "error": "No world map has been generated yet."}
    name = (a.name or "").strip()
    coords_given = a.x > -10**6 and a.y > -10**6
    if not name and not coords_given and (a.tag or "").strip():
        # "head toward the NEAREST village": pick the closest recorded landmark of that tag inside
        # the domain -- pure spatial math, so the model never has to remember coordinates.
        rad = a.radius or FAST_SEARCH_RADIUS
        px, py = w.player.x, w.player.y
        want = narrative._norm(a.tag)
        best = None                                   # (manhattan distance, display name, coords)
        for key, c in w.landmarks.items():
            nf = w.named_features.get(key, {})
            tag = narrative._norm(nf.get("tag", ""))
            if want not in (tag, narrative._norm(key)) or _manhattan_at(w, c) > rad:
                continue
            d = _manhattan_at(w, c)
            if best is None or d < best[0]:
                best = (d, nf.get("name") or key.title(), c)
        if best is None:
            return {"ok": False, "error": f"No known {a.tag} landmark within {rad} tiles. Survey with "
                                          f"world_look, or walk with world_travel and record what you find."}
        name = best[1]
    res = _world.fast_travel(w, name=name, x=a.x if (coords_given and not name) else None,
                             y=a.y if (coords_given and not name) else None,
                             radius=a.radius or FAST_SEARCH_RADIUS)
    if not res.get("ok"):
        if res.get("stopped_by") == "no_route":       # barrier completely cut off every land route
            stop = res.get("last_valid_tile") or [w.player.x, w.player.y]
            res["final_position"] = list(stop)
            res["events"] = [f"Fast travel blocked: no legal route to {res.get('destination', '?')}"]
            res["narrative_directive"] = ("FAST TRAVEL BLOCKED: rivers/lakes/cloud exclusion zones completely "
                                          "cut off every land route. The party holds position at the last valid "
                                          "tile - narrate the impassable barrier ahead, never a crossing.")
        return res
    if res.get("already_there"):
        res["clock"] = narrative.clock_text(ctx.state)
        return res

    # ---- Engine Clock: process the multi-tile path as ONE atomic update (bulk advance).
    mins = int(res.get("travel_minutes") or 0)
    days = narrative.advance(ctx.state, mins)
    ctx.flags["time_advanced"] = True                # the engine moved the clock: skip per-turn auto-advance
    res["days_crossed"] = days
    res["clock"] = narrative.clock_text(ctx.state)
    crossed_nightfall = mins > 0 and narrative.phase_name(ctx.state.minute) == "Night"
    if crossed_nightfall:
        ctx.adapter.rest()                           # night fell mid-trek: camp, restore daily resources
        res["auto_camp"] = True
        res["note"] = ("Night fell during the journey and the party made camp. Ask the player whether they "
                       "wish to rest here before continuing.")
    elif days:
        res["note"] = f"Days passed during the trek: {days}. Honor the new clock when narrating."
    res["events"] = [f"Fast travel: {res['tiles_traversed']} tiles to {res['destination']} "
                     f"({res['travel_hours']}h)" + (", night fell en route" if crossed_nightfall else "")]
    res["narrative_directive"] = (
        f"FAST TRAVEL COMPLETE: the party reached {res['destination']} after {res['tiles_traversed']} "
        f"tiles and {res['travel_hours']} hours on the road. Summarize the trip briefly in 2-3 sentences "
        f"upon their arrival at the gates; use terrain_mix for flavor only - do not invent encounters.")
    ctx.flags["fast_travel"] = {"destination": res["destination"], "hours": res["travel_hours"]}   # one-shot directive for game.py
    # CLI HUD flag (read by game.tools_line): distance traveled + time elapsed on the status line.
    ctx.flags["fast_travel_hud"] = {"tiles": res["tiles_traversed"], "hours": res["travel_hours"],
                                    "dest": res["destination"], "clock": res["clock"]}
    return res


def _manhattan_at(w, c) -> int:
    """Manhattan distance from the party to coordinate c (spatial helper for nearest-tag lookup)."""
    return abs(int(c[0]) - w.player.x) + abs(int(c[1]) - w.player.y)


TOOLS["world_fast_travel"] = Tool(
    "world_fast_travel",
    f"Skip-travel to a KNOWN landmark ({FAST_SEARCH_RADIUS}x{FAST_SEARCH_RADIUS} domain around the party by default). "
    "Python runs A* pathfinding over the real map: the exact shortest legal route (rivers, lakes and cloud "
    "exclusion zones are hard-blocked), accumulating march time BY BIOME (plains 2h, forest 4h, mountain 8h per "
    "tile), then advances the engine clock in one atomic update and snaps the party onto the destination tile. "
    "Use it when the player heads toward a place already recorded via record_fact ('the nearest village' -> "
    "tag='village'). If no legal land route exists the party halts at the last valid tile - narrate the blockage.",
    FastTravelArgs, _fast_travel)

WORLD_RULES += """
- SKIP TRAVEL: when the player heads toward a place ALREADY KNOWN (in your record_fact notes), call world_fast_travel(name=..., or tag='village' for the nearest of that kind, or x=/y= snapped to the nearest landmark). Python A*-pathfinds the exact shortest LEGAL route inside a 6x6 domain (hard-blocking rivers/lakes/clouds), charges real biome time (plains 2h, forest 4h, mountain 8h per tile), moves the clock in one atomic update, auto-camps if night falls, and snaps the party to the destination. Report tiles_traversed/travel_hours/clock exactly as returned; if stopped_by is 'no_route' the way is physically cut off - narrate the barrier, never a shortcut. Never fast-travel to a place the party has not discovered, and never to a still-sealed cloud zone."""


def take_fast_travel_directive(ctx: ToolContext) -> str:
    """POP this turn's fast-travel flag and return the ONE-TURN summary directive. game.py injects it
    into this turn's messages only; the next turn rebuilds without it (context stripping)."""
    ft = ctx.flags.pop("fast_travel", None)
    if not ft:
        return ""
    return (f"System Note: The party fast-traveled to {ft.get('destination', 'their destination')} "
            f"over {ft.get('hours', '?')} hours. Summarize the trip briefly in 2-3 sentences upon "
            f"their arrival at the gates. Do not invent obstacles or encounters along the road.")
