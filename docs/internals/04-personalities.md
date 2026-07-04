# Internals: Personalities — templates, assignment, options

Developer reference for the personality subsystem of `mod-ollama-chat`. This is
the code-level companion to the operator-facing framing in
[../BOT-BEHAVIOR.md](../BOT-BEHAVIOR.md) Section 2 (personalities) and
[../BOT-ECONOMY.md](../BOT-ECONOMY.md) Section 1 (the `gear_give_chance` column). Where
those docs describe *what* the system does, this one walks the actual functions,
structs, globals, and DB probes so you can modify or debug them.

Sources documented here:
- `modules/mod-ollama-chat/src/mod-ollama-chat_personality.cpp`
- `modules/mod-ollama-chat/src/mod-ollama-chat_config.cpp` (the
  `LoadPersonality*` loaders + the personality globals)
- `modules/mod-ollama-chat/src/mod-ollama-chat_config.h` (the
  `BotPersonalityTemplate` struct)
- `modules/mod-ollama-chat/src/mod-ollama-chat_querymanager.h` (the
  `OllamaQueryOptions` struct)
- `personalities.sql` (repo root; the 42 custom rows)
- `modules/mod-ollama-chat/data/sql/characters/base/*.sql` (schema migrations)

---

## 1. Purpose

Every playerbot is tagged with one **personality key** (e.g. `LFG_SPAMMER`,
`STOIC_PALADIN`) drawn from a DB-backed template pool. The personality supplies
the prompt text that gives the bot its 2008-player voice, plus four behavior
knobs the rest of the chat pipeline reads: assignment `weight`,
`reply_chance_multiplier` (talkativeness), `num_predict_override` /
`temperature_override` (per-personality LLM sampling), and `gear_give_chance`
(economy, see BOT-ECONOMY Section 1). Assignment is rolled once, weight-proportionally,
and is **stable for the life of the character** — the template *pool* hot-reloads
but an individual bot's assignment never re-rolls at runtime.

## 2. Entry points & call graph

Nothing in this subsystem is self-triggering. There are exactly **two write
paths** (startup + reload) that fill the caches, and a set of **pull-based
readers** called synchronously while a reply prompt is being built.

**Startup (world thread, once):**
```
OllamaChatConfigWorldScript::OnStartup            (config.cpp:780)
  └─ LoadOllamaChatConfig()                        (config.cpp:382)
       └─ LoadPersonalityTemplatesFromDB()         (config.cpp:669)
            → fills g_PersonalityTemplates, g_PersonalityPrompts,
              g_PersonalityKeys, g_PersonalityKeysRandomOnly,
              g_PersonalityRandomTotalWeight
  └─ LoadBotPersonalityList()                       (config.cpp:310)
       → fills g_BotPersonalityList from mod_ollama_chat_personality
```

**Reload (world thread, console command `ollama reload`):**
```
OllamaChatConfigCommand::HandleOllamaReloadCommand  (command.cpp:50)
  ├─ sConfigMgr->Reload()
  ├─ LoadOllamaChatConfig()  → LoadPersonalityTemplatesFromDB()   (templates re-cached)
  ├─ if (!g_EnableRPPersonalities) ClearAllBotPersonalities()
  └─ LoadBotPersonalityList()                        (assignments re-loaded from DB)
```
Note: `ollama reload` reloads *templates and the assignment table*, but because
`GetBotPersonality` short-circuits on the in-memory map (below), a reload does
**not** re-roll live bots — it only re-reads whatever is already persisted.

**Per-reply readers (world thread, inside prompt construction):**
```
PlayerBotChatHandler::ProcessChat / GenerateBotPrompt  (handler.cpp)
  ├─ GetPersonalityReplyChanceMultiplier(GetBotPersonality(bot))   handler.cpp:1356
  │      → gates whether this bot replies at all
  ├─ GetBotPersonality(bot)                                        handler.cpp:2087/2179/2313
  │      └─ GetPersonalityPromptAddition(personality)              → prompt persona text
  └─ GetPersonalityQueryOptions(bot)                               handler.cpp:2326
         → OllamaQueryOptions passed by const reference into submitQuery / QueryOllamaAPI
```
The same three readers are called from the other prompt builders:
`mod-ollama-chat_random.cpp` (ambient chatter, lines 153/549/711),
`mod-ollama-chat_channels.cpp` (General/Trade, 336/392/411/435/513/527),
`mod-ollama-chat_events.cpp` (event chatter, 306/428), `_guildnames.cpp`
(recruitment lines, 126/141), `_groupjoin.cpp` (174). Manual admin paths live in
`mod-ollama-chat_command.cpp`: `.ollama personality get|set|list`
(`GetBotPersonality`, `SetBotPersonality`, `GetAllPersonalityKeys`,
`PersonalityExists`).

## 3. Function-by-function

### `void LoadPersonalityTemplatesFromDB()`  — config.cpp:669
Populates the template caches. Called from `LoadOllamaChatConfig()` (startup and
every `ollama reload`).

Steps:
1. Clears `g_PersonalityPrompts`, `g_PersonalityTemplates`, `g_PersonalityKeys`,
   `g_PersonalityKeysRandomOnly`, and resets `g_PersonalityRandomTotalWeight = 0`.
2. **`information_schema` probe #1 — `hasBehaviorColumns`**: counts columns named
   `weight` on `mod_ollama_chat_personality_templates` in `DATABASE()`. Non-zero
   ⇒ the `2026_07_02_personality_behavior_columns.sql` migration ran.
3. **`information_schema` probe #2 — `hasGearGiveColumn`**: same query for the
   `gear_give_chance` column (added later by
   `2026_07_03_personality_gear_give.sql`).
4. Picks the `SELECT` column list by capability:
   - behavior + gear:
     `` `key`,`prompt`,`manual_only`,`weight`,`reply_chance_multiplier`,`num_predict_override`,`temperature_override`,`gear_give_chance` ``
   - behavior only: same minus `gear_give_chance`
   - neither: `` `key`,`prompt`,`manual_only` `` (and a `LOG_WARN` telling the
     operator to source the behavior migration).
5. For each row, builds a `BotPersonalityTemplate tmpl` from `prompt` +
   `manual_only`, then, **only if `hasBehaviorColumns`**, fills the four behavior
   fields. Two null-handling quirks worth knowing:
   - `numPredictOverride` = `IsNull() ? 0 : Get<uint32_t>()` — SQL `NULL` maps to
     `0`, which downstream means *use the global* `g_OllamaNumPredict`.
   - `temperatureOverride` = `IsNull() ? -1.0f : Get<float>()` — SQL `NULL` maps
     to `-1.0f`, meaning *use the global* `g_OllamaTemperature`.
   - `replyChanceMultiplier` and `gearGiveChance` are clamped with
     `std::max(0.0f, …)` — a negative column value can never invert a roll.
6. Writes each row into `g_PersonalityPrompts[key]` (prompt only) **and**
   `g_PersonalityTemplates[key]` (full struct), and appends `key` to
   `g_PersonalityKeys`.
7. **Random pool + weight sum**: if `!manualOnly`, appends `key` to
   `g_PersonalityKeysRandomOnly` and adds `tmpl.weight` to
   `g_PersonalityRandomTotalWeight`. Manual-only personalities are cached and
   settable by hand but are excluded from random assignment and the weight total.
8. Logs `Cached N personalities (M available for random assignment, total
   weight W)`.

Inputs: `CharacterDatabase`. Outputs/side effects: the five personality globals.
No return value; on a completely empty/missing table it `LOG_ERROR`s and returns
early (caches left empty).

### `void LoadBotPersonalityList()`  — config.cpp:310
Loads the **assignments** (guid → key) from `mod_ollama_chat_personality` into
`g_BotPersonalityList`. Called at startup and on every reload.

1. Probes `information_schema.tables` for `mod_ollama_chat_personality` in
   `acore_characters`; if absent, `LOG_ERROR` and return (graceful degrade).
2. `SELECT guid,personality FROM mod_ollama_chat_personality`; empty result or
   zero rows ⇒ return.
3. Each row: `g_BotPersonalityList[guid] = personalityKey`. The guid is read
   straight from the DB column as a raw `uint64_t`
   (`result->Fetch()[0].Get<uint64_t>()`, config.cpp:338) — this loader does
   **not** call `GetRawValue()` itself; it just consumes the raw-value keys that
   `GetBotPersonality` writes (via `GetGUID().GetRawValue()`, personality.cpp:13).
   Map keys are raw GUID values, not counters.

This is the reason assignment "only loads at startup": at runtime
`GetBotPersonality` trusts `g_BotPersonalityList` and never re-queries per-bot, so
clearing rows requires a restart (or reload) to take effect.

### `std::string GetBotPersonality(Player* bot)`  — personality.cpp:11
The central read/assign function. Returns the personality key for `bot`,
assigning + persisting one on first sight.

```cpp
std::string GetBotPersonality(Player* bot)
```

Flow:
1. `botGuid = bot->GetGUID().GetRawValue()`.
2. **Cache hit** (`g_BotPersonalityList.find(botGuid)` found):
   - if `!g_EnableRPPersonalities`, overwrite the entry to `"default"` and return
     `"default"` (RP disabled forces everyone flat);
   - else return the stored key. This is the hot path for every already-known
     bot and is why assignments are stable for life.
3. **RP disabled or empty pool** (`!g_EnableRPPersonalities ||
   g_PersonalityKeysRandomOnly.empty()`): store and return `"default"`.
4. **Vestigial DB branch** (`if (g_BotPersonalityList.find(botGuid) != end())`,
   lines 40–57): this can never be true — step 2 already returned on any cache
   hit. It is dead code left from an earlier design; do not rely on it. (See
   Gotchas.)
5. **Weighted roll** (the real assignment):
   - default pick: uniform `g_PersonalityKeysRandomOnly[urand(0, size-1)]`;
   - if `g_PersonalityRandomTotalWeight > 0`, do a proper weighted draw:
     `roll = urand(1, g_PersonalityRandomTotalWeight)`, walk
     `g_PersonalityKeysRandomOnly` subtracting each key's
     `g_PersonalityTemplates[key].weight` until `roll <= w`. So a key with
     `weight 130` is 130/W likely; the uniform pick is only the fallback when all
     weights are 0.
   - stores the result in `g_BotPersonalityList[botGuid]`.
6. **Persistence**: probes `information_schema.tables` for
   `mod_ollama_chat_personality`; if missing, logs a notice; else
   `INSERT INTO mod_ollama_chat_personality (guid, personality) VALUES (…)`
   (raw interpolated query, not a prepared statement — acceptable because the key
   is a controlled identifier).
7. Returns the chosen key.

Side effects: mutates `g_BotPersonalityList`; one `CharacterDatabase` probe +
`Execute` on first assignment only. Uses `urand` (project RNG helper).

### `std::string GetPersonalityPromptAddition(const std::string& personality)`  — personality.cpp:98
```cpp
std::string GetPersonalityPromptAddition(const std::string& personality)
```
Looks up `g_PersonalityPrompts[personality]`; returns the prompt text, or
`g_DefaultPersonalityPrompt` (config key `OllamaChat.DefaultPersonalityPrompt`) if
the key is absent. Pure read; the persona string injected into every prompt.

### `bool SetBotPersonality(Player* bot, const std::string& personality)`  — personality.cpp:106
```cpp
bool SetBotPersonality(Player* bot, const std::string& personality)
```
Manual override (used by `.ollama personality set`). Returns `false` if `bot` is
null, or if `personality` is neither `"default"` nor present in
`g_PersonalityPrompts`. On success updates `g_BotPersonalityList[botGuid]` and
issues `REPLACE INTO mod_ollama_chat_personality (guid, personality) …` — note
**`REPLACE`**, so it overwrites an existing assignment (unlike the plain `INSERT`
in `GetBotPersonality`, which assumes the row is new).

### `std::vector<std::string> GetAllPersonalityKeys()`  — personality.cpp:134
Returns a copy of `g_PersonalityKeys` (all cached keys, manual-only included).
Backs `.ollama personality list`.

### `bool PersonalityExists(const std::string& personality)`  — personality.cpp:139
`true` for `"default"` or any key in `g_PersonalityPrompts`. Guards
`SetBotPersonality`.

### `void ClearAllBotPersonalities()`  — personality.cpp:146
`g_BotPersonalityList.clear()`. Called from the reload command when
`!g_EnableRPPersonalities`, so re-enabling RP later re-rolls everyone fresh from
whatever is (or isn't) reloaded from the DB.

### `float GetPersonalityReplyChanceMultiplier(const std::string& personality)`  — personality.cpp:155
Returns `g_PersonalityTemplates[personality].replyChanceMultiplier`, or `1.0f` if
the key is unknown. The caller (`handler.cpp:1356`, `random.cpp:153`) multiplies
the base reply chance by this and clamps to 100: `effChance = min(100,
chance*mult + 0.5f)`. `SILENT_TYPE` (0.3) rarely speaks; `LFG_SPAMMER`/
`GUILD_RECRUITER` (2.0) nearly always do.

### `OllamaQueryOptions GetPersonalityQueryOptions(Player* bot)`  — personality.cpp:163
```cpp
OllamaQueryOptions GetPersonalityQueryOptions(Player* bot)
```
Resolves the bot's personality (via `GetBotPersonality(bot)` — note this can
trigger assignment as a side effect) and copies the template's
`numPredictOverride` / `temperatureOverride` into a fresh `OllamaQueryOptions`.
Returns defaults (`{0, -1.0f}` = "use globals") when `bot` is null or the key is
missing. The struct is passed **by const reference** into `QueryOllamaAPI`
(querymanager.h:16) and `QueryManager::submitQuery` (querymanager.h:22); the
value copy that actually crosses the thread boundary is taken *afterward* inside
the query manager — the `QueryTask.opts` member, the `std::thread` argument copy,
and `processQuery`'s by-value `opts` parameter (querymanager.cpp:30/35/42/53).
Either way, `numPredict == 0` ⇒ fall back to `g_OllamaNumPredict` and
`temperature < 0` ⇒ fall back to `g_OllamaTemperature`.
This is how `ONE_WORD_GRUNTER` stays capped at 10 tokens and `STOIC_PALADIN` runs
at temperature 0.5 while everyone else uses the global sampling params.

## 4. Data structures & DB

### `struct BotPersonalityTemplate`  — config.h:105
```cpp
struct BotPersonalityTemplate
{
    std::string prompt;
    bool     manualOnly            = false;
    uint32_t weight                = 100;    // random-assignment weight
    float    replyChanceMultiplier = 1.0f;   // scales reply/chatter chance rolls
    uint32_t numPredictOverride    = 0;      // 0 = use global g_OllamaNumPredict
    float    temperatureOverride   = -1.0f;  // < 0 = use global g_OllamaTemperature
    float    gearGiveChance        = 2.0f;   // % chance per gear-context to mail the item
};
```
The in-memory defaults double as the graceful-degrade values: if
`hasBehaviorColumns` is false, every template keeps `weight 100`,
`replyChanceMultiplier 1.0`, no overrides, `gearGiveChance 2.0`.

### `struct OllamaQueryOptions`  — querymanager.h:11
```cpp
struct OllamaQueryOptions {
    uint32_t numPredictOverride  = 0;     // 0 = use global g_OllamaNumPredict
    float    temperatureOverride = -1.0f; // < 0 = use global g_OllamaTemperature
};
```
Per-call override carrier, copied by value into the query queue.

### Globals (declared `extern` in config.h, defined in config.cpp:109–117)
| global | type | filled by | meaning |
|---|---|---|---|
| `g_BotPersonalityList` | `unordered_map<uint64_t,string>` | `LoadBotPersonalityList`, `GetBotPersonality`, `SetBotPersonality` | live guid→key assignments |
| `g_PersonalityPrompts` | `unordered_map<string,string>` | `LoadPersonalityTemplatesFromDB` | key→prompt text |
| `g_PersonalityTemplates` | `unordered_map<string,BotPersonalityTemplate>` | same | key→full behavior struct |
| `g_PersonalityKeys` | `vector<string>` | same | all keys (manual + random) |
| `g_PersonalityKeysRandomOnly` | `vector<string>` | same | non-`manual_only` keys, the random pool |
| `g_PersonalityRandomTotalWeight` | `uint32_t` | same | Σ weights of the random pool |
| `g_DefaultPersonalityPrompt` | `string` | `LoadOllamaChatConfig` | fallback prompt |
| `g_GearGiveBotCooldownMin` / `g_GearGivePairCooldownMin` | `uint32_t` | `LoadOllamaChatConfig` | economy cooldowns (BOT-ECONOMY Section 1) |

### DB tables (all in `acore_characters`)
- **`mod_ollama_chat_personality`** — assignments. Schema (`2025_05_30_personalities.sql`):
  `guid BIGINT PK`, `personality VARCHAR(64)`. Read by `LoadBotPersonalityList`;
  written by `GetBotPersonality` (`INSERT`) and `SetBotPersonality` (`REPLACE`).
- **`mod_ollama_chat_personality_templates`** — the pool. Column history:
  - `2025_05_31_personality_template.sql`: `key VARCHAR(64) PK`, `prompt TEXT`
    (+ the original 33 upstream rows).
  - `2025_11_01_personality_manual_only.sql`: `manual_only TINYINT(1) DEFAULT 0`.
  - `2026_07_02_personality_behavior_columns.sql`: `weight INT UNSIGNED DEFAULT
    100`, `reply_chance_multiplier FLOAT DEFAULT 1.0`, `num_predict_override INT
    UNSIGNED NULL`, `temperature_override FLOAT NULL`.
  - `2026_07_03_personality_playstyle.sql`: `playstyle VARCHAR(16) DEFAULT
    'default'` (read by **mod-playerbots**, not this module — see BOT-BEHAVIOR Section 3).
  - `2026_07_03_personality_gear_give.sql`: `gear_give_chance FLOAT DEFAULT 2.0`
    (+ creates `mod_ollama_chat_pending_gives`, see BOT-ECONOMY Section 2).

  Read by `LoadPersonalityTemplatesFromDB`. The chat module does **not** read the
  `playstyle` column — its full behavior+gear `SELECT` lists eight columns
  (through `gear_give_chance`, config.cpp:698; the behavior-only `SELECT` stops at
  `temperature_override` for seven, config.cpp:699); `playstyle` is joined
  directly out of the shared table by mod-playerbots.

### `personalities.sql` seed (repo root)
Not a module migration — applied by hand / `reset-world.sh`
(`sudo mariadb acore_characters < personalities.sql` then `ollama reload`).
Contents:
- `DELETE` + `INSERT` of **40 WotLK-2008 archetypes** (`LFG_SPAMMER` … 
  `FAIRWEATHER_FRIEND`), then a second `DELETE`/`INSERT` for `EGIRL` and
  `ELITE_ARENA_PVPER` — **42 custom rows** on top of the upstream 33 = **75 total**.
  All are `manual_only = 0`, so all 75 are random-assignable.
- Six `UPDATE … SET playstyle = …` blocks mapping the custom keys onto
  grinder/quester/socializer/explorer/pvper/idler (unlisted stays `default`).
- Seven `UPDATE … SET gear_give_chance = …` blocks (15 → 0) for the custom keys
  (unlisted keeps the column default 2.0).
- Two trailing `SELECT`s (`total_personalities`; per-`playstyle` counts) purely
  for eyeballing the result after sourcing.

Illustrative behavior values from the seed: `LFG_SPAMMER` (weight 130, mult 2.0,
num_predict 30), `ONE_WORD_GRUNTER` (weight 50, mult 0.6, num_predict 10, temp
0.7), `STOIC_PALADIN` (weight 50, mult 0.4, temp 0.5), `UNHINGED_TROLL` (weight
30, mult 1.3, temp 1.3).

## 5. Concurrency & threading

**All personality caches are lock-free.** Unlike sentiment
(`g_SentimentMutex`) and conversation history (`g_ConversationHistoryMutex`),
none of `g_BotPersonalityList`, `g_PersonalityTemplates`, `g_PersonalityPrompts`,
`g_PersonalityKeys`, or `g_PersonalityKeysRandomOnly` is mutex-guarded.

Safety rests on **world-thread confinement of the writers**:
- The only writers are `LoadPersonalityTemplatesFromDB`, `LoadBotPersonalityList`,
  `ClearAllBotPersonalities` (startup / reload command — world thread) and the
  lazy `INSERT` branch of `GetBotPersonality` / `SetBotPersonality`.
- The normal per-reply readers in `handler.cpp`, `random.cpp`, `channels.cpp`,
  `guildnames.cpp`, `groupjoin.cpp` resolve the personality **synchronously on
  the world thread** while building the prompt, then hand the resulting
  `std::string` / `OllamaQueryOptions` **by const reference** into `submitQuery` /
  `QueryOllamaAPI`; the query manager then value-copies both across to the
  detached Ollama worker thread (`QueryTask.opts` / the `std::thread` argument
  copy / `processQuery`'s by-value params). The worker never touches the
  personality maps — it only sees the copied prompt string and options struct. This mirrors the module's general rule
  (see BOT-ECONOMY Section 4): resolve everything on the world thread, capture raw GUIDs
  and value-copied data into the `std::thread`, reacquire `Player*` via
  `ObjectAccessor::FindPlayer` inside the worker.

**Known exception / latent race**: `mod-ollama-chat_events.cpp:306` calls
`GetPersonalityQueryOptions(botPtr)` *inside* the detached worker thread (the
lambda spawned at events.cpp:296). `GetPersonalityQueryOptions` → 
`GetBotPersonality` reads `g_BotPersonalityList`/`g_PersonalityTemplates`
unlocked, and for a never-seen bot could even take the assign+`INSERT` write
path off the world thread. In practice this is benign because after the normal
`reset-world.sh` startup every live bot already has an entry (all templates
loaded before the first bot logs in, per BOT-BEHAVIOR Section 2), so the maps are
effectively read-only at that point and concurrent reads are safe. It is still a
real unsynchronized access — if you add worker-thread callers or a runtime path
that assigns new personalities under load, add a mutex or move the resolution
back onto the world thread.

## 6. Config keys

All read via `sConfigMgr->GetOption<T>` in `LoadOllamaChatConfig()`
(config.cpp:382); loaded at startup and re-read on `ollama reload`.

| key | default | effect |
|---|---|---|
| `OllamaChat.EnableRPPersonalities` | `false` | master switch. When false, `GetBotPersonality` forces `"default"` for every bot and the reload command calls `ClearAllBotPersonalities()`. |
| `OllamaChat.DefaultPersonalityPrompt` | `""` | prompt returned by `GetPersonalityPromptAddition` for `"default"` / unknown keys. |
| `OllamaChat.GearGiveBotCooldownMin` | `30` | economy cooldown, stored in `g_GearGiveBotCooldownMin` (BOT-ECONOMY Section 1). |
| `OllamaChat.GearGivePairCooldownMin` | `1440` | economy cooldown, `g_GearGivePairCooldownMin`. |
| `OllamaChat.DebugEnabled` | `false` | gates the `[Ollama Chat]` personality assignment/lookup `LOG_INFO` lines. |

Global sampling knobs the per-personality overrides fall back to (also in
`LoadOllamaChatConfig`): `OllamaChat.NumPredict` (default `40` →
`g_OllamaNumPredict`) and `OllamaChat.Temperature` (default `0.8f` →
`g_OllamaTemperature`). The behavior columns themselves are **not** config keys —
they live in the DB template table and are tuned in `personalities.sql`.

## 7. Failure modes & gotchas

- **Missing behavior columns** — `LoadPersonalityTemplatesFromDB` `information_
  schema`-probes for `weight` and `gear_give_chance` independently. An older DB
  (no `2026_07_02` migration) loads only `key,prompt,manual_only`, logs a
  `LOG_WARN`, and every template keeps its struct defaults (weight 100, mult 1.0,
  no overrides, gearGiveChance 2.0). A DB with behavior but not gear columns
  loads seven columns and gear stays at the 2.0 default. The binary always runs.
- **Missing assignment table** — `LoadBotPersonalityList` and the
  `GetBotPersonality` persistence branch both probe `information_schema.tables`
  for `mod_ollama_chat_personality`; absent ⇒ log + skip the write. Bots still
  get an in-memory personality; it just isn't persisted across restarts.
- **Empty template pool** — if the template table is empty/missing,
  `LoadPersonalityTemplatesFromDB` `LOG_ERROR`s and leaves the caches empty;
  `GetBotPersonality` then hits the `g_PersonalityKeysRandomOnly.empty()` guard
  and returns `"default"` for everyone.
- **Dead DB branch in `GetBotPersonality`** (lines 40–57) — guarded by
  `g_BotPersonalityList.find(botGuid) != end()`, which is unreachable because the
  identical `find` at the top of the function already returned on any hit. It
  reads like a "load from DB" path but never executes; the real DB load is
  `LoadBotPersonalityList` at startup. Don't add logic assuming it runs.
- **`ollama reload` does not re-roll** — it reloads the template pool and re-reads
  the assignment table, but live bots short-circuit on the in-memory map, so
  existing characters keep their key. Re-rolling requires clearing
  `mod_ollama_chat_personality` rows **and** a restart (or reload after clear).
  `reset-world.sh` sidesteps this by loading all 75 templates before the first
  bot logs in, so first-login rolls already see the full pool.
- **`INSERT` vs `REPLACE`** — `GetBotPersonality` uses `INSERT` (assumes a new
  guid, consistent with only reaching that branch when the bot is absent from the
  in-memory map); `SetBotPersonality` uses `REPLACE` (overwrite). A guid present
  in the DB but somehow absent from memory would make the `INSERT` fail on the PK;
  the normal startup load prevents that mismatch.
- **NULL sentinels** — `num_predict_override` NULL → `0`, `temperature_override`
  NULL → `-1.0f`; both mean "use the global." Setting the column to `0` / a
  negative value has the same effect as NULL, so you can't force a literal
  zero-token or negative-temperature request through this path.
- **RP disabled mid-life** — flipping `EnableRPPersonalities` off then reloading
  clears assignments and forces `"default"`; flipping it back on re-rolls fresh.
  Assignments are not preserved across the disable.
- **Weighted-roll edge** — `g_PersonalityRandomTotalWeight == 0` (all weights 0,
  or no random pool) falls back to a uniform `urand` pick over
  `g_PersonalityKeysRandomOnly`; if that vector is also empty the function returns
  `"default"` before rolling.

## 8. Cross-references

- [../BOT-BEHAVIOR.md](../BOT-BEHAVIOR.md) — Section 2 personalities (operator view),
  Section 3 the `playstyle` column consumed by mod-playerbots' New RPG strategy, Section 9
  verification/SQL cookbook.
- [../BOT-ECONOMY.md](../BOT-ECONOMY.md) — Section 1 `gear_give_chance` gating and the
  mail path, Section 2 `mod_ollama_chat_pending_gives`, Section 4 the
  `OllamaChat_SpeakSituation` cross-module hook that also calls
  `GetPersonalityQueryOptions`.
- Prompt assembly + `GenerateGearContext` (where `GetBotPersonality` /
  `GetPersonalityPromptAddition` / `GetPersonalityQueryOptions` are consumed):
  `mod-ollama-chat_handler.cpp`.
- Sentiment subsystem (the other guid-keyed, but mutex-guarded, per-bot state):
  `mod-ollama-chat_sentiment.cpp`, BOT-BEHAVIOR Section 4.
