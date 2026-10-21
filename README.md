# LLM-driven DM on dnd5e-engine

The engine owns every number. The model narrates, picks tool calls, and writes story memory.

## Setup
    python -m venv venv && source venv/bin/activate      # Windows: venv\Scripts\activate
    pip install -r requirements.txt
    python test_smoke.py        
    python game.py              # needs a model server

## Point it at a model (config.json next to game.py, any field optional)
    {"base_url": "http://localhost:1234/v1", "model": "openai/gpt-oss-20b", "reasoning_effort": "low"}
Swap models by changing base_url/model only (LM Studio, Ollama, llama.cpp, Strata).
If your server rejects the extra reasoning field or response_format, set
`send_reasoning_effort` / `use_json_schema_format` to false.

## Layout
- engine_adapter.py  the ONLY file that imports dnd5e_engine (plain dicts in/out)
- tools.py           the model's whole reach: 19 validated tools, whitelist dispatch
- narrative.py       clock, 28-day scheduler, entity profiles, relationships, watchdog
- memory.py          save state, JSONL log, keyword lore search, summarizer + trigger policy
- game.py            prompt assembly, tool loop, forced summarize/save after every turn
- llm.py             OpenAI-compatible client + validated structured output
- lore/*.md          your setting notes: "# Title", optional "tags: a, b", then body

## Summarize/save policy (config.py)
After every turn the loop saves, and summarizes when unsummarized turns >= summary_every_turns (10)
or ~summary_token_threshold (1800) tokens, or when a combat ends. The newest summary_keep_recent (3)
turns stay verbatim. The raw log is never deleted (saves/<name>/log.jsonl).

## Known limits
- Mid-combat state is not saved; save/quit between encounters.
- rest() restores HP and spell slots only (feature-use recovery not wired up).
- Unique weapons (forge_weapon) are melee: a real SRD chassis + up to 2 rider damage entries + an optional single-target damage power with 1-3 charges. Powers cannot apply conditions (the engine can't resolve custom effects), and the save DC is set by the engine, not by us. Weapon masteries and passive wielder bonuses are not used.
- Gold is an engine ledger (starts at 15 gp). change_gold / buy_item / remove_item are the only ways money and plain items move; standard weapons and armor are priced from the SRD data and really equipped (body armor swaps and AC is recomputed by the engine; downgrades are refused).
- Rare/legendary weapons need a boss reward (earned by winning a fight with a CR>=1 or high-threat foe).
- Unique armor (forge_armor) = real SRD armor/shield chassis + magical AC bonus + up to 2 damage resistances + optional power; it replaces worn armor and may not lower AC. Rare armor and rare weapons draw from the same boss reward. Invented potions are not supported yet; trinkets are inventory only.
- Only a fixed action set is exposed (attack, spells, dash, dodge, ...); homebrew abilities come next.
- Pinned to dnd5e-engine 0.7.0; the API is young, so re-run test_smoke.py after any upgrade.

## Narrative layer (narrative.py)
- **Manifest**: saves/<name>/manifest.json (theme, goal, antagonist). Permanent; hand edits are honored. Asked once at new game; blanks are invented by the model.
- **Clock**: the engine owns the calendar. Each turn adds `minutes_per_turn`; the DM calls advance_time for travel/downtime; rest = 8 hours. The CLI prints `[Day N, Evening 19:30 | month M, week W/4 | HP | gp]`.
- **Monthly chunk (every `chunk_days`=28)**: the finished month is compressed into ONE history record, raw turns are wiped from the live state (log.jsonl on disk keeps everything), and a new 4-week plan is generated. Old records merge once there are more than `max_history_chunks`.
- **Prompt stays lean**: manifest + clock + THIS WEEK's beat + the last `history_in_prompt` records + only the relevant entity profiles. Forge tool schemas are only sent when loot or a boss reward is in play.
- **Entities** (npc / subplot / faction): fixed anchor (`core`: personality, ideology, premise) that can never be edited; mutable current_state / future_plans / objectives; a private `memories` list injected only when that entity is mentioned or in focus.
- **Relationships**: score -100..100 with tiers (Sworn Enemy, Hostile -25, Unfriendly, Neutral, Friendly, Trusted, Devoted). Daily caps stop emotional whiplash: hostile characters regain at most +3/day, devoted ones lose at most -5/day. Faction-to-faction relations move at most 10/day.
- **Watchdog**: `days_since_interaction` per entity; at `watchdog_days`=21 a CRITICAL alert is shown on the CLI and in the prompt, and the next monthly plan MUST reintroduce them (the engine repairs the plan if the model forgets). Story mentions also refresh the clock as a safety net.
- CLI: `/entities` (tiers and quiet days), `/plan`, `/scene`, `/summary`, `/quit`.
- Tests: `python test_narrative.py`
- **Permanent endings**: update_entity(status=...) retires an entity for good: `dead` (npc), `disbanded` (faction), `resolved` (subplot), always with a `reason`. Retired entities leave the watchdog, the planner (their ids are stripped from every plan), the profile injection and the cast-size limit, and the prompt carries a one-line "THE DEAD AND DISBANDED" roster so the DM never improvises them alive. `interact` and `create_entity` refuse to touch them. A tracked NPC killed in combat is marked dead automatically when a defeated monster's name exactly matches theirs. Reviving someone requires a concrete in-story `reason` (10+ characters) and is logged as a memory.

## Companions
- The engine builds each companion as a real character and plays their turns with a simple AI (see below). Enemies target companions too.
- Companion HP carries between fights; `rest` heals them. A companion who falls is marked dead in the roster AND the narrative tracker (permanent, same rules as any NPC). If the player character falls the fight ends as a defeat.
- Recruits are also tracked people (relationship starts at +20, memories, watchdog). Tests: `python test_party.py`.

## Text limits
Schemas never send a hard `maxLength` to the model: servers that enforce JSON schemas during generation would cut text mid-sentence at the limit. The limit is stated in the field description and anything still too long is trimmed to the last complete sentence (soft.py).

## Companions designed by the DM (setting-flexible)
- `party(recruit, ...)`: the DM picks a **class** (the mechanical chassis: fighter, rogue, barbarian, monk, wizard, cleric, druid, bard, sorcerer), a flavor **title**, and designs the abilities for this setting:
  - **Spells**: browse the engine's *verified* catalog with `find_spells`, pick up to 3 cantrips + 4-5 leveled spells the companion can cast, and **rename** each (`magic-missile` can be "Plasma Darts" in a sci-fi game or "Séance Bolt" in a Victorian one). Mechanics stay the engine's; only the name is the DM's.
  - **Powers**: any class can get up to 2 invented gadget/ritual abilities (save ability, damage dice and type, 1-3 charges), budgeted like weapon powers.
- `spells_verified.json` is generated by `python verify_spells.py`, which casts every candidate spell in the engine and keeps only instant combat spells that really resolve (28 on dnd5e-engine 0.7.0). Concentration spells, buffs, summons and control spells are deliberately not offered. Re-run it after upgrading the engine.
- **Companion AI** each turn: heal the most hurt party member (below half HP) -> use a power -> cast the strongest spell (walking into range; area spells are never aimed through the player or other allies) -> fall back to cantrips -> weapon. Casters with no spells fight with weapons. Companion spell slots refresh at the start of each fight (HP persists).
- Spell lists and powers are kept out of the DM's context (the engine names each cast in the combat log); they appear in `/scene`.

## Stat blocks stored in Python (not context)
- Major NPCs (the antagonist, lieutenants, recurring rivals) get a fixed combat stat block stored in their entity, never in the prompt (the profile only says "stat block: stored"). The block is created the first time they fight: `start_encounter` with a custom monster NAMED after the NPC turns those numbers into their permanent stat block.
- From then on, fight them with `start_encounter foes=["Name"]` or by name again: the **stored numbers win** over whatever the model writes (no drift). Nested stat-block fields are deliberately not in the everyday entity tools, to save context.
- Wounds persist: after a fight each tracked NPC's remaining HP is saved; they recover fully after a full in-game day. The new `flee` action (`submit_action flee`) ends a fight with survivors keeping their wounds, no XP, no loot.
- Killing a tracked NPC marks them dead in the tracker (permanent) and their stat block can never fight again.

## Player commands
- `/scene`: full stats for the player, party members, spells, powers, and enemy health words (set `show_enemy_stats` for exact numbers). Not sent to the model.
- `/stats <name>`: a companion's sheet or an NPC's stored stat block. `/dmscene`: the compact view the DM actually receives.
- Tests: `python test_design.py`.
