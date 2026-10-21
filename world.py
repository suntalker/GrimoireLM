"""Terrain generation, collision, and the overworld world map.

The LLM designs the continent ONCE at startup (bulk payload: cluster features, linear
features, and cloud exclusion zones). Python ingests that payload into a SPARSE coordinate
dictionary; every coordinate not explicitly defined defaults to open plains. All physical
movement is gated here, step by step, against impassable tags -- the model can never walk
the player through a mountain, a river, or an exclusion-zone barrier.

Design rules (mirrors the rest of the project):
  * plain dicts in / plain dicts out; no engine types cross this boundary
  * deterministic math only: the random walks are seeded from config.seed so a save rebuilds identically
  * the model NEVER sees the raw map (context bloat); it queries terrain through tools.py's
    world_* tools and keeps only landmark names/positions in its own notes
  * landmarks (village/town/city/dungeon/fortress) are strictly 1x1; forests and clouds spread
Pygame lives in world_render.py and imports THIS module, never the other way around, so all
logic stays testable without a display.
"""
from __future__ import annotations

import json
import random
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from soft import SoftModel as _SoftModel

# ---------------------------------------------------------------- terrain vocabulary
DEFAULT_TERRAIN = "plains"          # anything not explicitly painted is open grass

TERRAIN_EMOJI = {
    "plains": "\U0001f7e9",     # green square: open plains
    "forest": "\U0001f332",       # pine woods
    "mountain": "\U0001f3fb",     # peak
    "river": "\U0001f30a",        # water
    "hill": "\U0001f3d4",         # rolling highland
    "swamp": "\U0001f33e",        # marsh
    "desert": "\U0001f3dc\ufe0f",      # dunes
    "tundra": "\u2744\ufe0f",     # snow
    "coast": "\U0001f3d6",        # shore
    "lake": "\U0001f4ab",         # deep water
    "village": "\U0001f3d8",  # houses
    "town": "\U0001f3d8",   # houses
    "city": "\U0001f3f0",         # castle
    "office": "\U0001f3e2",       # office tower (modern/futuristic city skin)
    "dungeon": "\U0001f3db",      # classical ruins
    "factory": "\U0001f3ed",      # industrial dungeon skin
    "fortress": "\U0001f3f0",           # fortress (same skin as city)
    "ruins": "\U0001f3da",        # abandoned site
    "cloud": "\u2601",     # exclusion zone / magical barrier
}

# Travel time per biome in MINUTES per tile stepped onto (the engine clock; narrative.py mirrors this):
# plains 🟩 2h/tile (open ground, easy march), forest 🌲 4h/tile (dense brush slows the party),
# mountain 🗻 a full day/tile (grueling climbs). Unlisted passable terrain marches at the plains pace.
TERRAIN_TRAVEL_MINUTES = {"plains": 120, "forest": 240, "mountain": 1440}
TRAVEL_DEFAULT_MINUTES = 120


# Tags the raycast loop refuses to enter. Landmarks are always passable (they are places to visit).
IMPASSABLE_TAGS = frozenset({"mountain", "river", "cloud", "lake"})

# Cluster footprints grow as diamonds (Manhattan distance). size -> radius.
CLUSTER_SIZES = {"small": 1, "medium": 2, "large": 3}

# Linear feature kinds: mountains meander hard, rivers wander gently.
VECTOR_KINDS = {"mountain", "river", "ridge", "road"}

# Landmarks occupy exactly ONE tile and are always visitable/passable (never clusters).
POINT_TAGS = frozenset({"village", "town", "city", "dungeon", "fortress", "ruins", "office", "factory"})

# One-time landmark skins for a modern/sci-fi setting theme (theme-mapped in WorldMap.from_payload).
_MODERN_SKIN = {"city": "office", "dungeon": "factory"}
_MODERN_WORDS = re.compile(r"\b(cyber|sci[-\s]?fi|futuristic|modern|neon|urban|cityscap|spaceship|robot|"
                           r"industrial|post[-\s]?apocalyptic|present[-\s]?day)\b", re.I)


def _norm(text: str) -> str:
    return re.sub(r"[-_\s]+", "", str(text).lower().strip())


def emoji_for(tag: str) -> str:
    """Icon for a terrain tag; unknown custom tags still render (as a diamond), never crash."""
    return TERRAIN_EMOJI.get(tag, "\u25c6")


# ---------------------------------------------------------------- LLM schema (validated with pydantic, like the rest of the project)
def _build_schema():
    try:
        from pydantic import Field, ValidationError, model_validator

        class Feature(_SoftModel):
            """One bulk-payload entry: a cluster (size), a linear range (vector+length), or an exclusion zone (tag 'cloud')."""
            name: str = Field(min_length=1, max_length=60)
            coords: list[int] = Field(description="[x, y] tile coordinates of the feature's center/head")
            tag: str = Field(min_length=1, max_length=20, description="terrain tag, e.g. forest, mountain, river, village, city, dungeon, cloud")
            size: str = Field("", max_length=10, description="cluster features only: small | medium | large")
            vector: Optional[list[int]] = Field(None, description="linear features only: general [dx, dy] direction")
            length: int = Field(0, ge=0, le=60, description="linear features only: tiles long")

            @model_validator(mode="after")
            def _shape(self):
                if len(self.coords) != 2 or not all(isinstance(v, int) for v in self.coords):
                    raise ValueError("coords must be exactly two integers [x, y].")
                t = _norm(self.tag)
                if t in VECTOR_KINDS or t.startswith("range"):
                    if not self.vector or len(self.vector) != 2 or not any(self.vector):
                        raise ValueError(f"'{self.name}' is a linear feature ({self.tag}): give a non-zero vector [dx, dy].")
                    if self.length < 2:
                        raise ValueError(f"'{self.name}' is a linear feature ({self.tag}): give length >= 2.")
                elif not self.size:
                    raise ValueError(f"'{self.name}' is a point/cluster feature ({self.tag}): give size small, medium or large.")
                return self

        class ContinentPayload(_SoftModel):
            features: list[Feature] = Field(max_length=120, description="ALL landmarks, ranges and exclusion zones for the starting continent")

            @model_validator(mode="after")
            def _at_least_one(self):
                if not self.features:
                    raise ValueError("The continent payload needs at least one feature.")
                return self

        return {"Feature": Feature, "ContinentPayload": ContinentPayload, "ValidationError": ValidationError}
    except ImportError:
        return None


_SCHEMA = _build_schema()
Feature = _SCHEMA["Feature"] if _SCHEMA else None
ContinentPayload = _SCHEMA["ContinentPayload"] if _SCHEMA else None
ValidationError = _SCHEMA["ValidationError"] if _SCHEMA else Exception


# ---------------------------------------------------------------- the sparse world store
@dataclass
class PlayerState:
    x: int = 0
    y: int = 0
    facing: str = "east"
    steps_travelled: int = 0


@dataclass
class WorldMap:
    """Sparse coordinate dictionary + procedural expansion + step-by-step raycast collision.

    world_map maps (x, y) -> terrain tag. Coordinates absent from it default to `default`
    (open plains). Everything the LLM says is first validated here; the map itself is Python's.
    """
    world_map: dict[tuple[int, int], str] = field(default_factory=dict)
    landmarks: dict[str, tuple[int, int]] = field(default_factory=dict)   # lowercase name -> (x, y)
    named_features: dict[str, dict] = field(default_factory=dict)         # display name -> {tag, x, y, size?, length?}
    player: PlayerState = field(default_factory=PlayerState)
    default: str = DEFAULT_TERRAIN
    rng_seed: int = 0
    rng: random.Random = field(default_factory=random.Random)
    errors: list[str] = field(default_factory=list)       # malformed payload entries skipped at build time

    # -------------------------------------------------------------- construction
    @classmethod
    def from_payload(cls, payload, seed: int = 0, theme: str = "") -> "WorldMap":
        """Ingest the ONE-TIME bulk continent layout (a ContinentPayload or a raw dict/list of features).

        Fail-soft on malformed entries exactly like tools.dispatch: bad items are reported in
        `skipped`, good ones still build the world. Deterministic: same payload + seed -> same map.
        """
        if isinstance(payload, (str, bytes)):
            payload = json.loads(payload)
        if isinstance(payload, dict):
            feats = payload.get("features", [])
        elif isinstance(payload, list):
            feats = payload
        else:  # a validated ContinentPayload instance
            feats = [f.model_dump() if hasattr(f, "model_dump") else f for f in payload.features]

        w = cls(rng_seed=int(seed or 0))
        w.rng = random.Random(w.rng_seed)
        modern = bool(_MODERN_WORDS.search(theme or ""))
        errors: list[str] = []
        for raw in feats:
            try:
                feat = Feature.model_validate(raw) if Feature is not None else _dict_feature(raw)
            except Exception as ex:
                errors.append(f"{raw.get('name', '?') if isinstance(raw, dict) else '?'}: {str(ex)[:120]}")
                continue
            w._paint_feature(feat, modern=modern)
        w.errors = errors
        w.center_player()
        return w

    def _paint_feature(self, feat, modern: bool = False) -> None:
        x, y = int(feat.coords[0]), int(feat.coords[1])
        tag = _norm(feat.tag) or self.default
        if modern and tag in _MODERN_SKIN:                # sci-fi/modern themes reskin landmarks once, at build time
            tag = _MODERN_SKIN[tag]
        name = " ".join(str(feat.name).split())[:60]
        key = name.lower()
        head = self.landmarks.get(key)
        if head is not None:                              # duplicate landmark name: relocate silently, never overwrite
            x, y = self._free_spot(x, y)
        is_linear = tag in VECTOR_KINDS or (feat.vector is not None and feat.length >= 2)
        if is_linear:
            self._paint_line(name, tag, x, y, list(feat.vector), int(feat.length))
        elif tag in POINT_TAGS:
            self.set_tile(x, y, tag)                      # landmarks are strictly 1x1, never clusters
        else:
            radius = CLUSTER_SIZES.get((feat.size or "medium").lower(), 2)
            self._paint_cluster(x, y, tag, radius)
        self.landmarks[key] = (x, y)
        info = {"name": name, "tag": tag, "x": x, "y": y}
        if is_linear:
            info["length"] = int(feat.length)
        else:
            info["size"] = (feat.size or "medium").lower()
        self.named_features[name] = info

    @staticmethod
    def _dict_feature(raw: dict):
        """Fallback when pydantic is unavailable: minimal manual validation of a raw feature dict."""
        from types import SimpleNamespace
        coords = raw.get("coords")
        if not (isinstance(coords, list) and len(coords) == 2 and all(isinstance(v, int) for v in coords)):
            raise ValueError("coords must be exactly two integers [x, y].")
        return SimpleNamespace(name=str(raw.get("name", "Feature"))[:60], coords=coords,
                               tag=str(raw.get("tag", "plains")), size=str(raw.get("size", "") or ""),
                               vector=raw.get("vector"), length=int(raw.get("length", 0) or 0))

    def _free_spot(self, x: int, y: int) -> tuple[int, int]:
        """Spiral outward until a tile whose center footprint is untouched is found (duplicate-name relocation)."""
        for r in range(1, 40):
            for dx in range(-r, r + 1):
                for dy in (-r, r):
                    if self.tile_or_default(x + dx, y + dy) == self.default:
                        return x + dx, y + dy
            for dy in range(-r + 1, r):
                for dx in (-r, r):
                    if self.tile_or_default(x + dx, y + dy) == self.default:
                        return x + dx, y + dy
        return x, y

    # -------------------------------------------------------------- procedural painting
    def _paint_cluster(self, cx: int, cy: int, tag: str, radius: int) -> None:
        """Diamond footprint via Manhattan distance: |dx| + |dy| <= radius."""
        for dx in range(-radius, radius + 1):
            for dy in range(-radius - abs(dx), radius - abs(dx) + 1):
                x, y = cx + dx, cy + dy
                if self.tile_or_default(x, y) == self.default:
                    self.set_tile(x, y, tag)

    def _paint_line(self, name: str, tag: str, x: int, y: int, vector: list, length: int) -> None:
        """Jagged random walk for mountain ranges, gentler meander for rivers; biased toward the given vector."""
        vx, vy = int(vector[0]), int(vector[1])
        mag = max(1, (abs(vx) + abs(vy)))
        base_steps = [(vx // mag if vx else 0, vy // mag if vy else 0)]
        # perpendicular jitter directions make the walk organic instead of ruler-straight
        perp = [(vy // mag if vy else 0, -vx // mag if vx else 0),
                (-vy // mag if vy else 0, vx // mag if vx else 0)]
        roughness = 3 if tag in ("mountain", "ridge") else 2      # mountains jag harder than rivers meander
        jitter_dirs = [(1, 0), (-1, 0), (0, 1), (0, -1)]
        for i in range(int(length)):
            roll = self.rng.randrange(10)
            if roll < roughness:                                   # sideways jog off the main axis
                jx, jy = self.rng.choice(jitter_dirs)
                nx, ny = x + jx, y + jy
                if self.tile_or_default(nx, ny) == self.default:
                    x, y = nx, ny
            elif roll < roughness + 2 and perp[0] != (0, 0):       # drift along the perpendicular bias
                px, py = self.rng.choice([perp[0], perp[1]])
                nx, ny = x + px, y + py
                if self.tile_or_default(nx, ny) == self.default:
                    x, y = nx, ny
            else:                                                  # forward progress along the vector
                bx, by = base_steps[0]
                if bx == 0 and by == 0:
                    bx, by = self.rng.choice(jitter_dirs)
                nx, ny = x + bx, y + by
                if self.tile_or_default(nx, ny) != self.default:   # blocked ahead: step diagonally aside
                    alt = self.rng.choice(jitter_dirs)
                    nx, ny = x + alt[0], y + alt[1]
                x, y = nx, ny
            if self.tile_or_default(x, y) == self.default:
                self.set_tile(x, y, tag)
        self.named_features.setdefault(name, {"name": name, "tag": tag, "x": x, "y": y})

    # -------------------------------------------------------------- sparse store primitives
    def set_tile(self, x: int, y: int, tag: str) -> None:
        self.world_map[(int(x), int(y))] = tag

    def tile_or_default(self, x: int, y: int) -> str:
        """Default fallback logic: undefined coordinates are open plains."""
        return self.world_map.get((int(x), int(y)), self.default)

    def is_impassable(self, x: int, y: int) -> bool:
        return self.tile_or_default(x, y) in IMPASSABLE_TAGS

    def bounds(self) -> tuple[int, int, int, int]:
        if not self.world_map:
            return (-5, -5, 5, 5)
        xs = [p[0] for p in self.world_map]
        ys = [p[1] for p in self.world_map]
        return (min(xs), min(ys), max(xs), max(ys))

    def center_player(self) -> None:
        """Spawn at the safest spot near the geometric center of the painted continent.

        Appended preference: spawn on a tile the movement loop can enter cheaply (never in a
        mountain -- they are traversable now, just brutally slow); falls back to the old rule.
        """
        if self._center_player_on(lambda t: is_traversable(t) and t not in TRAVERSABLE_SLOW):
            return
        self._center_player_on(is_traversable)

    def _center_player_on(self, ok) -> bool:
        x0, y0, x1, y1 = self.bounds()
        cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
        if ok(self.tile_or_default(cx, cy)):
            self.player.x, self.player.y = cx, cy
            return True
        for r in range(1, 60):                                   # spiral to the nearest passable tile
            for dx in range(-r, r + 1):
                for dy in (-r, r):
                    if ok(self.tile_or_default(cx + dx, cy + dy)):
                        self.player.x, self.player.y = cx + dx, cy + dy
                        return True
            for dy in range(-r + 1, r):
                for dx in (-r, r):
                    if ok(self.tile_or_default(cx + dx, cy + dy)):
                        self.player.x, self.player.y = cx + dx, cy + dy
                        return True
        return False

    # -------------------------------------------------------------- step-by-step raycast & collision
    def move_steps(self, direction: str, steps: int) -> dict:
        """Advance ONE tile at a time; halt instantly and snap right before any barrier.

        Returns objective data for the LLM (steps_taken, stopped_by, ...) so it can narrate the
        block without ever seeing the map itself.

        Biome boundaries are SOFT stops (appended): crossing from one terrain tag into another
        halts the march after stepping ONTO the first tile of the new biome and reports
        stopped_by 'terrain_transition' -- a cinematic narrative trigger, never an error.
        Every tile stepped onto also accrues travel time (see TERRAIN_TRAVEL_MINUTES).
        """
        d = DIRECTIONS.get(_norm(direction))
        if d is None:
            return {"ok": False, "error": f"Unknown direction '{direction}'. Use: {', '.join(DIRECTIONS)}."}
        try:
            steps = int(steps)
        except (TypeError, ValueError):
            return {"ok": False, "error": "steps must be a whole number."}
        if steps < 1:
            return {"ok": False, "error": "steps must be at least 1."}
        steps = min(steps, MAX_MOVE_STEPS)
        p = self.player
        sx, sy = d
        taken = 0
        stopped_by: Optional[str] = None
        obstacle: Optional[tuple[int, int]] = None
        transition: Optional[dict] = None
        start_terrain = self.tile_or_default(p.x, p.y)     # state tracking: the biome the march begins in
        crossed: list[str] = []                             # tags of every tile actually stepped onto
        for _ in range(steps):
            nx, ny = p.x + sx, p.y + sy
            tag = self.tile_or_default(nx, ny)
            if not is_traversable(tag):                    # immediate halt & snap: stay right before the barrier
                stopped_by, obstacle = tag, (nx, ny)
                break
            p.x, p.y = nx, ny
            taken += 1
            crossed.append(tag)
            if tag != start_terrain:                       # tag mismatch after >=1 step: soft stop ON the new biome
                stopped_by, transition = "terrain_transition", {"from": start_terrain, "to": tag}
                break
        p.facing = direction
        p.steps_travelled += taken
        res = {"ok": True, "final_position": [p.x, p.y], "direction": direction,
               "requested": steps, "requested_steps": steps, "steps_taken": taken,
               "position": [p.x, p.y], "terrain": self.tile_or_default(p.x, p.y),
               "current_terrain": self.tile_or_default(p.x, p.y),
               "travel_minutes": travel_time_for(crossed),   # per-biome march cost: plains 2h, forest 4h, mountain 1 day
               "stopped_by": stopped_by}
        if transition:
            res["transition"] = transition                 # structured event the orchestrator flags as narrative
        if stopped_by and stopped_by != "terrain_transition":
            res["obstacle_at"] = list(obstacle)
            res["obstacle_emoji"] = emoji_for(stopped_by)
        return res

    def look(self, direction: str = "", distance: int = 1) -> dict:
        """Raycast report: what lies ahead, tile by tile, up to LOOK_RANGE tiles."""
        p = self.player
        if direction:
            d = DIRECTIONS.get(_norm(direction))
            if d is None:
                return {"ok": False, "error": f"Unknown direction '{direction}'. Use: {', '.join(DIRECTIONS)}."}
            cells = [(p.x + d[0] * k, p.y + d[1] * k) for k in range(1, min(int(distance or 1), LOOK_RANGE) + 1)]
        else:
            cells = [(p.x, p.y)]
        out = []
        for (x, y) in cells:
            tag = self.tile_or_default(x, y)
            out.append({"offset": [x - p.x, y - p.y], "terrain": tag, "impassable": not is_traversable(tag),
                        "travel_minutes": travel_time_for([tag]),
                        "near_landmark": self._nearest_landmark(x, y)})
        return {"ok": True, "position": [p.x, p.y], "tiles": out}

    def terrain_report(self, x: int, y: int, radius: int = 0) -> dict:
        """Compact terrain summary around a coordinate (used by landmark lookup; never dumps the map)."""
        x, y, radius = int(x), int(y), max(0, min(int(radius), 6))
        ring: dict[str, int] = {}
        for dx in range(-radius, radius + 1):
            for dy in range(-radius, radius + 1):
                if abs(dx) + abs(dy) <= radius:
                    tag = self.tile_or_default(x + dx, y + dy)
                    ring[tag] = ring.get(tag, 0) + 1
        return {"center": [x, y], "radius": radius, "terrain_mix": ring,
                "impassable_center": self.is_impassable(x, y)}

    def find_landmark(self, query: str) -> dict:
        key = " ".join(str(query).lower().split())
        hit = self.landmarks.get(key)
        if hit is None:                                    # tolerate partial names ('whispering' -> whispering woods)
            cands = [k for k in self.landmarks if key and (key in k or k.startswith(key))]
            if len(cands) == 1:
                hit = self.landmarks[cands[0]]
                key = cands[0]
            elif len(cands) > 1:
                return {"ok": False, "error": f"Ambiguous landmark '{query}'. Matches: {', '.join(sorted(cands))[:220]}."}
        if hit is None:
            return {"ok": False, "error": f"No landmark named '{query}' is recorded on the continent.",
                    "hint": "Only landmarks the DM registered exist; check your notes or record_fact."}
        name = next((n for n, (lx, ly) in self.landmarks.items() if (lx, ly) == hit), key)
        info = self.named_features.get(name, {})
        dist = abs(hit[0] - self.player.x) + abs(hit[1] - self.player.y)
        res = {"ok": True, "name": info.get("name", name.title()), "tag": info.get("tag", self.tile_or_default(*hit)),
               "coords": list(hit), "distance_tiles": dist,
               "bearing": bearing_from(self.player.x, self.player.y, hit[0], hit[1]),
               "terrain_here": self.tile_or_default(*hit)}
        res.update(self.terrain_report(hit[0], hit[1], radius=2))
        return res

    def _nearest_landmark(self, x: int, y: int, within: int = 2) -> Optional[str]:
        best, bestd = None, within + 1
        for key, (lx, ly) in self.landmarks.items():
            d = max(abs(lx - x), abs(ly - y))
            if d < bestd:
                best, bestd = key, d
        if best is None:
            return None
        info = self.named_features.get(next((n for n, k in self.landmarks.items() if k == best), best), {})
        return info.get("name", best.title())

    # -------------------------------------------------------------- persistence
    def export_state(self) -> dict:
        return {"world_map": {f"{x},{y}": t for (x, y), t in sorted(self.world_map.items())},
                "landmarks": {k: list(v) for k, v in self.landmarks.items()},
                "named_features": self.named_features,
                "player": asdict(self.player), "default": self.default, "rng_seed": self.rng_seed}

    def restore(self, data: dict) -> None:
        if not data:
            return
        self.world_map = {}
        for key, tag in data.get("world_map", {}).items():
            x, y = key.split(",")
            self.world_map[(int(x), int(y))] = tag
        self.landmarks = {k: tuple(v) for k, v in data.get("landmarks", {}).items()}
        self.named_features = dict(data.get("named_features", {}))
        self.default = data.get("default", DEFAULT_TERRAIN)
        self.rng_seed = int(data.get("rng_seed", 0) or 0)
        self.rng = random.Random(self.rng_seed)
        pl = data.get("player", {})
        self.player = PlayerState(x=int(pl.get("x", 0)), y=int(pl.get("y", 0)),
                                  facing=pl.get("facing", "east"), steps_travelled=int(pl.get("steps_travelled", 0)))

    def save_file(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.export_state(), indent=1))

    def load_file(self, path: str | Path) -> bool:
        p = Path(path)
        if not p.exists():
            return False
        self.restore(json.loads(p.read_text()))
        return True


# ---------------------------------------------------------------- directions & constants
DIRECTIONS = {
    "north": (0, -1), "n": (0, -1), "up": (0, -1),
    "south": (0, 1), "s": (0, 1), "down": (0, 1),
    "east": (1, 0), "e": (1, 0), "right": (1, 0),
    "west": (-1, 0), "w": (-1, 0), "left": (-1, 0),
    "northeast": (1, -1), "northwest": (-1, -1), "southeast": (1, 1), "southwest": (-1, 1),
}

MAX_MOVE_STEPS = 100      # per command; generous, the collision loop does the real gating
LOOK_RANGE = 12


def bearing_from(x0: int, y0: int, x1: int, y1: int) -> str:
    """Compass word describing x1,y1 seen from x0,y0 (screen convention: north = smaller y)."""
    dx, dy = x1 - x0, y1 - y0
    parts = []
    if dy < 0:
        parts.append("north")
    elif dy > 0:
        parts.append("south")
    if dx > 0:
        parts.append("east")
    elif dx < 0:
        parts.append("west")
    if not parts:
        return "here"
    return "-".join(parts[::-1]) if len(parts) == 2 else parts[0]


# ---------------------------------------------------------------- startup bulk generation prompt
CONTINENT_PROMPT = """You are designing the ENTIRE starting continent for a terrain-engine RPG, in ONE batch.

Output ONLY a JSON object: {"features": [ ... ]} containing 30 to 50 features total. Three shapes:

CLUSTER (forests, villages-as-hamlets, swamps, moors):
  {"name": "Whispering Woods", "coords": [3, 4], "tag": "forest", "size": "small|medium|large"}

LINEAR (mountain ranges, rivers, ridges):
  {"name": "Dragon Spine Range", "coords": [-5, 2], "tag": "mountain", "vector": [1, 1], "length": 8}

EXCLUSION ZONE (high-level/endgame barriers the party must NOT reach early - use tag "cloud"):
  {"name": "The Obsidian Citadel (Endgame)", "coords": [25, 30], "tag": "cloud", "size": "large"}

Landmarks are STRICTLY 1x1 (never a cluster): tags village, town, city, dungeon, fortress, ruins.

RULES
- Coordinates are tile integers, roughly within -40..40. Spread features across the whole continent.
- Include at least: 2 mountain ranges, 2 rivers, 3 forests, 2 villages or towns, 1 city, 1 dungeon,
  2 cloud exclusion zones placed FAR from the continent's center (they wall off your endgame areas).
- Keep at least a 10-tile clear corridor around (0,0): the hero spawns near the middle of the map.
- Do not stack features on the same tile. Rivers may run alongside a range; clouds should ring dangerous lands.
- Names must be evocative and fit the campaign theme. Do NOT describe terrain the engine already owns;
  you only place features - Python paints footprints, grows clusters and blocks movement."""


def build_continent_prompt(manifest: dict | None = None, min_features: int = 30, max_features: int = 50) -> str:
    theme = ""
    if manifest:
        theme = f"\nCAMPAIGN THEME: {manifest.get('theme', '')}\nGOAL: {manifest.get('goal', '')}\n" \
                f"ANTAGONIST: {manifest.get('antagonist_name', '')}"
    return CONTINENT_PROMPT.replace("30 to 50", f"{min_features} to {max_features}") + theme


# ---------------------------------------------------------------- fail-soft fallback continent
def fallback_continent(seed: int = 0) -> dict:
    """Deterministic hand-built continent used when the model server is down/rejects JSON.

    Same schema as the LLM payload, so Python expands it through the identical pipeline.
    """
    rng = random.Random(seed if seed is not None else 0)
    feats = [
        {"name": "Verdant Expanse", "coords": [0, 0], "tag": "plains", "size": "large"},
        {"name": "Larkspur Village", "coords": [2, -3], "tag": "village", "size": "small"},
        {"name": "Brackenham Town", "coords": [-6, 4], "tag": "town", "size": "small"},
        {"name": "Aurelia City", "coords": [9, 7], "tag": "city", "size": "small"},
        {"name": "Whispering Woods", "coords": [3, 4], "tag": "forest", "size": "large"},
        {"name": "Thornfield Copse", "coords": [-3, -6], "tag": "forest", "size": "medium"},
        {"name": "Gloomwood", "coords": [12, -4], "tag": "forest", "size": "large"},
        {"name": "Dragon Spine Range", "coords": [-5, 2], "tag": "mountain", "vector": [1, 1], "length": 14},
        {"name": "Greyfang Ridge", "coords": [-14, -8], "tag": "mountain", "vector": [1, 0], "length": 10},
        {"name": "Silverthread River", "coords": [0, -12], "tag": "river", "vector": [0, 1], "length": 18},
        {"name": "Mournfork", "coords": [14, 2], "tag": "river", "vector": [-1, 1], "length": 12},
        {"name": "Sunken Fen", "coords": [-10, 10], "tag": "swamp", "size": "medium"},
        {"name": "Barrowdown Ruins", "coords": [6, -8], "tag": "ruins", "size": "small"},
        {"name": "Crypt of the Pale King", "coords": [-8, -12], "tag": "dungeon", "size": "small"},
        {"name": "Ironhold Fortress", "coords": [16, 12], "tag": "fortress", "size": "small"},
        {"name": "The Obsidian Citadel (Endgame)", "coords": [28, 30], "tag": "cloud", "size": "large"},
        {"name": "Stormcage Peaks (High-Level)", "coords": [-30, -26], "tag": "cloud", "size": "large"},
        {"name": "Ashen Barrens", "coords": [22, -18], "tag": "desert", "size": "medium"},
        {"name": "Whitehollow Tundra", "coords": [-22, 18], "tag": "tundra", "size": "medium"},
        {"name": "Mirror Lake", "coords": [4, 14], "tag": "lake", "size": "small"},
    ]
    # scatter a few extra camps so the count lands in the 30-50 ballpark
    for i in range(12):
        x = rng.randrange(-20, 21)
        y = rng.randrange(-20, 21)
        feats.append({"name": f"Waystone Camp {chr(65 + i)}", "coords": [x, y], "tag": "ruins", "size": "small"})
    return {"features": feats}


# ---------------------------------------------------------------- one-time bulk generation call
def generate_continent(llm, cfg, manifest: dict | None = None, seed: int = 0, theme: str = "") -> WorldMap:
    """Startup payload: ask the model for the WHOLE continent in one structured call, then build it.

    Fails soft exactly like narrative.create_manifest: if the server is down or answers garbage,
    the deterministic fallback continent is used and the game still boots.
    """
    msgs = [{"role": "system", "content": "Reply with ONLY a JSON object matching the schema."},
            {"role": "user", "content": build_continent_prompt(manifest)}]
    payload = None
    if llm is not None and ContinentPayload is not None:
        try:
            result = llm.structured(msgs, ContinentPayload, effort=getattr(cfg, "planner_effort", "low"))
            payload = result.model_dump() if result is not None else None
        except Exception:
            payload = None
    if payload is None or not payload.get("features"):
        payload = fallback_continent(seed)
    return WorldMap.from_payload(payload, seed=seed, theme=theme or (manifest or {}).get("theme", ""))


# ---------------------------------------------------------------- CLI mini-map (no pygame needed)
def minimap(world: WorldMap, half: int = 7) -> str:
    """Text viewport centered on the player; used by the /map command when pygame is unavailable."""
    p = world.player
    rows = []
    for dy in range(-half, half + 1):
        cells = []
        for dx in range(-half * 2, half * 2 + 1):      # emojis are wide: double the horizontal span
            x, y = p.x + dx // 2, p.y + dy
            if x == p.x and y == p.y and dx % 2 == 0:
                cells.append("\U0001f534")             # player
            else:
                cells.append(emoji_for(world.tile_or_default(x, y)))
        rows.append("".join(cells))
    return "\n".join(rows)


# ---------------------------------------------------------------- traversal rules (appended)
# IMPASSABLE_TAGS is the HARD barrier set: the raycast refuses to enter those tiles at all.
# TRAVERSABLE_SLOW tags are ENTERED, but they cost extra time on the clock (mountains: a full
# day per tile). A march still soft-stops when it crosses from one biome tag into another, so
# entering a mountain pass reads as a cinematic terrain_transition, exactly like any boundary.
TRAVERSABLE_SLOW = frozenset({"mountain"})


def is_traversable(tag: str) -> bool:
    """Can the step-by-step raycast physically enter a tile with this tag?"""
    return tag not in IMPASSABLE_TAGS or tag in TRAVERSABLE_SLOW


def travel_time_for(tags) -> int:
    """Minutes consumed walking the given sequence of terrain tags (one entry per tile stepped onto).

    Plains 🟩 2h/tile, forest 🌲 4h/tile, mountain 🗻 a full day/tile; unpriced passable terrain
    marches at the plains pace. This is the single source of truth -- narrative.py mirrors it.
    """
    return sum(TERRAIN_TRAVEL_MINUTES.get(t, TRAVEL_DEFAULT_MINUTES) for t in tags)


# ---------------------------------------------------------------- exclusion-zone lifting (appended)
# The LLM can NEVER rewrite the sparse map from prose; barrier removal is a sanctioned tool call
# (tools.py: world_lift_exclusion) into these primitives. Underneath every lifted ☁ cluster lies
# the untouched landscape -- clouds are painted OVER the dict and default-fallback logic already
# treats absent coordinates as open plains, so revealing = deleting the cloud tiles that cover them.
CLOUD_TAG = "cloud"


def _manhattan(a: tuple[int, int], b: tuple[int, int]) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def lift_exclusion(world: "WorldMap", name: str = "", x: Optional[int] = None,
                   y: Optional[int] = None, radius: int = 3) -> dict:
    """Lift a cloud exclusion zone: delete its ☁ tiles so the real landscape underneath shows.

    Target by landmark `name` (the zone's recorded center + footprint radius are used), or by an
    explicit (x, y) survey point with `radius`. Tiles whose tag != "cloud" are never touched, so
    lifting can only ever open magical barriers -- mountains/rivers/lakes keep blocking movement
    until the party physically walks around or through them. Returns objective data (revealed tags,
    new terrain mix) so the DM narrates what Python actually uncovered, never invented scenery.
    """
    cx = cy = None
    if name and str(name).strip():
        info = world.find_landmark(str(name).strip())
        if not info.get("ok"):
            return {"ok": False, "error": info.get("error", f"Unknown exclusion zone '{name}'.")}
        # find_landmark returns flat data (no nested 'feature'); read center + size from there.
        if info.get("tag") != CLOUD_TAG:
            return {"ok": False,
                    "error": f"'{info.get('name', name)}' is a {info.get('tag')} landmark, not a cloud exclusion zone."}
        cx, cy = int(info["coords"][0]), int(info["coords"][1])
        nf = world.named_features.get(info.get("name", ""), {})
        radius = max(int(radius), CLUSTER_SIZES.get(str(nf.get("size") or "medium").lower(), 2))
    elif x is not None and y is not None:
        cx, cy = int(x), int(y)
    else:
        return {"ok": False, "error": "Provide either an exclusion-zone name or center coordinates (x, y)."}

    removed: list[list[int]] = []
    revealed: dict[tuple[int, int], str] = {}
    for (tx, ty), tag in list(world.world_map.items()):
        if tag == CLOUD_TAG and _manhattan((tx, ty), (cx, cy)) <= radius:
            del world.world_map[(tx, ty)]
            removed.append([tx, ty])
            revealed[(tx, ty)] = world.tile_or_default(tx, ty)   # post-deletion read = what lay beneath
    if not removed:
        return {"ok": True, "center": [cx, cy], "cloud_tiles_removed": 0,
                "note": "No cloud barrier tiles here -- this area is already open."}
    mix: dict[str, int] = {}
    for t in revealed.values():
        mix[t] = mix.get(t, 0) + 1
    return {"ok": True, "center": [cx, cy], "radius": radius,
            "cloud_tiles_removed": len(removed), "new_terrain_mix": mix,
            "passable_now": any(is_traversable(t) for t in revealed.values()),
            "sample_revealed": [{"coords": [k[0], k[1]], "terrain": v}
                                for k, v in sorted(revealed.items(), key=lambda kv: _manhattan((cx, cy), kv[0]))[:8]]}


# ==================================================== skip travel: A* pathfinding (appended)
# Fast travel is Python-owned: the model only NAMES a destination and the engine traces the legal
# route tile by tile. Hard barriers (river 🌊, lake, cloud ☁️ exclusion zones) are collision-gated
# out of path generation; every legal step accumulates its biome's march cost (plains 2h, forest
# 4h, mountain 8 hours... a full day). If no land route exists at all, the party halts on the last
# valid tile before the barrier -- never teleported, never phased through water or magical fog.
import heapq as _heapq

FAST_SEARCH_RADIUS = 6     # domain around the player searched for the requested landmark (Manhattan)
FAST_MAX_PATH = 150        # hard cap on tiles a single skip-travel may cover

# 8-neighborhood: cardinals + diagonals (D&D 5e RAW diagonal movement; corner-cutting allowed).
_FAST_NEIGHBORS = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))


def astar_path(world: "WorldMap", start: tuple[int, int], goal: tuple[int, int]) -> Optional[list]:
    """Shortest LEGAL route across the sparse grid, minimizing accumulated terrain TIME.

    A* with a Manhattan-distance heuristic scaled by the cheapest tile cost (plains pace), which
    keeps it ADMISSIBLE: h(node) never overestimates the true remaining time, so the first time the
    goal is popped its g-cost is provably optimal. Terrain weights: plains 2h/tile, forest 4h/tile,
    mountain a full day/tile; impassable tags (river 🌊 / lake 💫 / cloud ☁) are never expanded, so
    a barrier simply has no node and hard-blocks any route through it. The start tile is free (the
    party already stands on it). Returns the tile list INCLUDING start and goal, or None when the
    goal is unreachable or the route exceeds the search cap.
    """
    start = (int(start[0]), int(start[1]))
    goal = (int(goal[0]), int(goal[1]))
    if start == goal:
        return [start]
    if not is_traversable(world.tile_or_default(*goal)):
        return None                                   # never path INTO a barrier tile
    counter = 0                                       # deterministic tie-breaker between equal f-scores
    open_heap: list = [(_manhattan(start, goal) * TRAVEL_DEFAULT_MINUTES, 0, start)]
    g_cost = {start: 0}
    closed: set[tuple[int, int]] = set()
    came: dict[tuple[int, int], tuple[int, int]] = {}
    expanded = 0
    while open_heap:
        _, _, cur = _heapq.heappop(open_heap)
        if cur == goal:
            path = [cur]
            while path[-1] in came:
                path.append(came[path[-1]])
            path.reverse()
            return path if len(path) - 1 <= FAST_MAX_PATH else None
        if cur in closed:                             # stale heap entry: best cost already settled
            continue
        closed.add(cur)
        cg = g_cost[cur]
        expanded += 1
        if expanded > 40000:                          # runaway-search guard on huge continents
            return None
        for dx, dy in _FAST_NEIGHBORS:
            nxt = (cur[0] + dx, cur[1] + dy)
            if nxt in closed or world.is_impassable(*nxt):
                continue                              # collision gate: river/lake/cloud block path generation
            tag = world.tile_or_default(*nxt)         # EXACT biome weight of the tile stepped onto
            ng = cg + TERRAIN_TRAVEL_MINUTES.get(tag, TRAVEL_DEFAULT_MINUTES)
            if ng < g_cost.get(nxt, float("inf")):
                g_cost[nxt] = ng
                came[nxt] = cur
                counter += 1
                _heapq.heappush(open_heap, (ng + _manhattan(nxt, goal) * TRAVEL_DEFAULT_MINUTES, counter, nxt))
    return None                                       # frontier exhausted: completely cut off


def fast_travel(world: "WorldMap", name: str = "", x: Optional[int] = None, y: Optional[int] = None,
                radius: int = FAST_SEARCH_RADIUS) -> dict:
    """Skip-travel entry point used by tools.world_fast_travel (and any future UI layer).

    Destination resolution: an explicit landmark `name`, or coordinates (x, y) snapped to the
    nearest recorded landmark inside the search domain. Without a name, Python establishes a
    `radius`x`radius` Manhattan domain around the party and path-traces the CLOSEST matching
    landmark; with a name, that landmark must lie within the domain unless `radius` is widened.
    Returns objective path metadata (tiles_traversed, travel_hours, days_crossed, final_position,
    path_preview, ...); time accrual/clock/auto-camp are applied by the tool wrapper in tools.py.
    """
    p = world.player
    px, py = p.x, p.y
    radius = max(1, min(int(radius or FAST_SEARCH_RADIUS), 80))
    dest_key = dest_name = ""
    coords: Optional[tuple[int, int]] = None
    if name and str(name).strip():
        info = world.find_landmark(str(name).strip())
        if not info.get("ok"):
            return {"ok": False, "error": info.get("error", f"Unknown landmark '{name}'.")}
        coords = (int(info["coords"][0]), int(info["coords"][1]))
        dest_key = next((k for k, v in world.landmarks.items() if v == coords), "")
        dest_name = info.get("name", dest_key.title() or name)
        dist = _manhattan(coords, (px, py))
        if dist > radius:
            return {"ok": False,
                    "error": f"'{dest_name}' lies {dist} tiles away -- outside the {radius}-tile fast-travel "
                             f"domain. Walk closer with world_travel, or pass a bigger radius only if the "
                             f"story justifies a long organized journey."}
    elif x is not None and y is not None:
        tx, ty = int(x), int(y)
        best: Optional[tuple[int, int, str]] = None   # (distance, coords, key)
        for key, c in world.landmarks.items():
            d = _manhattan(c, (tx, ty))
            if d <= radius and (best is None or d < best[0]):
                best = (d, c, key)
        if best is None:
            return {"ok": False,
                    "error": f"No recorded landmark within {radius} tiles of ({tx}, {ty}). "
                             f"Fast travel only reaches known places; survey with world_terrain first."}
        _, coords, dest_key = best
        nf = world.named_features.get(dest_key, {})
        dest_name = nf.get("name") or dest_key.title()
    else:
        return {"ok": False, "error": "Provide either a landmark name or target coordinates (x, y)."}

    if coords == (px, py):
        return {"ok": True, "destination": dest_name, "already_there": True, "tiles_traversed": 0,
                "travel_hours": 0.0, "days_crossed": 0, "final_position": [px, py]}

    path = astar_path(world, (px, py), coords)
    if path is None:                                  # route failure: halt at the last valid tile
        stop = (px, py)
        last_tag = "unknown"
        for dx, dy in _FAST_NEIGHBORS:
            nb = (coords[0] + dx, coords[1] + dy)
            t = world.tile_or_default(*nb)
            if is_traversable(t):
                stop, last_tag = nb, t
                break
        return {"ok": False, "stopped_by": "no_route", "barrier": world.tile_or_default(*coords),
                "last_valid_tile": list(stop), "destination": dest_name,
                "error": f"No legal land route to {dest_name}: rivers, lakes, or cloud exclusion zones "
                         f"completely cut it off. The party holds position"
                         + (f" (nearest approach: {stop[0]}, {stop[1]}, {last_tag} ground)." if stop != (px, py) else ".")}

    crossed = path[1:]                                # time accrues per tile stepped ONTO (start is free)
    minutes = travel_time_for(crossed)
    mix: dict[str, int] = {}
    for t in crossed:
        mix[t] = mix.get(t, 0) + 1
    p.x, p.y = coords                                 # coordinate snapping: instant, atomic
    p.steps_travelled += len(crossed)
    p.facing = bearing_from(px, py, coords[0], coords[1])
    return {"ok": True, "destination": dest_name, "destination_tag": world.tile_or_default(*coords),
            "tiles_traversed": len(crossed), "travel_minutes": minutes,
            "travel_hours": round(minutes / 60.0, 1), "days_crossed": 0,
            "final_position": [coords[0], coords[1]], "from_position": [px, py],
            "terrain_mix": mix,
            "path_preview": [[t, world.tile_or_default(*xy)] for t, xy in enumerate(path[:12])]
                            + ([["...", f"{len(path) - 12} more tiles"]] if len(path) > 12 else [])}
