"""All tunables in one place. Override any field via config.json next to this file."""
from __future__ import annotations

import json
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass
class Config:
    # --- model endpoint (any OpenAI-compatible server: LM Studio, Ollama, llama.cpp, Strata) ---
    base_url: str = "http://localhost:1234/v1"
    model: str = "openai/gpt-oss-20b"
    api_key: str = "not-needed"
    temperature: float = 0.8
    timeout: float = 180.0
    reasoning_effort: str = "low"      # low | medium | high, used for DM turns
    summary_effort: str = "low"
    send_reasoning_effort: bool = True  # set False if your server rejects the extra field
    use_json_schema_format: bool = True  # set False if your server rejects response_format

    # --- agent loop ---
    max_tool_steps: int = 6            # tool-call rounds per player turn before forcing narration
    max_nudges: int = 2                # times to reject a turn where the model narrated mechanics without a tool

    # --- summarize / save policy (the loop forces this, the model never decides) ---
    summary_every_turns: int = 10      # summarize once this many turns are unsummarized...
    summary_token_threshold: int = 1800  # ...or the unsummarized log passes ~this many tokens
    summary_keep_recent: int = 3       # newest turns stay verbatim, never summarized yet
    canon_in_prompt: int = 15          # newest canon facts always shown to the DM

    # --- narrative scheduler / clock / watchdog ---
    chunk_days: int = 28               # monthly chunk: raw history is wiped and a 4-week plan generated every N days
    watchdog_days: int = 21            # an NPC/subplot/faction ignored this long triggers a CRITICAL alert
    minutes_per_turn: int = 15         # in-game time that passes each turn unless the DM calls advance_time
    start_hour: int = 8
    max_history_chunks: int = 6        # older chunk records are merged into one 'earlier' record
    history_in_prompt: int = 2         # how many recent chunk records the DM sees each turn
    planner_effort: str = "medium"     # reasoning effort for the (rare) monthly planning call
    profiles_in_prompt: int = 3        # max entity profiles injected per turn (only relevant ones)

    # --- party / companions ---
    max_active_companions: int = 3     # only ACTIVE companions are placed in combat
    max_roster: int = 8

    # --- engine / safety limits ---
    max_monsters: int = 6
    max_boss_threat: int = 1500        # cap for stored NPC stat blocks (HP x average damage); bosses may exceed the per-encounter cap
    show_enemy_stats: bool = False     # /scene reveals exact enemy HP when True (the DM never sees it either way)
    max_encounter_cr: float = 2.0      # sum of CRs the model may spawn in one encounter (SRD templates)
    max_custom_threat: int = 400       # per character level; threat = HP x avg damage per hit, summed over invented creatures
    grid_width: int = 8
    grid_height: int = 6
    seed: int | None = None

    # --- paths ---
    save_dir: str = "saves"
    save_name: str = "campaign1"
    lore_dir: str = "lore"

    @classmethod
    def load(cls, path: str | Path = "config.json") -> "Config":
        cfg = cls()
        p = Path(path)
        if p.exists():
            data = json.loads(p.read_text())
            known = {f.name for f in fields(cls)}
            for k, v in data.items():
                if k in known:
                    setattr(cfg, k, v)
        return cfg
