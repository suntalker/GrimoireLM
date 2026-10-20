"""The ONLY module that imports dnd5e_engine.

Everything here takes and returns plain dicts/strings. The rest of the project never sees
engine types, so swapping or forking the rules engine means rewriting this one file.

The engine owns every number (HP, AC, rolls, positions). The model only ever sees the
compact snapshots and event digests produced here, and can only act through the
whitelisted methods below.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import random
import re
import threading
from pathlib import Path
from typing import Any, Optional

import dnd5e_engine as E
from dnd5e_engine.lib_loader import get_lib_loader
from dnd5e_srd_data import DamagePart

from config import Config

PC_ID = "pc:hero"
PC_START_ZONE = "1,2"

CR_XP = {0: 10, 0.125: 25, 0.25: 50, 0.5: 100, 1: 200, 2: 450, 3: 700, 4: 1100, 5: 1800}

# What the model may ask for -> engine intent_type. Anything else is rejected.
ACTION_MAP = {
    "attack": "attack",
    "cast_spell": "cast_spell",
    "dodge": "dodge",
    "dash": "dash",
    "disengage": "disengage",
    "help": "help",
    "hide": "hide",
    "end_turn": "pass",
}

DAMAGE_TYPES = ("acid", "bludgeoning", "cold", "fire", "force", "lightning", "necrotic",
                "piercing", "poison", "psychic", "radiant", "slashing", "thunder")
DICE_RE = re.compile(r"^([1-4])d(4|6|8|10|12)([+-]\d{1,2})?$")
CUSTOM_LIMITS = {"hp": (1, 80), "ac": (8, 18), "attack_bonus": (-2, 7), "dexterity": (6, 20)}

MAX_CUSTOM_WEAPONS = 8
MAX_INVENTORY = 30

# --- weapon forge: real SRD chassis + budgeted affixes ---------------------------------
FORGE_CHASSIS = ("rapier", "shortsword", "scimitar", "longsword", "battleaxe", "warhammer", "mace", "morningstar",
                 "flail", "war-pick", "quarterstaff", "greatsword", "greataxe", "maul", "glaive", "halberd", "whip")
TIERS = {          # tier -> (point budget, max magical bonus, needs boss reward, min level)
    "uncommon": (8, 1, False, 1),
    "rare": (14, 2, True, 1),
    "legendary": (22, 3, True, 5),
}
ARMOR_SLUGS = ("padded-armor", "leather-armor", "hide-armor", "studded-leather-armor", "chain-shirt", "scale-mail",
               "ring-mail", "breastplate", "half-plate-armor", "chain-mail", "splint-armor", "plate-armor", "shield")
GEAR_ALIASES = {"chainmail": "chain-mail", "plate mail": "plate-armor", "platemail": "plate-armor",
                "full plate": "plate-armor", "studded leather": "studded-leather-armor", "half plate": "half-plate-armor",
                "splint mail": "splint-armor", "scale armor": "scale-mail", "breast plate": "breastplate"}
FREE_GEAR_MAX_GP = 100      # standard gear worth more than this cannot simply be handed out as loot
RIDER_RE = re.compile(r"^([12])d(4|6|8)$")
POWER_RE = re.compile(r"^([1-4])d(4|6|8)$")
POWER_RANGE_FT = 30
PHYSICAL_TYPES = ("bludgeoning", "piercing", "slashing")
ABILITIES = ("str", "dex", "con", "int", "wis", "cha")

# --- companions: martial classes only (casters would need spell choices the engine expects us to supply) ---
COMPANION_PRESETS = {                      # chassis only: the DM supplies spells / powers / flavor per recruit
    "fighter": {"scores": {"str": 16, "dex": 12, "con": 14, "int": 10, "wis": 10, "cha": 8},
                "equipment": ("longsword", "chain-mail", "shield"), "cast": None},
    "rogue": {"scores": {"str": 10, "dex": 16, "con": 12, "int": 12, "wis": 12, "cha": 10},
              "equipment": ("rapier", "leather-armor"), "cast": None},
    "barbarian": {"scores": {"str": 16, "dex": 13, "con": 15, "int": 8, "wis": 10, "cha": 10},
                  "equipment": ("greataxe",), "cast": None},
    "monk": {"scores": {"str": 10, "dex": 16, "con": 13, "int": 10, "wis": 14, "cha": 8}, "equipment": (), "cast": None},
    "wizard": {"scores": {"str": 8, "dex": 14, "con": 13, "int": 16, "wis": 12, "cha": 10},
               "equipment": ("quarterstaff",), "cast": "int"},
    "cleric": {"scores": {"str": 14, "dex": 10, "con": 13, "int": 10, "wis": 16, "cha": 10},
               "equipment": ("mace", "chain-mail", "shield"), "cast": "wis"},
    "druid": {"scores": {"str": 10, "dex": 12, "con": 14, "int": 10, "wis": 16, "cha": 8},
              "equipment": ("quarterstaff", "leather-armor"), "cast": "wis"},
    "bard": {"scores": {"str": 8, "dex": 14, "con": 12, "int": 10, "wis": 10, "cha": 16},
             "equipment": ("rapier", "leather-armor"), "cast": "cha"},
    "sorcerer": {"scores": {"str": 8, "dex": 14, "con": 14, "int": 10, "wis": 10, "cha": 16},
                 "equipment": ("dagger",), "cast": "cha"},
}
MAX_COMPANION_CANTRIPS = 3
MAX_COMPANION_POWER_POINTS = 12
SPELLS_PATH = Path(__file__).with_name("spells_verified.json")
BOSS_LIMITS = {"hp": (1, 200), "ac": (8, 20), "attack_bonus": (-2, 10), "dexterity": (6, 22)}


def load_spell_catalog() -> dict:
    """Spells the engine was proven (by verify_spells.py) to resolve. The DM may only hand out these."""
    try:
        return json.loads(SPELLS_PATH.read_text())
    except (OSError, ValueError):
        return {}


COMPANION_SPECIES = ("human", "elf", "dwarf", "halfling")
ALLY_ZONES = ("1,1", "1,3", "0,2", "0,1", "0,3")

UNARMED = "unarmed-strike"
UNARMED_ALIASES = {"fist", "fists", "punch", "unarmed", "unarmed strike", "unarmed_strike", "bare hands",
                   "hands", "kick", "none", "no weapon", "bare-handed", "unarmed-strike"}
ATTACK_FAIL_WHY = {
    "out_of_range": "target out of reach", "target_invalid": "invalid target",
    "no_action_economy": "no action left this turn", "action_unavailable": "that action is unavailable",
    "weapon_already_fired": "weapon already fired this turn", "target_is_charmer": "cannot attack the charmer",
    "no_granted_die": "no granted die available",
}

SKIP_EVENTS = {"area_targeted", "turn_phase", "turn_started", "turn_ended", "round_started", "round_ended", "intent_submitted"}


def _err(msg: str, **extra) -> dict:
    return {"ok": False, "error": msg, **extra}


def _cr(value: Any) -> float:
    if isinstance(value, str) and "/" in value:
        a, b = value.split("/")
        return float(a) / float(b)
    return float(value)


def _avg_dice(expr: str) -> float:
    n, die, mod = DICE_RE.match(expr).groups()
    return int(n) * (int(die) + 1) / 2 + int(mod or 0)


def _clean_custom(raw: dict, boss: bool = False) -> tuple[Optional[dict], Optional[str]]:
    """Validate and normalize a model-invented creature. The engine, not the model, enforces the limits."""
    try:
        name = " ".join(str(raw["name"]).split())[:40]
        c = {"name": name}
        for key, (lo, hi) in (BOSS_LIMITS if boss else CUSTOM_LIMITS).items():
            val = int(raw.get(key, 10 if key == "dexterity" else None))
            if not lo <= val <= hi:
                return None, f"{name or 'creature'}: {key}={val} is outside the allowed range {lo}..{hi}."
            c[key] = val
        dice = str(raw["damage_dice"]).replace(" ", "")
        if not DICE_RE.match(dice):
            return None, f"{name}: damage_dice '{dice}' invalid. Use NdM or NdM+K with N 1-4, M in 4/6/8/10/12, e.g. '1d8+2'."
        dtype = str(raw["damage_type"]).lower()
        if dtype not in DAMAGE_TYPES:
            return None, f"{name}: damage_type '{dtype}' invalid. Allowed: {', '.join(DAMAGE_TYPES)}."
        if not name:
            return None, "Every custom creature needs a name."
    except (KeyError, TypeError, ValueError) as ex:
        return None, f"Malformed custom creature ({type(ex).__name__}: {ex})."
    c.update(damage_dice=dice, damage_type=dtype)
    c["threat"] = c["hp"] * _avg_dice(dice)
    return c, None


def _cell(zone: str) -> tuple[int, int]:
    c, r = zone.split(",")
    return int(c), int(r)


def _status(hp: int, hp_max: int) -> str:
    if hp <= 0:
        return "down"
    ratio = hp / max(hp_max, 1)
    if ratio >= 1:
        return "unhurt"
    if ratio > 0.5:
        return "lightly wounded"
    if ratio > 0.25:
        return "bloodied"
    return "near death"


class _Overlay:
    """Serves improvised items on top of the bundled SRD corpus; everything else is delegated.

    `kinds` records what each custom slug is, so a forged weapon is never offered when the engine asks for armor.
    """

    def __init__(self, base):
        self.base = base
        self.extra: dict = {}
        self.kinds: dict[str, str] = {}      # slug -> "weapon" | "armor" | "item"

    def put(self, slug: str, obj, kind: str) -> None:
        self.extra[slug] = obj
        self.kinds[slug] = kind

    def get_weapon(self, slug):
        return self.extra[slug] if self.kinds.get(slug) == "weapon" else self.base.get_weapon(slug)

    def get_armor(self, slug):
        return self.extra[slug] if self.kinds.get(slug) == "armor" else self.base.get_armor(slug)

    def get_item(self, slug):
        return self.extra[slug] if slug in self.extra else self.base.get_item(slug)

    def __getattr__(self, name):          # only called for attributes not found above
        return getattr(self.base, name)


class EngineAdapter:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        base = get_lib_loader()
        self.loader = _Overlay(base.base if isinstance(base, _Overlay) else base)
        E.configure_lib_loader(self.loader)   # must be installed before any combat starts
        self.custom_weapons: list[dict] = []
        self.custom_armor: list[dict] = []
        self.inventory: list[dict] = []
        self.power_uses: dict[str, dict] = {}   # weapon slug -> {name, left, max}
        self.gold = 15                          # gp ledger; only the engine changes it
        self.boss_rewards = 0                   # unspent 'a boss fell' rewards (gate for rare+ weapons)
        self._boss_fight = False
        # Own event loop on a background thread: works even inside Spyder/Jupyter, where the
        # main thread already has a running asyncio loop and run_until_complete would fail.
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.rng = random.Random(cfg.seed)
        self.pc = None            # PartyMemberSpec (carries current HP between encounters)
        self._fresh = None        # pristine copy used by rest()
        self.sheet = None         # DerivedSheet, used for skill checks
        self.pc_params: dict = {}
        self.handle = None        # live CombatHandle or None
        self.names: dict[str, str] = {}
        self.enemy_hp_max: dict[str, int] = {}
        self._encounters = 0
        self._saw_out_of_range = False
        self._oor_actors: set = set()           # who just failed an attack as out of range (allies use this too)
        self.spells: dict = load_spell_catalog()
        self.roster: dict[str, dict] = {}       # companions: id -> {name, class, status: active|inactive|dead, hp...}

    # ------------------------------------------------------------------ plumbing
    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

    def _n(self, ident: Any) -> Any:
        return self.names.get(ident, ident) if isinstance(ident, str) else ident

    def _is_weapon(self, slug: str) -> bool:
        try:
            return self.loader.get_weapon(slug) is not None
        except Exception:
            return False

    # ------------------------------------------------------------------ character
    def create_pc(self, name: str, species_slug: str = "human", class_slug: str = "fighter",
                  level: int = 1, ability_scores: Optional[dict] = None,
                  equipment: tuple = ("longsword", "chain-mail", "shield")) -> dict:
        scores = ability_scores or {"str": 16, "dex": 12, "con": 14, "int": 10, "wis": 10, "cha": 8}
        bs = E.make_build_spec(species_slug=species_slug, class_slug=class_slug, level=level,
                               ability_scores=scores, equipment=tuple(equipment))
        self.sheet = E.derive_sheet(bs, loader=self.loader)
        inst = E.CombatInstance(entity_id=PC_ID, name=name, zone_id=PC_START_ZONE, initiative=None)
        self.pc = self._with_perks(E.build_party_member(bs, inst, loader=self.loader))
        self._fresh = self.pc
        self.names[PC_ID] = name
        self.pc_params = dict(name=name, species_slug=species_slug, class_slug=class_slug, level=level,
                              ability_scores=scores, equipment=list(equipment))
        return self.snapshot()

    def export_state(self) -> dict:
        """What must survive a restart. (Mid-combat state is not persisted; save between encounters.)"""
        return {"pc_params": self.pc_params, "hp_current": self.pc.hp_current if self.pc else None,
                "custom_weapons": self.custom_weapons, "custom_armor": self.custom_armor, "inventory": self.inventory,
                "power_uses": self.power_uses, "boss_rewards": self.boss_rewards, "gold": self.gold,
                "roster": self.roster}

    def restore(self, data: dict) -> None:
        if not data or not data.get("pc_params"):
            return
        for spec in data.get("custom_weapons", []):      # re-register BEFORE rebuilding the character
            try:
                self._register_weapon(spec)
            except Exception:                            # an old-format or corrupt weapon must not block loading
                continue
            self.custom_weapons.append(spec)
        for spec in data.get("custom_armor", []):
            try:
                self._register_armor(spec)
            except Exception:
                continue
            self.custom_armor.append(spec)
        self.inventory = list(data.get("inventory", []))
        self.roster = dict(data.get("roster", {}))
        for m in self.roster.values():                     # companion powers are loader items too
            for p in m.get("powers", []):
                self._register_power({"slug": p["slug"], "power": p})
        self.power_uses = {k: v for k, v in data.get("power_uses", {}).items()
                           if k in self.loader.extra or (k + "-power") in self.loader.extra}   # weapons/armor, or companion powers
        self.boss_rewards = int(data.get("boss_rewards", 0))
        self.gold = int(data.get("gold", self.gold))
        self.create_pc(**data["pc_params"])
        if data.get("hp_current") is not None:
            self.pc = self.pc.model_copy(update={"hp_current": int(data["hp_current"])})

    # ------------------------------------------------------------------ items
    @staticmethod
    def _rid(seed: str) -> str:
        return "PWR" + hashlib.md5(seed.encode()).hexdigest()[:13]

    def _register_weapon(self, spec: dict) -> None:
        """Build a unique weapon: real SRD chassis + extra damage dice (+ optional companion 'power' item)."""
        base = self.loader.base.get_weapon(spec["chassis"])
        d = base.model_dump(mode="python")
        bonus = int(spec.get("magical_bonus", 0))
        d.update(slug=spec["slug"], name=spec["name"], magical_bonus=bonus, magical=True, cost_gp=0.0,
                 rarity=spec.get("tier", "uncommon"),
                 damage_parts=list(d["damage_parts"]) + [dict(r) for r in spec.get("riders", [])])
        self.loader.put(spec["slug"], type(base).model_validate(d), "weapon")
        self._register_power(spec)

    def _register_armor(self, spec: dict) -> None:
        """Build unique armor: real SRD armor chassis + magical AC bonus (resistances are applied to the character)."""
        base = self.loader.base.get_armor(spec["chassis"])
        d = base.model_dump(mode="python")
        d.update(slug=spec["slug"], name=spec["name"], magical_bonus=int(spec.get("magical_bonus", 0)), magical=True,
                 cost_gp=0.0, rarity=spec.get("tier", "uncommon"))
        self.loader.put(spec["slug"], type(base).model_validate(d), "armor")
        self._register_power(spec)

    def _register_power(self, spec: dict) -> None:
        """Companion item carrying a single-target save-for-damage activity; the adapter enforces the charges."""
        power = spec.get("power")
        if not power:
            return
        jav = self.loader.base.get_weapon("javelin-of-lightning").model_dump(mode="python")
        save = copy.deepcopy(next(a for a in jav["activities"] if a["kind"] == "save"))
        num, den = POWER_RE.match(power["damage_dice"]).groups()
        save["id"] = self._rid(spec["slug"])
        save["name"] = power["name"]
        save["damage"]["parts"][0].update(number=int(num), denomination=int(den), types=[power["damage_type"]])
        save["damage"]["on_save"] = power.get("on_save", "half")
        save["save"]["ability"] = [power["ability"]]
        save["range"].update(value=str(POWER_RANGE_FT), units="ft")
        save["target"]["template"].update(type="", size="", width="", units="ft")
        save["target"]["affects"].update(type="creature", count="1")
        save["consumption"]["spell_slot"] = False
        save["uses"]["max"] = ""           # the engine does not enforce charges; the adapter does
        pot = self.loader.base.get_item("potion-of-healing")
        item = pot.model_dump(mode="python")
        item.update(slug=spec["slug"] + "-power", name=power["name"], activities=[save], passive_effects=[], uses=None)
        self.loader.put(spec["slug"] + "-power", type(pot).model_validate(item), "item")
        self.power_uses.setdefault(spec["slug"], {"name": power["name"], "left": power["charges"], "max": power["charges"]})

    @staticmethod
    def _power_cost(pw: Optional[dict]) -> int:
        if not pw:
            return 0
        n, die = POWER_RE.match(pw["damage_dice"]).groups()
        return math.ceil(int(n) * (int(die) + 1) / 2 / 2) + 2 * int(pw["charges"])

    @staticmethod
    def _forge_cost(spec: dict) -> int:
        cost = 4 * int(spec.get("magical_bonus", 0))
        for r in spec.get("riders", []):
            n, die = RIDER_RE.match(r["dice"]).groups()
            cost += math.ceil(int(n) * (int(die) + 1) / 2)
        return cost + EngineAdapter._power_cost(spec.get("power"))

    @staticmethod
    def _armor_cost(spec: dict) -> int:
        cost = 4 * int(spec.get("magical_bonus", 0))
        cost += sum(8 if r in PHYSICAL_TYPES else 4 for r in spec.get("resistances", []))
        return cost + EngineAdapter._power_cost(spec.get("power"))

    def _armor_kind(self, slug: str) -> Optional[str]:
        """'body', 'shield', or None for things that are not armor."""
        if slug == "shield":
            return "shield"
        if slug in ARMOR_SLUGS:
            return "body"
        spec = next((a for a in self.custom_armor if a["slug"] == slug), None)
        if spec:
            return "shield" if spec["chassis"] == "shield" else "body"
        return None

    def _with_perks(self, pc):
        """Fold worn unique armor's damage resistances into the character the engine will fight with."""
        res = set(pc.damage_resistances)
        for spec in self.custom_armor:
            if spec["slug"] in pc.equipment:
                res.update(spec.get("resistances", []))
        return pc if res == set(pc.damage_resistances) else pc.model_copy(update={"damage_resistances": sorted(res)})

    def _rebuild_pc(self, add: Optional[str] = None, remove: Optional[str] = None) -> None:
        params = dict(self.pc_params)
        equipment = [e for e in params["equipment"] if e != remove] + ([add] if add else [])
        hp, slots = self.pc.hp_current, dict(self.pc.spell_slots)
        self.create_pc(**{**params, "equipment": tuple(equipment)})
        self.pc = self.pc.model_copy(update={"hp_current": hp, "spell_slots": slots})

    # ------------------------------------------------------------------ standard gear (real engine equipment)
    def _is_armor(self, slug: str) -> bool:
        try:
            return slug in ARMOR_SLUGS and self.loader.get_armor(slug) is not None
        except Exception:
            return False

    @staticmethod
    def _pretty(slug: str) -> str:
        return slug.replace("-", " ")

    def _gear_price(self, slug: str) -> int:
        item = self.loader.base.get_armor(slug) if slug in ARMOR_SLUGS else self.loader.base.get_weapon(slug)
        return max(1, math.ceil(float(item.cost_gp)))

    def _standard_gear_in(self, name: str) -> Optional[str]:
        """If a free-text item name is really standard SRD gear, return its slug (so it can't dodge engine pricing)."""
        text = " ".join(str(name).lower().replace("-", " ").split())
        for alias, slug in GEAR_ALIASES.items():
            if re.search(rf"\b{re.escape(alias)}\b", text):
                return slug
        for slug in FORGE_CHASSIS + ARMOR_SLUGS:
            if re.search(rf"\b{re.escape(slug.replace('-', ' '))}\b", text):
                return slug
        return None

    def _ac_with(self, equipment) -> int:
        p = self.pc_params
        bs = E.make_build_spec(species_slug=p["species_slug"], class_slug=p["class_slug"], level=p["level"],
                               ability_scores=p["ability_scores"], equipment=tuple(equipment))
        return E.derive_sheet(bs, loader=self.loader).ac

    def _plan_gear(self, slug: str, allow_equal: bool = False, kind: Optional[str] = None) -> tuple[Optional[dict], Optional[dict]]:
        """Decide how gear would change the character. Body armor SWAPS (the engine allows only one suit)."""
        eq = list(self.pc.equipment)
        pretty = self._pretty(slug)
        if slug in eq:
            return None, _err(f"Already owns {slug}.", auto_always=True, auto_reply=f"You already have {pretty}.")
        kind = kind or (self._armor_kind(slug) if slug in ARMOR_SLUGS else None)
        if kind:
            current = [e for e in eq if self._armor_kind(e) == kind]
            remove = current[0] if current else None
            new_ac = self._ac_with([e for e in eq if e != remove] + [slug])
            worse = new_ac < self.pc.ac if allow_equal else new_ac <= self.pc.ac
            if worse:
                now = f"AC {self.pc.ac} now, {new_ac} with it"
                if allow_equal:
                    return None, _err(f"{slug} would lower AC ({now}). Pick a heavier chassis or a higher magical bonus.")
                return None, _err(f"{slug} would not improve AC ({now}).", auto_always=True,
                                  auto_reply=f"The {pretty} wouldn't improve your armor class ({now}).")
            return {"add": slug, "remove": remove, "ac": new_ac}, None
        return {"add": slug, "remove": None}, None

    def _apply_gear(self, plan: dict) -> dict:
        self._rebuild_pc(add=plan["add"], remove=plan["remove"])
        out = {"equipped": plan["add"], "ac": self.pc.ac}
        if plan.get("remove"):
            out["replaced"] = plan["remove"]
        return out

    def grant_item(self, name: str, description: str = "", gear: str = "") -> dict:
        """Found items. A plain trinket (key, map, letter), or standard gear (cheap SRD weapon/armor) that is truly equipped."""
        if self.pc is None:
            return _err("No character exists yet.")
        name = " ".join(str(name).split())[:40]
        if not name:
            return _err("The item needs a name.")
        if len(self.inventory) >= MAX_INVENTORY:
            return _err(f"Inventory is full ({MAX_INVENTORY} items).")
        if gear:
            if self.handle:
                return _err("Gear can only be granted outside combat.")
            if gear not in FORGE_CHASSIS + ARMOR_SLUGS:
                return _err(f"Unknown gear '{gear}'.", gear_options=list(FORGE_CHASSIS + ARMOR_SLUGS))
            if self._gear_price(gear) > FREE_GEAR_MAX_GP:
                return _err(f"{gear} is worth too much to simply hand out ({self._gear_price(gear)} gp > {FREE_GEAR_MAX_GP}).")
            plan, bad = self._plan_gear(gear)
            if bad:
                return bad
            return {"ok": True, "granted": self._pretty(gear), "kind": "gear", **self._apply_gear(plan)}
        found = self._standard_gear_in(name)
        if found:
            kind = "armor" if found in ARMOR_SLUGS else "weapon"
            return _err(f"'{name}' is standard {kind}. Call grant_item with gear='{found}' so it really works in the engine.")
        self.inventory.append({"name": name, "description": str(description)[:240]})
        return {"ok": True, "granted": name, "kind": "trinket"}

    @staticmethod
    def _norm(text: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(text).lower()).strip()

    def change_gold(self, amount: int, reason: str = "") -> dict:
        """The ONLY way gold moves: loot/rewards (+) or payments/bribes (-). The balance can never go negative."""
        amount, level = int(amount), int(self.pc_params.get("level", 1))
        if amount == 0:
            return _err("Amount must not be zero.")
        if amount > 25 * level:
            return _err(f"At most {25 * level} gp can be gained at once at level {level}.")
        if self.gold + amount < 0:
            return _err(f"Not enough gold: have {self.gold} gp, need {-amount} gp.", auto_always=True,
                        auto_reply=f"You can't afford that: you have {self.gold} gp but need {-amount} gp.")
        self.gold += amount
        return {"ok": True, "change_gp": amount, "gold": self.gold, "reason": str(reason)[:100]}

    def remove_item(self, name: str) -> dict:
        """Remove a plain item the player gave away, used up, or lost. Unique weapons cannot be discarded this way."""
        key = self._norm(name)
        hits = [i for i in self.inventory if key and (key == self._norm(i["name"]) or key in self._norm(i["name"]))]
        exact = [i for i in hits if key == self._norm(i["name"])]
        hits = exact or hits
        if not hits:
            return _err(f"No item called '{name}'.", inventory=[i["name"] for i in self.inventory], requested=name,
                        auto_reply=f"You don't have a '{name}'.")
        if len(hits) > 1:
            return _err(f"'{name}' matches several items.", matches=[i["name"] for i in hits])
        item = hits[0]
        if item.get("weapon_id") or item.get("armor_id"):
            return _err(f"{item['name']} is equipment and cannot be discarded this way.")
        self.inventory.remove(item)
        return {"ok": True, "removed": item["name"], "gold": self.gold}

    def buy_item(self, item: str, price_gp: int = 1, description: str = "", weapon: str = "", armor: str = "") -> dict:
        """Atomic purchase: check funds, deduct gold, grant the item. Standard weapons/armor are priced and equipped by the engine."""
        if self.pc is None:
            return _err("No character exists yet.")
        name = " ".join(str(item).split())[:40]
        if not name:
            return _err("The item needs a name.")
        if weapon and armor:
            return _err("Buy one thing at a time: set either weapon or armor.")
        gear = weapon or armor
        if gear:
            if self.handle:
                return _err("Gear can only be bought outside combat.")
            allowed = FORGE_CHASSIS if weapon else ARMOR_SLUGS
            if gear not in allowed or not (self._is_weapon(gear) if weapon else self._is_armor(gear)):
                return _err(f"Unknown {'weapon' if weapon else 'armor'} '{gear}'.", options=list(allowed))
            plan, bad = self._plan_gear(gear)
            if bad:
                return bad
            price, name = self._gear_price(gear), self._pretty(gear).title()
        else:
            found = self._standard_gear_in(name)
            if found:
                param = "armor" if found in ARMOR_SLUGS else "weapon"
                return _err(f"'{name}' is standard gear: call buy_item with {param}='{found}' "
                            "(the engine sets the price and really equips it).")
            if len(self.inventory) >= MAX_INVENTORY:
                return _err(f"Inventory is full ({MAX_INVENTORY} items).")
            price = int(price_gp)
            if not 1 <= price <= 100:
                return _err("price_gp must be between 1 and 100 (use grant_item for free items).")
        if self.gold < price:
            return _err(f"Cannot afford {name}: costs {price} gp, have {self.gold} gp.", auto_always=True,
                        auto_reply=f"You can't afford the {name}: it costs {price} gp and you have {self.gold} gp.")
        self.gold -= price
        if gear:
            return {"ok": True, "bought": name, "paid_gp": price, "gold_left": self.gold, **self._apply_gear(plan)}
        self.inventory.append({"name": name, "description": str(description)[:240]})
        return {"ok": True, "bought": name, "paid_gp": price, "gold_left": self.gold}

    def _tier_gate(self, tier: str, magical_bonus: int) -> tuple[int, bool, Optional[dict]]:
        if tier not in TIERS:
            return 0, False, _err(f"Unknown tier '{tier}'.", tiers=list(TIERS))
        budget, max_bonus, needs_boss, min_level = TIERS[tier]
        level = int(self.pc_params.get("level", 1))
        if level < min_level:
            return 0, False, _err(f"A {tier} item needs character level {min_level}+. Use a lower tier.")
        if needs_boss and self.boss_rewards <= 0:
            return 0, False, _err(f"A {tier} item is only awarded after defeating a boss-tier foe. Use tier 'uncommon'.")
        if not 0 <= int(magical_bonus) <= max_bonus:
            return 0, False, _err(f"A {tier} item allows a magical bonus of at most +{max_bonus}.")
        return budget, needs_boss, None

    def _clean_power(self, power: Optional[dict]) -> tuple[Optional[dict], Optional[dict]]:
        if not power:
            return None, None
        pdice, ptype = str(power.get("damage_dice", "")).replace(" ", ""), str(power.get("damage_type", "")).lower()
        pname = " ".join(str(power.get("name", "")).split())[:30]
        ability, charges = str(power.get("ability", "")).lower(), int(power.get("charges", 0))
        if not (pname and POWER_RE.match(pdice) and ptype in DAMAGE_TYPES and ability in ABILITIES and 1 <= charges <= 3):
            return None, _err("Bad power: needs name, damage_dice 1d4..4d8, a valid damage_type, ability "
                              f"({'/'.join(ABILITIES)}) and charges 1-3.")
        return {"name": pname, "ability": ability, "damage_dice": pdice, "damage_type": ptype,
                "on_save": "none" if power.get("on_save") == "none" else "half", "charges": charges}, None

    def forge_armor(self, name: str, lore: str, chassis: str, tier: str = "uncommon", magical_bonus: int = 0,
                    resistances: Optional[list] = None, power: Optional[dict] = None) -> dict:
        """Create unique armor: real SRD armor chassis + magical AC bonus + damage resistances + optional power."""
        if self.pc is None:
            return _err("No character exists yet.")
        if self.handle:
            return _err("Armor can only be forged outside combat.")
        name = " ".join(str(name).split())[:40]
        if not name:
            return _err("The armor needs a name.")
        if chassis not in ARMOR_SLUGS or not self._is_armor(chassis):
            return _err(f"Unknown chassis '{chassis}'.", chassis_options=list(ARMOR_SLUGS))
        budget, needs_boss, bad = self._tier_gate(tier, magical_bonus)
        if bad:
            return bad
        res = []
        for r in (resistances or []):
            r = str(r).lower()
            if r not in DAMAGE_TYPES:
                return _err(f"Unknown damage type '{r}'. Allowed: {', '.join(DAMAGE_TYPES)}.")
            if r not in res:
                res.append(r)
        if len(res) > 2:
            return _err("At most 2 damage resistances.")
        clean_power, bad = self._clean_power(power)
        if bad:
            return bad
        if len(self.custom_armor) >= MAX_CUSTOM_WEAPONS or len(self.inventory) >= MAX_INVENTORY:
            return _err("No room for another unique armor piece.")
        base_slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "armor"
        slug, n = base_slug, 2
        while slug in self.loader.extra or self._is_weapon(slug) or self._is_armor(slug):
            slug, n = f"{base_slug}-{n}", n + 1
        spec = dict(slug=slug, name=name, lore=str(lore)[:240], chassis=chassis, tier=tier,
                    magical_bonus=int(magical_bonus), resistances=res, power=clean_power)
        cost = self._armor_cost(spec)
        if cost > budget:
            return _err(f"Too powerful for a {tier} item: {cost} points > budget {budget}. Costs: +1 AC=4, energy resistance=4, "
                        "physical resistance=8, power=ceil(avg dice/2)+2 per charge. Remove or shrink something.")
        self._register_armor(spec)
        self.custom_armor.append(spec)          # registered first so planning can see the new piece
        plan, bad = self._plan_gear(slug, allow_equal=True, kind="shield" if chassis == "shield" else "body")
        if bad:                                  # roll back: nothing was granted or spent
            self.custom_armor.pop()
            self.loader.extra.pop(slug, None)
            self.loader.extra.pop(slug + "-power", None)
            self.power_uses.pop(slug, None)
            return bad
        self.inventory.append({"name": name, "description": spec["lore"], "armor_id": slug})
        if needs_boss:
            self.boss_rewards -= 1
        out = self._apply_gear(plan)
        return {"ok": True, "forged": name, "armor_id": slug, "tier": tier, "points": f"{cost}/{budget}",
                "resistances": res, "has_power": bool(clean_power), **out}

    def forge_weapon(self, name: str, lore: str, chassis: str, tier: str = "uncommon", magical_bonus: int = 0,
                     riders: Optional[list] = None, power: Optional[dict] = None) -> dict:
        """Create a unique weapon from a real SRD chassis plus budgeted affixes. Fixed forever once created."""
        if self.pc is None:
            return _err("No character exists yet.")
        if self.handle:
            return _err("Weapons can only be forged outside combat.")
        name = " ".join(str(name).split())[:40]
        if not name:
            return _err("The weapon needs a name.")
        if chassis not in FORGE_CHASSIS or not self._is_weapon(chassis):
            return _err(f"Unknown chassis '{chassis}'.", chassis_options=list(FORGE_CHASSIS))
        if tier not in TIERS:
            return _err(f"Unknown tier '{tier}'.", tiers=list(TIERS))
        budget, max_bonus, needs_boss, min_level = TIERS[tier]
        level = int(self.pc_params.get("level", 1))
        if level < min_level:
            return _err(f"A {tier} weapon needs character level {min_level}+. Use a lower tier.")
        if needs_boss and self.boss_rewards <= 0:
            return _err(f"A {tier} weapon is only awarded after defeating a boss-tier foe. Use tier 'uncommon'.")
        if not 0 <= int(magical_bonus) <= max_bonus:
            return _err(f"A {tier} weapon allows a magical bonus of at most +{max_bonus}.")
        riders = riders or []
        if len(riders) > 2:
            return _err("At most 2 rider damage entries.")
        clean_riders = []
        for r in riders:
            dice, dtype = str(r.get("dice", "")).replace(" ", ""), str(r.get("damage_type", "")).lower()
            if not RIDER_RE.match(dice) or dtype not in DAMAGE_TYPES:
                return _err(f"Bad rider {r}. Dice must be 1d4..2d8 (e.g. '1d6'); damage_type one of {', '.join(DAMAGE_TYPES)}.")
            clean_riders.append({"dice": dice, "damage_type": dtype})
        clean_power = None
        if power:
            pdice, ptype = str(power.get("damage_dice", "")).replace(" ", ""), str(power.get("damage_type", "")).lower()
            pname = " ".join(str(power.get("name", "")).split())[:30]
            ability, charges = str(power.get("ability", "")).lower(), int(power.get("charges", 0))
            if not (pname and POWER_RE.match(pdice) and ptype in DAMAGE_TYPES and ability in ABILITIES and 1 <= charges <= 3):
                return _err("Bad power: needs name, damage_dice 1d4..4d8, a valid damage_type, ability "
                            f"({'/'.join(ABILITIES)}) and charges 1-3.")
            clean_power = {"name": pname, "ability": ability, "damage_dice": pdice, "damage_type": ptype,
                           "on_save": "none" if power.get("on_save") == "none" else "half", "charges": charges}
        if len(self.custom_weapons) >= MAX_CUSTOM_WEAPONS or len(self.inventory) >= MAX_INVENTORY:
            return _err("No room for another unique weapon.")
        base_slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "weapon"
        slug, n = base_slug, 2
        while slug in self.loader.extra or self._is_weapon(slug):
            slug, n = f"{base_slug}-{n}", n + 1
        spec = dict(slug=slug, name=name, lore=str(lore)[:240], chassis=chassis, tier=tier,
                    magical_bonus=int(magical_bonus), riders=clean_riders, power=clean_power)
        cost = self._forge_cost(spec)
        if cost > budget:
            return _err(f"Too powerful for a {tier} weapon: {cost} points > budget {budget}. Costs: +1 bonus=4, "
                        "rider=ceil(avg dice), power=ceil(avg dice/2)+2 per charge. Remove or shrink something.")
        self._register_weapon(spec)
        self.custom_weapons.append(spec)
        self.inventory.append({"name": name, "description": spec["lore"], "weapon_id": slug})
        if needs_boss:
            self.boss_rewards -= 1
        self._rebuild_pc(add=slug)
        return {"ok": True, "forged": name, "weapon_id": slug, "tier": tier, "points": f"{cost}/{budget}",
                "has_power": bool(clean_power)}

    def rest(self) -> dict:
        """Long rest: full HP, spell slots, and weapon-power charges. (Feature-use recovery is not wired up yet.)"""
        if self.handle:
            return _err("Cannot rest during combat.")
        self.pc = self.pc.model_copy(update={
            "hp_current": self._fresh.hp_max,
            "spell_slots": dict(self._fresh.spell_slots),
            "pact_slots": dict(self._fresh.pact_slots),
        })
        for pw in self.power_uses.values():
            pw["left"] = pw["max"]
        for m in self.roster.values():
            if m["status"] != "dead":
                m["hp_current"] = None            # None = full health
        return {"ok": True, "pc_hp": self.pc.hp_current, "pc_hp_max": self.pc.hp_max}

    def _weapons(self) -> list[str]:
        owned = [w for w in self.pc.equipment if self._is_weapon(w)]
        return owned + ([UNARMED] if self._is_weapon(UNARMED) else [])

    def _resolve_weapon(self, raw: str) -> Optional[str]:
        weapons = self._weapons()
        key = (raw or "").strip().lower()
        if not key:
            return next((w for w in weapons if w != UNARMED), UNARMED if UNARMED in weapons else None)
        if key in UNARMED_ALIASES:
            return UNARMED if UNARMED in weapons else None
        norm = key.replace(" ", "-").replace("_", "-")
        for w in weapons:
            if norm == w.lower():
                return w
        hits = [w for w in weapons if norm in w.lower() or w.lower() in norm]
        return hits[0] if len(hits) == 1 else None

    def _resolve_target(self, raw: str, v) -> tuple[Optional[str], Optional[dict]]:
        """Accept an id, a name, or (if exactly one enemy lives) nothing. Small models are sloppy here."""
        living = sorted(v.encounter_ids - v.dead_ids)
        key = (raw or "").strip().lower()
        if key in living:
            return key, None
        if key:
            hits = [e for e in living if key == self.names.get(e, "").lower()
                    or key in self.names.get(e, "").lower() or self.names.get(e, "").lower() in key]
            if len(hits) == 1:
                return hits[0], None
        if len(living) == 1:
            return living[0], None
        return None, _err(f"Invalid or ambiguous target '{raw}'.",
                          valid_targets=[{"id": e, "name": self.names.get(e, e)} for e in living])

    # ------------------------------------------------------------------ companions
    def _spell_label(self, actor: str, spell: str) -> str:
        m = self.roster.get(actor) or {}
        return (m.get("spell_names") or {}).get(spell) or self.spells.get(spell, {}).get("name") or spell.replace("-", " ").title()

    def _companion_spec(self, m: dict):
        return E.make_build_spec(species_slug=m["species_slug"], class_slug=m["class_slug"], level=m["level"],
                                 ability_scores=m["ability_scores"], equipment=tuple(m["equipment"]))

    def _build_companion(self, m: dict):
        spec = E.build_party_member(self._companion_spec(m), E.CombatInstance(
            entity_id=m["id"], name=m["name"], zone_id="1,1", initiative=None), loader=self.loader)
        upd: dict = {}
        ability = COMPANION_PRESETS[m["class_slug"]]["cast"]
        if m.get("spells") and ability:
            upd["spells_known"] = list(m["spells"])
            upd["spell_abilities"] = {sp: ability for sp in m["spells"]}
        if m.get("hp_current") is not None:
            upd["hp_current"] = max(0, int(m["hp_current"]))
        return spec.model_copy(update=upd) if upd else spec

    def _roster_view(self) -> list[dict]:
        out = []
        for m in self.roster.values():
            row = {"name": m["name"], "class": m["class_slug"], "status": m["status"],
                   "hp": f"{m['hp_max'] if m['hp_current'] is None else m['hp_current']}/{m['hp_max']}"}
            if m.get("title"):
                row["title"] = m["title"]             # spells/powers stay out of the DM's context; /scene shows them
            out.append(row)
        return out

    def _find_member(self, name: str) -> Optional[dict]:
        key = self._norm(name)
        exact = [m for m in self.roster.values() if self._norm(m["name"]) == key]
        hits = exact or [m for m in self.roster.values() if key and (key in self._norm(m["name"]) or self._norm(m["name"]) in key)]
        return hits[0] if len(hits) == 1 else None

    def find_spells(self, kind: str = "", max_level: int = 1, damage_type: str = "", query: str = "") -> dict:
        """Browse the VERIFIED spell catalog. Rename picks to fit the setting when recruiting."""
        if not self.spells:
            return _err("The verified spell catalog (spells_verified.json) is missing. Run verify_spells.py.")
        rows = []
        for slug, sp in sorted(self.spells.items(), key=lambda kv: (kv[1]["level"], kv[0])):
            if sp["level"] > int(max_level):
                continue
            if kind == "heal" and not sp["heal"]:
                continue
            if kind == "damage" and not sp["damage_types"]:
                continue
            if kind == "attack" and "attack" not in sp["kinds"]:
                continue
            if damage_type and damage_type not in sp["damage_types"]:
                continue
            if query and self._norm(query) not in self._norm(slug + " " + sp["name"]):
                continue
            bits = [f"L{sp['level']}" if sp["level"] else "cantrip"]
            if sp["heal"]:
                bits.append(f"heals {sp['heal']}")
            if sp["damage_types"]:
                bits.append(f"{sp['damage'] or 'damage'} {'/'.join(sp['damage_types'])}")
            if sp["save"]:
                bits.append("/".join(a.upper() for a in sp["save"]) + " save")
            if sp["shape"]:
                bits.append(sp["shape"])
            bits.append(f"reach {sp['reach_ft']}ft")
            rows.append(f"{slug} | {sp['name']} | " + ", ".join(bits))
        return {"ok": True, "count": len(rows), "spells": rows[:14],
                "note": "" if len(rows) <= 14 else "Narrow the search (kind, max_level, damage_type, query)."}

    def _pick_spells(self, class_slug: str, picks: list, max_slot: int) -> tuple[Optional[list], Optional[dict], Optional[dict]]:
        """Validate the DM's spell list. Returns (slugs, display_names, error)."""
        if not picks:
            return [], {}, None
        if not COMPANION_PRESETS[class_slug]["cast"]:
            casters = [c for c, p in COMPANION_PRESETS.items() if p["cast"]]
            return None, None, _err(f"A {class_slug} cannot cast spells. Give them `powers` instead, or pick a caster class {casters}.")
        slugs, names, cantrips, leveled = [], {}, 0, 0
        for pick in picks:
            slug = str(pick.get("spell", "")).strip().lower()
            sp = self.spells.get(slug)
            if sp is None:
                return None, None, _err(f"'{slug}' is not a verified spell. Browse with find_spells.")
            if slug in slugs:
                continue
            if sp["level"] > max_slot:
                return None, None, _err(f"{sp['name']} is level {sp['level']}; this companion only has level {max_slot} slots.")
            cantrips += sp["level"] == 0
            leveled += sp["level"] > 0
            slugs.append(slug)
            names[slug] = " ".join(str(pick.get("name") or sp["name"]).split())[:30]
        if cantrips > MAX_COMPANION_CANTRIPS or leveled > 4 + max_slot:
            return None, None, _err(f"Too many spells: at most {MAX_COMPANION_CANTRIPS} cantrips and {4 + max_slot} leveled spells.")
        return slugs, names, None

    def party(self, action: str, name: str, class_slug: str = "", species_slug: str = "human", title: str = "",
              spells: Optional[list] = None, powers: Optional[list] = None) -> dict:
        """Companion roster. ONLY 'active' members are placed in combat; nobody can join mid-fight."""
        if self.pc is None:
            return _err("No character exists yet.")
        if self.handle:
            return _err("The party can only change outside combat. Set it BEFORE start_encounter; nobody can join mid-fight.")
        name = " ".join(str(name).split())[:40]
        if not name:
            return _err("A companion needs a name.")
        active = sum(1 for m in self.roster.values() if m["status"] == "active")
        cap = self.cfg.max_active_companions
        member = self._find_member(name)
        if action == "recruit":
            if member:
                return _err(f"{member['name']} is already in the roster ({member['status']}). Use activate or deactivate.")
            if class_slug not in COMPANION_PRESETS:
                return _err(f"Companion class must be one of {list(COMPANION_PRESETS)}.")
            if species_slug not in COMPANION_SPECIES:
                return _err(f"Companion species must be one of {list(COMPANION_SPECIES)}.")
            if len(self.roster) >= self.cfg.max_roster:
                return _err(f"The roster is full ({self.cfg.max_roster}). Dismiss someone first.")
            preset = COMPANION_PRESETS[class_slug]
            base = "ally:" + (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "companion")
            mid, n = base, 2
            while mid in self.roster:
                mid, n = f"{base}-{n}", n + 1
            m = {"id": mid, "name": name, "class_slug": class_slug, "species_slug": species_slug,
                 "title": " ".join(str(title).split())[:40], "level": int(self.pc_params.get("level", 1)),
                 "ability_scores": dict(preset["scores"]), "equipment": list(preset["equipment"]),
                 "status": "inactive", "hp_current": None, "hp_max": 1, "spells": [], "spell_names": {}, "powers": []}
            try:
                sheet = E.derive_sheet(self._companion_spec(m), loader=self.loader)
                m["hp_max"] = sheet.hp_max
            except Exception as ex:
                return _err(f"Could not build that companion: {type(ex).__name__}: {str(ex)[:160]}")
            max_slot = max(list(sheet.spell_slots) or [0])
            slugs, names, bad = self._pick_spells(class_slug, spells or [], max_slot)
            if bad:
                return bad
            clean_powers, spent = [], 0
            if len(powers or []) > 2:
                return _err("At most 2 powers per companion.")
            for i, raw in enumerate(powers or [], start=1):
                p, bad = self._clean_power(raw)
                if bad:
                    return bad
                spent += self._power_cost(p)
                clean_powers.append({**p, "slug": f"{mid}-p{i}"})
            if spent > MAX_COMPANION_POWER_POINTS:
                return _err(f"Powers too strong: {spent} points > {MAX_COMPANION_POWER_POINTS} (power = ceil(avg dice/2) + 2 per charge).")
            m.update(spells=slugs, spell_names=names, powers=clean_powers)
            for p in clean_powers:
                self._register_power({"slug": p["slug"], "power": p})
            m["status"] = "active" if active < cap else "inactive"
            self.roster[mid] = m
            note = "" if m["status"] == "active" else f"The active party is full ({cap}); they wait as inactive."
            if COMPANION_PRESETS[class_slug]["cast"] and not slugs and not clean_powers:
                note = (note + " No spells chosen: they will fight with a weapon.").strip()
            return {"ok": True, "action": "recruit", "member": name, "status": m["status"], "note": note,
                    "party": self._roster_view()}
        if member is None:
            return _err(f"No companion named '{name}'.", roster=[m["name"] for m in self.roster.values()])
        if member["status"] == "dead":
            return _err(f"{member['name']} is dead and cannot rejoin.")
        if action == "activate":
            if member["status"] != "active" and active >= cap:
                return _err(f"The active party is full ({cap}). Deactivate someone first.", party=self._roster_view())
            member["status"] = "active"
        elif action == "deactivate":
            member["status"] = "inactive"
        elif action == "dismiss":
            for p in member.get("powers", []):
                self.loader.extra.pop(p["slug"] + "-power", None)
                self.loader.kinds.pop(p["slug"] + "-power", None)
                self.power_uses.pop(p["slug"], None)
            del self.roster[member["id"]]
        else:
            return _err(f"Unknown action '{action}'.")
        return {"ok": True, "action": action, "member": member["name"],
                "status": member["status"] if action != "dismiss" else "left", "party": self._roster_view()}

    def _ally_weapon(self, ally_id: str) -> str:
        m = self.roster.get(ally_id, {})
        return next((w for w in m.get("equipment", []) if self._is_weapon(w)), UNARMED)

    @staticmethod
    def _dist(v, a: str, b: str) -> int:
        (ax, ay), (bx, by) = _cell(v.actor_zone[a]), _cell(v.actor_zone[b])
        return 5 * max(abs(ax - bx), abs(ay - by))

    def _act(self, actor: str, intent) -> Optional[list[str]]:
        """Submit an intent for an ally. Engine rejections return None instead of raising."""
        try:
            self._run(E.submit_player_intent(self.handle, actor, intent))
        except Exception:
            E.drain_pending_events(self.handle)
            return None
        return self._digest(E.drain_pending_events(self.handle))

    def _slot_for(self, v, actor: str, spell_level: int) -> Optional[int]:
        if spell_level == 0:
            return 0
        slots = v.spell_slots_by_entity.get(actor, {})
        return next((lv for lv in sorted(slots) if lv >= spell_level and slots[lv] > 0), None)

    def _still_my_turn(self, actor: str) -> bool:
        v = E.get_live(self.handle)
        return not self._combat_over(v) and v.initiative[v.current_turn_index].entity_id == actor

    def _reach(self, actor: str, target: str, reach_ft: int, events: list) -> bool:
        """Walk `actor` until `target` is within reach (adjacent for short-reach spells). True if in reach afterwards."""
        v = E.get_live(self.handle)
        if self._dist(v, actor, target) <= reach_ft:
            return True
        dest = self._approach_dest(v, actor, target, stop_ft=5 if reach_ft <= 15 else reach_ft)
        if dest:
            events += self._act(actor, E.PlayerIntent(intent_type="move", target_zone_id=dest)) or []
        if not self._still_my_turn(actor):
            return False
        v = E.get_live(self.handle)
        return target in v.actor_zone and self._dist(v, actor, target) <= max(reach_ft, 5)

    def _ally_heal(self, ally: str, m: dict, v, events: list) -> bool:
        heals = [sp for sp in m.get("spells", []) if self.spells.get(sp, {}).get("heal")]
        if not heals:
            return False
        hurt = []
        for pid in v.party_ids - v.dead_ids:
            hp = v.tracked_hp.get(pid, 0)
            top = self.pc.hp_max if pid == PC_ID else self.roster.get(pid, {}).get("hp_max", hp or 1)
            if hp / max(top, 1) <= 0.5:
                hurt.append((hp / max(top, 1), pid))
        if not hurt:
            return False
        _, tid = min(hurt)
        for sp in sorted(heals, key=lambda x: self.spells[x]["level"]):
            info = self.spells[sp]
            lvl = self._slot_for(v, ally, info["level"])
            if lvl is None:
                continue
            if not self._reach(ally, tid, max(info["reach_ft"], 5), events):
                continue
            ev = self._act(ally, E.PlayerIntent(intent_type="cast_spell", spell_id=sp, target_id=tid, slot_level=lvl or None))
            if ev is not None:
                events += ev
                return True
        return False

    def _ally_power(self, ally: str, m: dict, v, events: list) -> bool:
        foes = sorted(v.encounter_ids - v.dead_ids, key=lambda f: (self._dist(v, ally, f), f))
        for p in m.get("powers", []):
            pw = self.power_uses.get(p["slug"])
            if not foes or not pw or pw["left"] <= 0:
                continue
            tgt = foes[0]
            if not self._reach(ally, tgt, POWER_RANGE_FT, events):
                continue
            ev = self._act(ally, E.PlayerIntent(intent_type="use_item", item_id=p["slug"] + "-power",
                                                activity_id=self._rid(p["slug"]), target_id=tgt))
            if ev is not None:
                if any(" makes a " in e and " save " in e and not e.startswith(m["name"] + " ") for e in ev):
                    pw["left"] -= 1
                events += [f"{m['name']} unleashes {p['name']}"] + ev
                return True
        return False

    @staticmethod
    def _parse_shape(shape: str) -> tuple[str, int]:
        m = re.match(r"(\w+)\s*(\d+)", shape or "")
        return (m.group(1), int(m.group(2))) if m else ("", 0)

    def _friendly_fire(self, v, caster: str, target: str, info: dict) -> bool:
        """Would this area spell catch the player or another party member? (Cones/lines: along the aim; others: around the target.)"""
        kind, size = self._parse_shape(info.get("shape", ""))
        if not kind:
            return False
        (cx, cy), (tx, ty) = _cell(v.actor_zone[caster]), _cell(v.actor_zone[target])
        cells = max(1, size // 5)
        for pid in v.party_ids - v.dead_ids - {caster}:
            ax, ay = _cell(v.actor_zone[pid])
            if kind in ("cone", "line"):
                va, vt = (ax - cx, ay - cy), (tx - cx, ty - cy)
                la, lt = math.hypot(*va), math.hypot(*vt)
                if la and lt and la <= cells + 0.5 and (va[0] * vt[0] + va[1] * vt[1]) / (la * lt) >= 0.7:
                    return True
            elif math.hypot(ax - tx, ay - ty) <= cells + 0.5:
                return True
        return False

    def _ally_cast(self, ally: str, m: dict, v, events: list) -> bool:
        foes = sorted(v.encounter_ids - v.dead_ids, key=lambda f: (self._dist(v, ally, f), f))
        if not foes or not m.get("spells"):
            return False
        tgt, options = foes[0], []
        for sp in m["spells"]:
            info = self.spells.get(sp)
            if not info or (info["heal"] and not info["damage_types"]):
                continue
            lvl = self._slot_for(v, ally, info["level"])
            if lvl is None:
                continue
            area_bonus = 5 if (info["shape"] and len(foes) >= 2) else 0
            options.append((info["level"] * 10 + area_bonus, sp, lvl))
        for _, sp, lvl in sorted(options, reverse=True):          # strongest spell first, cantrips last
            info = self.spells[sp]
            if not self._reach(ally, tgt, max(info["reach_ft"], 5), events):
                if not self._still_my_turn(ally):
                    return True
                continue
            if self._friendly_fire(E.get_live(self.handle), ally, tgt, info):
                continue                                         # never fry the player: pick a different spell
            ev = self._act(ally, E.PlayerIntent(intent_type="cast_spell", spell_id=sp, target_id=tgt, slot_level=lvl or None))
            if ev is not None:
                events += ev
                return True
        return False

    def _ally_turn(self, ally: str) -> list[str]:
        """Companion AI: heal the hurt, then a power, then the strongest spell, else a weapon (walking up if needed)."""
        events: list[str] = []
        m = self.roster.get(ally, {})
        v = E.get_live(self.handle)
        acted = (self._ally_heal(ally, m, v, events) or self._ally_power(ally, m, E.get_live(self.handle), events)
                 or self._ally_cast(ally, m, E.get_live(self.handle), events)) if self._still_my_turn(ally) else True
        v = E.get_live(self.handle)
        foes = sorted(v.encounter_ids - v.dead_ids, key=lambda f: (self._dist(v, ally, f), f))
        if not acted and foes:
            tgt = foes[0]
            attack = E.PlayerIntent(intent_type="attack", target_id=tgt, weapon_id=self._ally_weapon(ally))
            self._oor_actors.discard(ally)
            ev = self._act(ally, attack)
            events += ev or []
            if ev is None or ally in self._oor_actors:
                events = [e for e in events if "could not be made" not in e]
                v = E.get_live(self.handle)
                dest = self._approach_dest(v, ally, tgt)
                if dest:
                    events += self._act(ally, E.PlayerIntent(intent_type="move", target_zone_id=dest)) or []
                    v = E.get_live(self.handle)
                    if not self._combat_over(v) and tgt not in v.dead_ids and v.initiative[v.current_turn_index].entity_id == ally:
                        events += self._act(ally, attack) or []
        if self._still_my_turn(ally):
            events += self._act(ally, E.PlayerIntent(intent_type="pass")) or []
        return events

    # ------------------------------------------------------------------ player-facing report (/scene)
    def _item_label(self, slug: str) -> str:
        spec = next((x for x in self.custom_weapons + self.custom_armor if x["slug"] == slug), None)
        return spec["name"] if spec else slug.replace("-", " ")

    def _member_lines(self, m: dict, v=None) -> list[str]:
        short = {"strength": "STR", "dexterity": "DEX", "constitution": "CON", "intelligence": "INT", "wisdom": "WIS", "charisma": "CHA"}
        try:
            msh = E.derive_sheet(self._companion_spec(m), loader=self.loader)
            ac = msh.ac
            scores = " ".join(f"{short[k]} {val}" for k, val in msh.ability_scores.model_dump().items() if k in short)
            slots = dict(msh.spell_slots)
        except Exception:
            ac, scores, slots = "?", "", {}
        cur = m["hp_max"] if m["hp_current"] is None else m["hp_current"]
        if v and m["id"] in v.tracked_hp:
            cur = v.tracked_hp[m["id"]]
        tag = {"active": "active  ", "inactive": "inactive", "dead": "DEAD    "}[m["status"]]
        title = f" ({m['title']})" if m.get("title") else ""
        out = [f"  [{tag}] {m['name']}{title} - {m['class_slug'].title()} L{m['level']}   HP {cur}/{m['hp_max']}   AC {ac}",
               "      " + scores,
               "      Weapons: " + (", ".join(self._item_label(w) for w in m["equipment"] if self._is_weapon(w)) or "unarmed")]
        if m.get("spells"):
            out.append("      Spells: " + ", ".join(self._spell_label(m["id"], sp) for sp in m["spells"]) + f"   slots {slots}")
        if m.get("powers"):
            out.append("      Powers: " + ", ".join(
                f"{p['name']} ({p['damage_dice']} {p['damage_type']}, {p['ability'].upper()} save) "
                f"{self.power_uses.get(p['slug'], {}).get('left', '?')}/{p['charges']}" for p in m["powers"]))
        return out

    def member_report(self, name: str) -> Optional[str]:
        m = self._find_member(name)
        return "\n".join(self._member_lines(m, E.get_live(self.handle) if self.handle else None)) if m else None

    def report(self) -> str:
        """Detailed stats for the PLAYER. The DM's own per-turn scene stays compact; none of this is sent to the model."""
        if self.pc is None:
            return "(no character yet)"
        short = {"strength": "STR", "dexterity": "DEX", "constitution": "CON", "intelligence": "INT", "wisdom": "WIS", "charisma": "CHA"}
        abil = lambda d: " ".join(f"{short.get(k, k.upper())} {val}" for k, val in d.items() if k in short or k in short.values())  # noqa: E731
        v = E.get_live(self.handle) if self.handle else None
        pc, sh = self.pc, self.sheet
        hp = v.tracked_hp.get(PC_ID, pc.hp_current) if v else pc.hp_current
        cls = self.pc_params.get("class_slug", "?").title()
        out = [f"{pc.name} - {cls} L{self.pc_params.get('level', 1)}   HP {hp}/{pc.hp_max}   AC {pc.ac}   "
               f"Speed {sh.base_speed} ft   Prof +{sh.proficiency_bonus}   {self.gold} gp",
               "  " + abil(sh.ability_scores.model_dump()),
               "  Weapons: " + ", ".join(self._item_label(w) for w in self._weapons()),
               "  Armor: " + (", ".join(self._item_label(a) for a in pc.equipment if self._armor_kind(a)) or "none")]
        if pc.damage_resistances:
            out.append("  Resistances: " + ", ".join(pc.damage_resistances))
        pw_pc = [f"{pw['name']} {pw['left']}/{pw['max']}" for slug, pw in self.power_uses.items() if slug in pc.equipment]
        if pw_pc:
            out.append("  Powers: " + ", ".join(pw_pc))
        if pc.spells_known:
            out.append("  Spells: " + ", ".join(self._spell_label(PC_ID, s) for s in pc.spells_known) + f"   slots {dict(pc.spell_slots)}")
        if self.roster:
            out.append("Party:")
            for m in self.roster.values():
                out += self._member_lines(m, v)
        if v:
            out.append(f"Combat - round {v.round_number}, turn: {self.names.get(v.initiative[v.current_turn_index].entity_id, '?')}")
            for e in sorted(v.encounter_ids):
                hp_e = v.tracked_hp.get(e, 0)
                mx = self.enemy_hp_max.get(e, hp_e or 1)
                state = "dead" if e in v.dead_ids else _status(hp_e, mx)
                exact = f"  HP {hp_e}/{mx}" if self.cfg.show_enemy_stats else ""
                out.append(f"  enemy {self.names.get(e, e)}: {state}, {self._dist(v, PC_ID, e)} ft away{exact}")
        return "\n".join(out)

    # ------------------------------------------------------------------ snapshots
    def snapshot(self) -> dict:
        pc = self.pc
        if pc is None:
            return {"in_combat": False, "pc": None}
        weapons = self._weapons()
        base = {
            "in_combat": self.handle is not None,
            "pc": {
                "id": PC_ID, "name": pc.name, "hp": pc.hp_current, "hp_max": pc.hp_max, "ac": pc.ac,
                "weapons": weapons, "spells_known": list(pc.spells_known), "spell_slots": dict(pc.spell_slots),
            },
        }
        if self.inventory:
            base["pc"]["inventory"] = [i["name"] for i in self.inventory[-15:]]
        if self.roster and not self.handle:
            base["pc"]["party"] = self._roster_view()
        powers = []
        for slug, pw in self.power_uses.items():
            spec = next((w for w in self.custom_weapons + self.custom_armor if w["slug"] == slug), None)
            if spec and slug in pc.equipment:
                p = spec["power"]
                powers.append({"weapon_id": slug, "name": pw["name"], "charges": f"{pw['left']}/{pw['max']}",
                               "effect": f"{p['damage_dice']} {p['damage_type']}, {p['ability'].upper()} save, {POWER_RANGE_FT} ft"})
        if powers:
            base["pc"]["powers"] = powers
        base["pc"]["armor"] = [e for e in pc.equipment if self._armor_kind(e)]
        if pc.damage_resistances:
            base["pc"]["resistances"] = list(pc.damage_resistances)
        base["pc"]["gold"] = self.gold
        if self.boss_rewards:
            base["pc"]["unspent_boss_rewards"] = self.boss_rewards
        if not self.handle:
            return base
        v = E.get_live(self.handle)
        cur = v.initiative[v.current_turn_index]
        pc_hp = v.tracked_hp.get(PC_ID, pc.hp_current)
        base["pc"]["hp"] = pc_hp
        base["pc"]["conditions"] = sorted(v.active_conditions.get(PC_ID, []))
        pc_cell = _cell(v.actor_zone[PC_ID])
        pc_c = next((c for c in v.initiative if c.entity_id == PC_ID), None)
        if pc_c is not None:
            base["pc"]["movement_left_ft"] = pc_c.movement_remaining
            base["pc"]["action_available"] = pc_c.action_available
        enemies = []
        for eid in sorted(v.encounter_ids):
            hp = v.tracked_hp.get(eid, 0)
            ec = _cell(v.actor_zone[eid])
            dist = 5 * max(abs(ec[0] - pc_cell[0]), abs(ec[1] - pc_cell[1]))
            enemies.append({
                "id": eid, "name": self.names.get(eid, eid),
                "status": "dead" if eid in v.dead_ids else _status(hp, self.enemy_hp_max.get(eid, hp or 1)),
                "distance_ft": dist, "conditions": sorted(v.active_conditions.get(eid, [])),
            })
        allies = []
        for aid in sorted(v.party_ids - {PC_ID}):
            m = self.roster.get(aid)
            hp = v.tracked_hp.get(aid, 0)
            allies.append({"id": aid, "name": self.names.get(aid, aid),
                           "status": "down" if aid in v.dead_ids else _status(hp, m["hp_max"] if m else (hp or 1)),
                           "distance_ft": self._dist(v, PC_ID, aid), "conditions": sorted(v.active_conditions.get(aid, []))})
        base.update(round=v.round_number, current_actor=cur.entity_id,
                    your_turn=(cur.entity_id == PC_ID), enemies=enemies, allies=allies)
        return base

    # ------------------------------------------------------------------ event digest
    def _digest(self, events: list) -> list[str]:
        """Turn typed engine events into short plain-English lines the model can narrate."""
        out: list[str] = []
        last_move: Optional[tuple[str, int]] = None  # (actor, index in out)
        for ev in events:
            t = ev.type
            if t in SKIP_EVENTS:
                continue
            d = ev.model_dump()
            if t == "actor_moved":
                actor = d.get("actor_id")
                if last_move and last_move[0] == actor:
                    # merge consecutive steps into one line
                    first = out[last_move[1]].split(" from ", 1)[1].split(" to ")[0]
                    out[last_move[1]] = f"{self._n(actor)} moves from {first} to {d.get('to_zone')}"
                else:
                    out.append(f"{self._n(actor)} moves from {d.get('from_zone')} to {d.get('to_zone')}")
                    last_move = (actor, len(out) - 1)
                continue
            last_move = None
            if t == "attack_rolled":
                tag = "CRITICAL HIT" if d.get("is_crit") else ("HIT" if d.get("is_hit") else "MISS")
                opp = " (opportunity attack)" if d.get("is_opportunity_attack") else ""
                out.append(f"{self._n(d['attacker_id'])} attacks {self._n(d['target_id'])}{opp}: {tag}")
            elif t == "damage_applied":
                kill = " (killing blow)" if d.get("is_overkill") else ""
                out.append(f"{self._n(d['target_id'])} takes {d['amount']} {d.get('damage_type', '')} damage{kill}")
            elif t == "spell_cast":
                out.append(f"{self._n(d['actor_id'])} casts {self._spell_label(d['actor_id'], d['spell_id'])}")
            elif t == "save_rolled":
                ok = bool(d.get("succeeded"))
                out.append(f"{self._n(d['target_id'])} makes a {str(d.get('ability', '')).upper()} save "
                           f"(rolled {d.get('roll_total')} vs DC {d.get('dc')}): {'PASSES' if ok else 'FAILS'}")
            elif t == "attack_failed":
                if d.get("reason") == "out_of_range":
                    self._oor_actors.add(d.get("actor_id"))
                    if d.get("actor_id") == PC_ID:
                        self._saw_out_of_range = True
                why = ATTACK_FAIL_WHY.get(d.get("reason"), d.get("reason"))
                out.append(f"{self._n(d['actor_id'])}'s attack could not be made ({why}). This is NOT a miss")
            elif t == "death":
                out.append(f"{self._n(d['target_id'])} dies")
            else:
                parts = []
                for k, val in d.items():
                    if k == "type" or val in (None, "", [], {}, False):
                        continue
                    parts.append(f"{k}={self._n(val)}")
                out.append(f"{t.replace('_', ' ')}: " + ", ".join(parts))
        return out

    # ------------------------------------------------------------------ combat flow
    def start_encounter(self, templates: list[str], custom: Optional[list[dict]] = None,
                        tracked: Optional[list[dict]] = None) -> dict:
        custom, tracked = custom or [], tracked or []
        if self.handle:
            return _err("A combat is already in progress.")
        if self.pc is None:
            return _err("No character exists yet.")
        if self.pc.hp_current <= 0:
            return _err("The character is down. They must rest first.")
        if not 1 <= len(templates) + len(custom) + len(tracked) <= self.cfg.max_monsters:
            return _err(f"Between 1 and {self.cfg.max_monsters} monsters allowed in total.")

        monsters, total_cr = [], 0.0
        for slug in templates:
            try:
                m = self.loader.get_monster(slug)
            except Exception:
                return _err(f"Unknown monster template '{slug}'.", hint=self._monster_hint())
            total_cr += _cr(m.cr)
            monsters.append((slug, m))
        if total_cr > self.cfg.max_encounter_cr:
            return _err(f"Encounter too strong (total CR {total_cr:g} > limit {self.cfg.max_encounter_cr:g}).",
                        hint=self._monster_hint())

        cleaned, threat = [], 0.0
        for raw in custom:
            c, problem = _clean_custom(raw)
            if problem:
                return _err(problem)
            cleaned.append(c)
            threat += c["threat"]
        cap = self.cfg.max_custom_threat * int(self.pc_params.get("level", 1))
        if threat > cap:
            return _err(f"Invented creatures too strong (threat {threat:.0f} > budget {cap}; "
                        "threat = HP x average damage per hit, summed). Lower their HP or damage.")

        tracked_clean = []
        for t in tracked:                                       # stored NPC stat blocks: validated at creation, capped for bosses
            c, problem = _clean_custom({"name": t["name"], "hp": t["hp_max"], "ac": t["ac"], "attack_bonus": t["attack_bonus"],
                                        "damage_dice": t["damage_dice"], "damage_type": t["damage_type"],
                                        "dexterity": t.get("dexterity", 10)}, boss=True)
            if problem:
                return _err(problem)
            if c["threat"] > self.cfg.max_boss_threat:
                return _err(f"{c['name']}'s stored stat block is over the boss cap (threat {c['threat']:.0f} > {self.cfg.max_boss_threat}).")
            c["hp_now"] = max(1, min(int(t.get("hp_start", c["hp"])), c["hp"]))
            tracked_clean.append(c)
        self._boss_fight = (any(_cr(m.cr) >= 1 for _, m in monsters) or any(c["threat"] >= 250 for c in cleaned)
                            or bool(tracked_clean))
        entries = [dict(name=m.name, hp=m.hp, ac=m.ac, dex=m.ability_scores.dex, slug=slug,
                        xp=CR_XP.get(_cr(m.cr), 0), extra={}) for slug, m in monsters]
        entries += [dict(name=c["name"], hp=c["hp"], ac=c["ac"], dex=c["dexterity"], slug=None,
                         xp=min(450, int(c["threat"] / 4)),
                         extra=dict(attack_bonus=c["attack_bonus"], damage_dice=c["damage_dice"],
                                    damage_type=c["damage_type"])) for c in cleaned]
        entries += [dict(name=c["name"], hp=c["hp"], hp_now=c["hp_now"], ac=c["ac"], dex=c["dexterity"], slug=None,
                         xp=min(900, int(c["threat"] / 4)),
                         extra=dict(attack_bonus=c["attack_bonus"], damage_dice=c["damage_dice"], damage_type=c["damage_type"]))
                    for c in tracked_clean]
        totals: dict[str, int] = {}
        for e in entries:
            totals[e["name"]] = totals.get(e["name"], 0) + 1

        self._encounters += 1
        self.names = {PC_ID: self.pc.name}
        self.enemy_hp_max = {}
        encounter, seen = [], {}
        for i, e in enumerate(entries, start=1):
            eid = f"mon:{i}"
            seen[e["name"]] = seen.get(e["name"], 0) + 1
            label = e["name"] if totals[e["name"]] == 1 else f"{e['name']} {seen[e['name']]}"
            self.names[eid] = label
            self.enemy_hp_max[eid] = e["hp"]
            col = self.cfg.grid_width - 2 - ((i - 1) // self.cfg.grid_height)
            row = (i - 1) % self.cfg.grid_height
            encounter.append(E.EncounterMemberSpec(
                entity_id=eid, entity_type="Monster", name=label, initiative=None,
                hp_current=e.get("hp_now", e["hp"]), hp_max=e["hp"], ac=e["ac"], zone_id=f"{col},{row}",
                monster_template_slug=e["slug"], xp_value=e["xp"], dexterity=e["dex"], **e["extra"]))
        pc = self.pc.model_copy(update={"zone_id": PC_START_ZONE, "initiative": None})
        allies = []
        for m in [m for m in self.roster.values() if m["status"] == "active" and (m["hp_current"] is None or m["hp_current"] > 0)]:
            if len(allies) >= len(ALLY_ZONES):
                break
            allies.append(self._build_companion(m).model_copy(update={"zone_id": ALLY_ZONES[len(allies)], "initiative": None}))
            self.names[m["id"]] = m["name"]
        res = self._run(E.start_combat(
            session_id=f"enc{self._encounters}", party=[pc, *allies], encounter=encounter,
            grid_scene=E.GridScene(width=self.cfg.grid_width, height=self.cfg.grid_height),
            rng_seed=self.rng.randrange(1 << 31)))
        self.handle = res.handle
        events = self._digest(res.events) + self._settle()
        return self._result(events)

    def submit_action(self, action: str, target_id: str = "", weapon_id: str = "",
                      spell_id: str = "", slot_level: int = 0) -> dict:
        if not self.handle:
            return _err("Not in combat.")
        v = E.get_live(self.handle)
        if v.initiative[v.current_turn_index].entity_id != PC_ID:
            return _err("It is not the player's turn.")
        if action == "move_toward":
            tid, bad = self._resolve_target(target_id, v)
            return bad or self._move_toward(tid)
        if action == "use_power":
            return self._use_power(target_id, weapon_id)
        if action == "flee":
            return self._flee()
        kind = ACTION_MAP.get(action)
        if kind is None:
            return _err(f"Unknown action '{action}'. Allowed: {sorted(ACTION_MAP) + ['move_toward', 'use_power', 'flee']}")
        kw: dict = {}
        tid = ""
        if action in ("attack", "cast_spell", "help") and (target_id or action == "attack"):
            tid, bad = self._resolve_target(target_id, v)
            if bad:
                return bad
        if action == "attack":
            weapon = self._resolve_weapon(weapon_id)
            if weapon is None:
                owned = ", ".join(w.replace("-", " ") for w in self._weapons())
                return _err(f"Invalid weapon '{weapon_id}'.", valid_weapons=self._weapons(), requested=weapon_id,
                            auto_reply=f"You don't have a '{weapon_id}'. You can attack with: {owned}.")
            intent = E.PlayerIntent(intent_type="attack", target_id=tid, weapon_id=weapon)
            return self._attack_with_approach(intent, tid)
        if action == "cast_spell":
            if spell_id not in self.pc.spells_known:
                known = ", ".join(self.pc.spells_known) or "none"
                return _err(f"Unknown spell '{spell_id}'.", spells_known=list(self.pc.spells_known), requested=spell_id,
                            auto_reply=f"You don't know a spell called '{spell_id}'. Spells you know: {known}.")
            kw = dict(spell_id=spell_id, target_id=tid or None, slot_level=slot_level or None)
        elif action == "help" and tid:
            kw = dict(target_id=tid)
        return self._submit(E.PlayerIntent(intent_type=kind, **kw))

    def _use_power(self, target_raw: str, weapon_raw: str) -> dict:
        """Fire a weapon's signature power. The engine rolls the save and damage; the adapter enforces charges."""
        v = E.get_live(self.handle)
        owned = {sl: pw for sl, pw in self.power_uses.items() if sl in self.pc.equipment}
        key = (weapon_raw or "").strip().lower().replace(" ", "-")
        slug = key if key in owned else next(
            (sl for sl, pw in owned.items() if key and (key in sl or key == pw["name"].lower().replace(" ", "-"))), None)
        if slug is None and len(owned) == 1:
            slug = next(iter(owned))
        if slug is None:
            return _err("No such weapon power.", powers=list(owned), requested=weapon_raw,
                        auto_reply="None of your weapons has a power like that." if not owned else
                        "You can use: " + ", ".join(f"{pw['name']} ({sl})" for sl, pw in owned.items()) + ".")
        pw = owned[slug]
        if pw["left"] <= 0:
            return _err(f"{pw['name']} has no charges left.", auto_always=True,
                        auto_reply=f"{pw['name']} is spent until you rest.")
        tid, bad = self._resolve_target(target_raw, v)
        if bad:
            return bad
        me, foe = _cell(v.actor_zone[PC_ID]), _cell(v.actor_zone[tid])
        dist = 5 * max(abs(me[0] - foe[0]), abs(me[1] - foe[1]))
        if dist > POWER_RANGE_FT:
            return _err(f"Target is {dist} ft away; {pw['name']} reaches {POWER_RANGE_FT} ft. Use move_toward first.")
        before_left = pw["left"]
        res = self._submit(E.PlayerIntent(intent_type="use_item", item_id=slug + "-power",
                                          activity_id=self._rid(slug), target_id=tid))
        pc_name = self._n(PC_ID)
        if res.get("ok") and any(" makes a " in e and " save " in e and not e.startswith(pc_name + " ")
                                 for e in res.get("events", [])):
            pw["left"] -= 1                      # spend a charge only if the power actually resolved
        res["power_charges_left"] = f"{pw['left']}/{pw['max']}"
        if res.get("ok") and pw["left"] < before_left:
            res["events"] = [f"{pc_name} uses {pw['name']}"] + res.get("events", [])
        return res

    def _flee(self) -> dict:
        """The player breaks off the fight. Survivors keep their wounds; nobody earns XP or loot."""
        name = self._n(PC_ID)
        return {"ok": True, "events": [f"{name} breaks away and flees the fight."], **self._finish(fled=True)}

    def _attack_with_approach(self, intent, tid: str) -> dict:
        """Attack; if the target is out of reach, walk up to it and retry once (what 'I punch him' means)."""
        self._saw_out_of_range = False
        res = self._submit(intent)
        if not (res.get("ok") and self._saw_out_of_range) or res.get("combat_over"):
            return res
        carried = [e for e in res["events"] if "could not be made" not in e]
        mv = self._move_toward(tid)
        if not mv.get("ok"):
            return {**_err("Target is out of reach and the character cannot get closer this turn: " + mv.get("error", "")),
                    "events": carried}
        carried += mv["events"]
        if mv.get("combat_over"):
            return {**mv, "events": carried}
        self._saw_out_of_range = False
        res2 = self._submit(intent)
        if res2.get("ok"):
            res2["events"] = carried + res2["events"]
            if self._saw_out_of_range:
                res2["events"].append("Even after moving, the target is still out of reach this turn. This is NOT a miss.")
        return res2

    def _approach_dest(self, v, actor: str, target_id: str, stop_ft: int = 5) -> Optional[str]:
        """The cell `actor` can reach this turn that gets closest to the target (None if it cannot get closer)."""
        me = next(c for c in v.initiative if c.entity_id == actor)
        steps = int(me.movement_remaining // 5)
        cur, goal = _cell(v.actor_zone[actor]), _cell(v.actor_zone[target_id])
        occupied = {_cell(z) for k, z in v.actor_zone.items() if k != actor and k not in v.dead_ids}
        sign = lambda x: (x > 0) - (x < 0)  # noqa: E731
        for _ in range(steps):
            if 5 * max(abs(goal[0] - cur[0]), abs(goal[1] - cur[1])) <= stop_ft:
                break
            sx, sy = sign(goal[0] - cur[0]), sign(goal[1] - cur[1])
            for nxt in ((cur[0] + sx, cur[1] + sy), (cur[0] + sx, cur[1]), (cur[0], cur[1] + sy)):
                inside = 0 <= nxt[0] < self.cfg.grid_width and 0 <= nxt[1] < self.cfg.grid_height
                if nxt != cur and inside and nxt not in occupied:
                    cur = nxt
                    break
            else:
                break
        dest = f"{cur[0]},{cur[1]}"
        return None if dest == v.actor_zone[actor] else dest

    def _move_toward(self, target_id: str) -> dict:
        v = E.get_live(self.handle)
        if target_id not in v.encounter_ids or target_id in v.dead_ids:
            return _err(f"Invalid target '{target_id}'.", valid_targets=sorted(v.encounter_ids - v.dead_ids))
        dest = self._approach_dest(v, PC_ID, target_id)
        if dest is None:
            return _err("Cannot get closer (already adjacent, out of movement, or blocked).")
        return self._submit(E.PlayerIntent(intent_type="move", target_zone_id=dest))

    def _submit(self, intent) -> dict:
        try:
            self._run(E.submit_player_intent(self.handle, PC_ID, intent))
        except Exception as ex:  # engine rejections come back as data so the model can adjust
            E.drain_pending_events(self.handle)
            return _err(f"{type(ex).__name__}: {ex}"[:300])
        events = self._digest(E.drain_pending_events(self.handle)) + self._settle()
        return self._result(events)

    @staticmethod
    def _combat_over(v) -> bool:
        """The engine does not flip `ended` on its own when a side is wiped out; the host must notice."""
        return bool(v.ended or v.encounter_ids <= v.dead_ids or PC_ID in v.dead_ids or v.party_ids <= v.dead_ids)

    def _incapacitated(self, v, eid: str) -> bool:
        return v.tracked_hp.get(eid, 1) <= 0 or "unconscious" in v.active_conditions.get(eid, ())

    def _settle(self) -> list[str]:
        """Run enemy and companion turns (and auto-pass a downed PC) until it is the PC's turn or combat ends."""
        events: list[str] = []
        for _ in range(80):
            v = E.get_live(self.handle)
            if self._combat_over(v):
                break
            cur = v.initiative[v.current_turn_index].entity_id
            if cur in v.party_ids:
                if self._incapacitated(v, cur):
                    self._run(E.submit_player_intent(self.handle, cur, E.PlayerIntent(intent_type="pass")))
                    events += self._digest(E.drain_pending_events(self.handle))
                elif cur == PC_ID:
                    break
                else:
                    events += self._ally_turn(cur)
                    v2 = E.get_live(self.handle)
                    if not self._combat_over(v2) and v2.initiative[v2.current_turn_index].entity_id == cur:
                        break                       # a companion that cannot end its turn must not trap us in a loop
            else:
                self._run(E.advance_monster_turn(self.handle))
                events += self._digest(E.drain_pending_events(self.handle))
        return events

    def _result(self, events: list[str]) -> dict:
        v = E.get_live(self.handle)
        out: dict = {"ok": True, "events": events}
        if self._combat_over(v):
            out.update(self._finish())
        else:
            out["scene"] = self.snapshot()
        return out

    def _finish(self, fled: bool = False) -> dict:
        v = E.get_live(self.handle)
        fallen = [self.names.get(e, e) for e in sorted(v.dead_ids & v.encounter_ids)]
        foe_hp = {self.names.get(e, e): int(v.tracked_hp.get(e, 0)) for e in sorted(v.encounter_ids)}
        pc_dead, foes_down = PC_ID in v.dead_ids, v.encounter_ids <= v.dead_ids
        res = self._run(E.end_combat(self.handle))
        o = res.outcome
        hp = 0 if pc_dead else o.residual_hp.get(PC_ID, self.pc.hp_current)
        self.pc = self.pc.model_copy(update={"hp_current": hp})
        fallen_allies = []
        for aid in sorted(v.party_ids - {PC_ID}):
            m = self.roster.get(aid)
            if m is None:
                continue
            if aid in v.dead_ids:
                m["status"], m["hp_current"] = "dead", 0
                fallen_allies.append(m["name"])
            elif aid in o.residual_hp:
                m["hp_current"] = int(o.residual_hp[aid])
        self.handle = None
        reward = {}
        if o.ended_reason == "victory" and self._boss_fight and not pc_dead and not fled:
            self.boss_rewards += 1
            reward = {"boss_reward": "A boss-tier foe has fallen. You may now craft ONE signature item with tier 'rare': "
                                     "forge_weapon OR forge_armor, whichever fits the story, themed on this foe "
                                     "(its damage types, powers, personality)."}
        self._boss_fight = False
        return {
            **reward, "combat_over": True, "result": "fled" if fled else ("defeat" if (pc_dead and not foes_down) else o.ended_reason),
            "foe_hp": foe_hp,
            "fallen_names": fallen, "fallen_allies": fallen_allies, "party": self._roster_view(), "xp_awarded": sum(o.xp_awarded.values()),
            "loot": [d.name or d.item_id for d in o.loot_drops], "pc_hp": hp, "pc_hp_max": self.pc.hp_max,
            "closing_events": self._digest(res.events),
        }

    def _monster_hint(self) -> list[str]:
        try:
            slugs = list(self.loader.list_slugs("monsters"))
        except Exception:
            return []
        ok = []
        for s in slugs:
            try:
                if _cr(self.loader.get_monster(s).cr) <= self.cfg.max_encounter_cr:
                    ok.append(s)
            except Exception:
                continue
        return ok[:25]

    # ------------------------------------------------------------------ checks
    def skill_check(self, skill: str, dc: int, advantage: str = "normal") -> dict:
        if self.sheet is None:
            return _err("No character exists yet.")
        sh = self.sheet
        short = {"strength": "str", "dexterity": "dex", "constitution": "con",
                 "intelligence": "int", "wisdom": "wis", "charisma": "cha"}
        scores = {short[k]: val for k, val in sh.ability_scores.model_dump().items() if k in short}
        try:
            res = E.resolve_check(E.CheckSpec(
                kind="skill", ability_scores=scores, proficient_skills=tuple(sh.skill_proficiencies),
                proficient_saves=tuple(sh.save_proficiencies), proficiency_bonus=sh.proficiency_bonus,
                skill=skill, dc=dc, advantage=(advantage == "advantage"),
                disadvantage=(advantage == "disadvantage"), expertise_skills=tuple(sh.skill_expertise),
                jack_of_all_trades=sh.jack_of_all_trades, reliable_talent=sh.reliable_talent, rng=self.rng))
        except Exception as ex:
            return _err(f"{type(ex).__name__}: {ex}"[:300])
        return {"ok": True, "skill": skill, "dc": dc, "natural_roll": res.natural_roll,
                "total": res.roll_total, "success": res.success}
