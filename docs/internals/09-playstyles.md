# Internals 09 — Personality-driven playstyles

Developer reference for the code that turns the New RPG activity roll into a
**per-personality** decision. This is the deep counterpart to
[BOT-BEHAVIOR Section 3](../BOT-BEHAVIOR.md) — that section frames *what* the feature
does for an operator; this doc explains *how the code works*, function by
function, for someone about to modify or debug it.

Authoritative sources:
- `modules/mod-playerbots/src/Ai/World/Rpg/Action/NewRpgBaseAction.cpp`
  (`GetBotPlaystyle`, `RandomChangeStatus`, `CheckRpgStatusAvailable`)
- `modules/mod-playerbots/src/PlayerbotAIConfig.cpp` (`Initialize()` — weight
  table + profile loading)
- `modules/mod-playerbots/src/PlayerbotAIConfig.h` (`NewRpgStatus` enum,
  `RpgStatusProbWeight`, `RpgStatusProbWeightProfiles`)
- `modules/mod-ollama-chat/data/sql/characters/base/2026_07_03_personality_playstyle.sql`
  (the `playstyle` column + upstream-33 mapping)

---

## 1. Purpose

When `AiPlayerbot.EnableNewRpgStrategy = 1`, an idle bot periodically rolls its
next activity (quest / grind / camp / wander / fly / rest / world-PvP) from a
**weighted** table. Upstream ships a single global weight table shared by all
bots. This subsystem makes that roll **per-bot**: it looks up the bot's
mod-ollama-chat personality, maps it to one of six *playstyle* weight profiles
(`grinder`, `quester`, `socializer`, `explorer`, `pvper`, `idler`), and rolls
from that profile's weights instead — so a bot's personality decides what it
does all day, not just how it talks. Bots without a resolvable playstyle fall
back to the global table, so the feature degrades cleanly to vanilla behaviour.

---

## 2. Entry points & call graph

Execution enters through the ordinary Playerbots strategy/action tick — there
is no dedicated timer or hook. `NewRpgStrategy` installs one default action; it
fires whenever the bot AI updates non-combat strategies (gated by
`AiPlayerbot.RpgDelay`), and only actually rolls when the bot's RPG state
machine is in `RPG_IDLE`.

```
map-update thread  (PlayerbotAI non-combat tick)
  NewRpgStrategy::getDefaultActions()                         [NewRpgStrategy.cpp:10]
    NextAction("new rpg status update", 11.0f)
      NewRpgStatusUpdateAction::Execute(Event)                [NewRpgAction.cpp:58]
        └─ status == RPG_IDLE ?
             NewRpgBaseAction::RandomChangeStatus(            [NewRpgBaseAction.cpp:1125]
               {RPG_GO_CAMP, RPG_GO_GRIND, RPG_WANDER_RANDOM,
                RPG_WANDER_NPC, RPG_DO_QUEST, RPG_TRAVEL_FLIGHT,
                RPG_REST, RPG_OUTDOOR_PVP})                    // 8 candidates, no RPG_IDLE
               ├─ GetBotPlaystyle(bot)                        [anon ns, NewRpgBaseAction.cpp:1083]
               │    ├─ (once) information_schema column probe → hasPlaystyleColumn
               │    ├─ playstyleCache lookup   (playstyleCacheMutex)
               │    └─ CharacterDatabase.Query  personality ⨝ templates.playstyle
               ├─ select weight table:
               │    RpgStatusProbWeightProfiles[playstyle]  (profile hit)
               │    else RpgStatusProbWeight                 (default / miss)
               ├─ weightOf(status) lambda  (0 ⇒ candidate skipped)
               ├─ CheckRpgStatusAvailable(status)            [NewRpgBaseAction.cpp:1273]
               │    └─ SelectRandomGrindPos / SelectRandomCampPos /
               │       SelectRandomFlightTaxiNode / quest-POI scan / OutdoorPvP probe
               ├─ urand(1, probSum) → cumulative weighted pick → chosenStatus
               ├─ LOG_DEBUG "[Playstyle] Bot {} (profile '{}') rolled status {}"
               └─ switch(chosenStatus) → botAI->rpgInfo.ChangeTo*()   // commits next activity
```

Other RPG statuses (`RPG_GO_GRIND`, `RPG_WANDER_RANDOM`, …) persist for a
fixed duration and then call `info.ChangeToIdle()`, which returns the bot to
`RPG_IDLE` so the **next** `NewRpgStatusUpdateAction::Execute` re-rolls. That
is the roll cadence: one `RandomChangeStatus` per completed activity cycle, not
one per tick.

---

## 3. Function-by-function

### 3.1 `GetBotPlaystyle(Player* bot)` — anonymous namespace, `NewRpgBaseAction.cpp:1083`

```cpp
namespace
{
    std::mutex playstyleCacheMutex;
    std::unordered_map<ObjectGuid::LowType, std::pair<std::string, uint32>> playstyleCache;

    std::string GetBotPlaystyle(Player* bot)
}
```

Resolves a bot's playstyle string (the profile-map key) from its chat
personality via the shared characters DB. File-local (anonymous namespace) —
not a class member, not exported. Steps:

1. **One-time schema probe.** `static int hasPlaystyleColumn = -1`. On first
   call it runs:
   ```sql
   SELECT 1 FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE()
   AND TABLE_NAME = 'mod_ollama_chat_personality_templates' AND COLUMN_NAME = 'playstyle'
   ```
   Result present ⇒ `hasPlaystyleColumn = 1` and it logs, exactly once,
   `LOG_INFO("playerbots", "[Playstyle] personality playstyle column found - per-personality RPG profiles active")`.
   Absent ⇒ `0`. **If 0, the function returns `""` immediately on every call** —
   the JOIN below is never attempted, so a DB without the chat module's
   migration (or without the chat module at all) transparently falls back to
   the global weight table.
2. **Cache read.** Key is `bot->GetGUID().GetCounter()` (an
   `ObjectGuid::LowType`, i.e. the guid *counter* — same convention as the
   sentiment/gear tables). Under `playstyleCacheMutex`: return the cached
   string if it is either **non-empty** (a real profile — cached for the whole
   session, never re-queried) **or** younger than `5 * MINUTE * IN_MILLISECONDS`
   (an empty result inside its retry window). `now` is `getMSTime()`.
3. **DB lookup.** Cache miss / expired-empty:
   ```sql
   SELECT t.playstyle FROM mod_ollama_chat_personality p
   JOIN mod_ollama_chat_personality_templates t ON t.`key` = p.personality
   WHERE p.guid = {}
   ```
   (`{}` = the guid counter.) One row → `playstyle = (*result)[0].Get<std::string>()`.
4. **Normalise.** `playstyle == "default"` is cleared to `""` (the column
   default and the "balanced mix" value both mean *use the global table*). A
   non-empty result logs `LOG_DEBUG("playerbots", "[Playstyle] Bot {} uses RPG profile '{}'")`.
5. **Cache write & return.** Under the mutex again: `playstyleCache[guid] = {playstyle, now}`; return `playstyle`.

**Inputs:** live `Player*` (used only synchronously, on the calling thread — no
pointer is stored). **Output:** profile key (`""` for default/none). **Side
effects:** two static caches (`hasPlaystyleColumn`, `playstyleCache`), two
synchronous `CharacterDatabase.Query` calls at most (probe once; join at most
once per bot per 5 min), log lines.

Non-obvious: a bot whose personality maps to `default` (or has no personality
row yet) returns `""` and is therefore **re-queried every 5 minutes for the
whole session** — the empty-retry branch exists precisely so bots that get a
personality assigned *after* login still pick up a profile mid-session. A bot
with a real profile is queried **once** and cached permanently.

### 3.2 `NewRpgBaseAction::RandomChangeStatus(std::vector<NewRpgStatus> candidateStatus)` — `NewRpgBaseAction.cpp:1125`

```cpp
bool NewRpgBaseAction::RandomChangeStatus(std::vector<NewRpgStatus> candidateStatus)
```

The heart of the subsystem. Given the caller's candidate statuses, picks one by
weighted random draw and commits the bot to it. Steps:

1. **Resolve the weight table.**
   ```cpp
   std::string playstyle = GetBotPlaystyle(bot);
   auto profileIt = sPlayerbotAIConfig.RpgStatusProbWeightProfiles.find(playstyle);
   auto const& statusWeights =
       (!playstyle.empty() && profileIt != sPlayerbotAIConfig.RpgStatusProbWeightProfiles.end())
       ? profileIt->second
       : sPlayerbotAIConfig.RpgStatusProbWeight;
   ```
   Non-empty playstyle that exists in the profile map ⇒ that profile's table;
   otherwise (empty, or an **unrecognised** playstyle string) ⇒ the global
   table. `statusWeights` is a `const&` — no copy.
2. **Weight lambda.**
   ```cpp
   auto weightOf = (NewRpgStatus status)(NewRpgStatus status) -> uint32
   {
       auto it = statusWeights.find(status);
       return it != statusWeights.end() ? it->second : 0;
   };
   ```
   Missing key ⇒ weight `0`. Captures `statusWeights` by reference.
3. **Filter candidates & sum weights.** For each `status` in `candidateStatus`:
   skip if `weightOf(status) == 0` (a zero weight removes the status regardless
   of availability); otherwise call `CheckRpgStatusAvailable(status)` and, if
   available, push to `availableStatus` and add its weight to `probSum`.
4. **Degenerate guard.** If `availableStatus.empty() || probSum == 0`:
   `botAI->rpgInfo.ChangeToRest()`, `bot->SetStandState(UNIT_STAND_STATE_SIT)`,
   `return true`. (This is why a bot with all-zero weights sits and rests
   rather than doing nothing.)
5. **Weighted draw.**
   ```cpp
   uint32 rand = urand(1, probSum);      // inclusive [1, probSum]
   uint32 accumulate = 0;
   NewRpgStatus chosenStatus = RPG_STATUS_END;
   for (NewRpgStatus status : availableStatus)
   {
       accumulate += weightOf(status);
       if (accumulate >= rand) { chosenStatus = status; break; }
   }
   ```
6. **Debug line** (the distribution-verification hook):
   ```cpp
   LOG_DEBUG("playerbots", "[Playstyle] Bot {} (profile '{}') rolled status {}",
             bot->GetName(), playstyle.empty() ? "default" : playstyle,
             static_cast<int>(chosenStatus));
   ```
   The status is logged as the **integer enum value**, not a name — map it via
   the `NewRpgStatus` enum in Section 4 (`1 = GoGrind`, `5 = DoQuest`, `8 = OutdoorPvp`,
   …). Counting these lines by `(profile, int)` is the canonical way to verify a
   profile's distribution (see [BOT-BEHAVIOR Section 9](../BOT-BEHAVIOR.md)).
7. **Commit.** `switch (chosenStatus)` maps each status to the matching
   `botAI->rpgInfo.ChangeTo*()` transition and returns:

   | chosenStatus | action | returns |
   |---|---|---|
   | `RPG_WANDER_RANDOM` | `rpgInfo.ChangeToWanderRandom()` | `true` |
   | `RPG_WANDER_NPC` | `rpgInfo.ChangeToWanderNpc()` | `true` |
   | `RPG_GO_GRIND` | `SelectRandomGrindPos(bot)`; if non-empty `ChangeToGoGrind(pos)` → `true`, else `false` |
   | `RPG_GO_CAMP` | `SelectRandomCampPos(bot)`; if non-empty `ChangeToGoCamp(pos)` → `true`, else `false` |
   | `RPG_DO_QUEST` | scan quest log for a quest with a reachable POI (`GetQuestPOIPosAndObjectiveIdx(..., true)`, skipping `botAI->lowPriorityQuest`), pick one at random → `ChangeToDoQuest(questId, quest)` → `true`, else `false` |
   | `RPG_TRAVEL_FLIGHT` | `SelectRandomFlightTaxiNode(...)`; on success `ChangeToTravelFlight(entry, pos, path)` → `true`, else `false` |
   | `RPG_IDLE` | `rpgInfo.ChangeToIdle()` → `true` |
   | `RPG_REST` | `rpgInfo.ChangeToRest()` + `SetStandState(UNIT_STAND_STATE_SIT)` → `true` |
   | `RPG_OUTDOOR_PVP` | `rpgInfo.ChangeToOutdoorPvp()` → `true` |
   | `default:` | `ChangeToRest()` + sit → `true` |

**Inputs:** candidate status list (the RPG_IDLE caller passes 8 statuses,
excluding `RPG_IDLE` itself). **Output:** `bool` — `true` when the bot's RPG
state was changed; `false` when the chosen status could not actually be entered
(e.g. `GO_GRIND` chosen but `SelectRandomGrindPos` returned empty *this* time —
the strategy re-rolls next tick). **Side effects:** mutates `botAI->rpgInfo`
(the bot's RPG state machine), possibly `bot` stand state, and emits the debug
line.

Non-obvious: the same selection helpers (`SelectRandomGrindPos`,
`SelectRandomCampPos`, `SelectRandomFlightTaxiNode`, the quest-POI scan) run
**twice** for a chosen status — once in `CheckRpgStatusAvailable` (the filter)
and again here (the commit). They are randomized each call, so the commit can
fail even though the availability check just passed. This double-run is also
why "select random grind pos" log lines fire during availability checks and are
not a reliable per-bot behaviour count.

### 3.3 `NewRpgBaseAction::CheckRpgStatusAvailable(NewRpgStatus status)` — `NewRpgBaseAction.cpp:1273`

```cpp
bool NewRpgBaseAction::CheckRpgStatusAvailable(NewRpgStatus status)
```

Availability gate; a status only participates in the draw if this returns
`true`. Per-status:

- `RPG_IDLE`, `RPG_REST` → always `true`.
- `RPG_WANDER_RANDOM` → `AI_VALUE(Unit*, "grind target") != nullptr`.
- `RPG_GO_GRIND` → `SelectRandomGrindPos(bot) != WorldPosition()`.
- `RPG_GO_CAMP` → `SelectRandomCampPos(bot) != WorldPosition()`.
- `RPG_WANDER_NPC` → `AI_VALUE(GuidVector, "possible new rpg targets").size() >= 3`.
- `RPG_DO_QUEST` → any quest slot (excluding `botAI->lowPriorityQuest`) has a
  reachable POI via `GetQuestPOIPosAndObjectiveIdx(questId, poiInfo, true)`.
- `RPG_TRAVEL_FLIGHT` → `SelectRandomFlightTaxiNode(...)` succeeds.
- `RPG_OUTDOOR_PVP` → `bot->IsPvP()` **and** zone `!= AREA_NAGRAND` **and**
  `sOutdoorPvPMgr->GetOutdoorPvPToZoneId(zoneId) != nullptr`.
- `default:` → `false`.

Non-obvious: this is why the `pvper` profile's large `OutdoorPvp` weight
"disappears" at low level — a non-PvP-flagged bot, or one not standing in an
outdoor-PvP zone, fails this check, `RPG_OUTDOOR_PVP` is dropped from
`availableStatus`, and its weight is simply absent from `probSum`. The
remaining statuses' relative weights therefore renormalise automatically; there
is no explicit redistribution code.

### 3.4 Profile-table construction — `PlayerbotAIConfig::Initialize()`, `PlayerbotAIConfig.cpp:678`

```cpp
RpgStatusProbWeight[RPG_WANDER_RANDOM] = sConfigMgr->GetOption<int32>("AiPlayerbot.RpgStatusProbWeight.WanderRandom", 15);
RpgStatusProbWeight[RPG_WANDER_NPC]    = sConfigMgr->GetOption<int32>("AiPlayerbot.RpgStatusProbWeight.WanderNpc", 20);
RpgStatusProbWeight[RPG_GO_GRIND]      = sConfigMgr->GetOption<int32>("AiPlayerbot.RpgStatusProbWeight.GoGrind", 15);
RpgStatusProbWeight[RPG_GO_CAMP]       = sConfigMgr->GetOption<int32>("AiPlayerbot.RpgStatusProbWeight.GoCamp", 10);
RpgStatusProbWeight[RPG_DO_QUEST]      = sConfigMgr->GetOption<int32>("AiPlayerbot.RpgStatusProbWeight.DoQuest", 60);
RpgStatusProbWeight[RPG_TRAVEL_FLIGHT] = sConfigMgr->GetOption<int32>("AiPlayerbot.RpgStatusProbWeight.TravelFlight", 15);
RpgStatusProbWeight[RPG_REST]          = sConfigMgr->GetOption<int32>("AiPlayerbot.RpgStatusProbWeight.Rest", 5);
RpgStatusProbWeight[RPG_OUTDOOR_PVP]   = sConfigMgr->GetOption<int32>("AiPlayerbot.RpgStatusProbWeight.OutdoorPvp", 10);
```

Then the six profiles are built from a local table and, for each, every status
weight is read from config with the built-in default as fallback:

```cpp
struct ProfileDefault { char const* name; char const* confName; uint32 w[RPG_STATUS_END]; };
// weight order:            IDLE GRIND CAMP WRAND WNPC QUEST FLIGHT REST PVP
ProfileDefault const profiles[] = {
    {"grinder",    "Grinder",    {0, 70, 15,  5,  5, 25, 10,  5,  5}},
    {"quester",    "Quester",    {0,  5,  5,  5, 15, 90, 15,  5,  5}},
    {"socializer", "Socializer", {0,  5, 15, 10, 60, 25, 15, 15,  5}},
    {"explorer",   "Explorer",   {0, 10, 10, 50, 15, 30, 40,  5,  5}},
    {"pvper",      "Pvper",      {0, 15,  5, 10, 15, 30, 10,  5, 60}},
    {"idler",      "Idler",      {0,  5, 20, 10, 25, 15,  5, 40,  5}},
};
char const* statusNames[RPG_STATUS_END] = {"Idle", "GoGrind", "GoCamp", "WanderRandom",
                                           "WanderNpc", "DoQuest", "TravelFlight", "Rest", "OutdoorPvp"};
for (ProfileDefault const& prof : profiles)
{
    std::unordered_map<NewRpgStatus, uint32>& weights = RpgStatusProbWeightProfiles[prof.name];
    for (int s = RPG_GO_GRIND; s < RPG_STATUS_END; ++s)
        weights[static_cast<NewRpgStatus>(s)] = sConfigMgr->GetOption<int32>(
            std::string("AiPlayerbot.RpgStatusProbWeight.") + prof.confName + "." + statusNames[s], prof.w[s]);
}
```

Non-obvious details:
- The inner loop starts at `RPG_GO_GRIND` (index 1), so `RPG_IDLE` (index 0) is
  **never inserted** into a profile map — `weightOf(RPG_IDLE)` returns 0 for
  profile bots. Harmless, because `RPG_IDLE` is never in the candidate list. The
  `w[0] = 0` column in every profile row is likewise dead.
- The global `RpgStatusProbWeight` table also has no `RPG_IDLE` entry, for the
  same reason.
- Config keys are assembled by string concatenation:
  `AiPlayerbot.RpgStatusProbWeight.<confName>.<statusName>`, e.g.
  `AiPlayerbot.RpgStatusProbWeight.Grinder.GoGrind`. The map key stored in
  `RpgStatusProbWeightProfiles` is the lowercase `name` ("grinder"), which is
  exactly what `GetBotPlaystyle` returns — the two must stay in sync.

### 3.5 Selection helpers (called from both the gate and the commit)

Documented in full elsewhere in `NewRpgBaseAction.cpp`; relevant here only as
the availability/commit backends whose randomness makes the roll non-idempotent:

- `WorldPosition NewRpgBaseAction::SelectRandomGrindPos(Player* bot)` — random
  grind spot from `sTravelMgr.GetLocsPerLevelCache(level)`; empty `WorldPosition`
  if none in range.
- `WorldPosition NewRpgBaseAction::SelectRandomCampPos(Player* bot)` — random
  travel-hub/innkeeper position from `sTravelMgr.GetTravelHubs(bot)`.
- `bool NewRpgBaseAction::SelectRandomFlightTaxiNode(uint32&, WorldPosition&, std::vector<uint32>&)`
  — nearest flight master + a random known destination path.
- `bool NewRpgBaseAction::GetQuestPOIPosAndObjectiveIdx(uint32 questId, std::vector<POIInfo>&, bool toComplete)`
  — resolves reachable quest POIs for the DoQuest path.

---

## 4. Data structures & DB

### Enum `NewRpgStatus` (`PlayerbotAIConfig.h:50`)

```cpp
enum NewRpgStatus : int
{
    RPG_IDLE = 0, RPG_GO_GRIND = 1, RPG_GO_CAMP = 2,
    RPG_WANDER_RANDOM = 3, RPG_WANDER_NPC = 4, RPG_DO_QUEST = 5,
    RPG_TRAVEL_FLIGHT = 6, RPG_REST = 7, RPG_OUTDOOR_PVP = 8,
    RPG_STATUS_END = 9
};
```
`RPG_STATUS_END` doubles as the array length for the per-profile weight rows.
The integer values are what the `[Playstyle] ... rolled status N` debug line
prints.

### Config-owned weight tables (`PlayerbotAIConfig.h:373`)

```cpp
std::unordered_map<NewRpgStatus, uint32> RpgStatusProbWeight;                 // global / default
std::unordered_map<std::string,
    std::unordered_map<NewRpgStatus, uint32>> RpgStatusProbWeightProfiles;    // playstyle -> weights
```
Both are populated once in `Initialize()` and thereafter read-only at runtime.
`RpgStatusProbWeightProfiles` is keyed by the six lowercase profile names.

### File-local caches (`NewRpgBaseAction.cpp:1080`)

```cpp
std::mutex playstyleCacheMutex;
std::unordered_map<ObjectGuid::LowType, std::pair<std::string, uint32>> playstyleCache;
static int hasPlaystyleColumn = -1;   // -1 unprobed, 0 absent, 1 present
```
`playstyleCache` maps guid counter → `{playstyleString, lastQueryMs}`.

### DB (all in `acore_characters`, owned by mod-ollama-chat)

| table | columns read | used by |
|---|---|---|
| `information_schema.COLUMNS` | `TABLE_SCHEMA`, `TABLE_NAME`, `COLUMN_NAME` | one-time capability probe in `GetBotPlaystyle` |
| `mod_ollama_chat_personality` | `guid`, `personality` (aliased `p`) | JOIN left side; `guid` = bot guid counter |
| `mod_ollama_chat_personality_templates` | `` `key` ``, `playstyle` (aliased `t`) | JOIN right side; supplies the profile string |

The `playstyle` column is `VARCHAR(16) NOT NULL DEFAULT 'default'`, added by
`2026_07_03_personality_playstyle.sql` (which also maps the upstream 33
personalities). This subsystem **only reads** these tables; it never writes
them. No mod-playerbots table is involved.

---

## 5. Concurrency & threading

- **Where it runs.** `RandomChangeStatus` (and therefore `GetBotPlaystyle`) runs
  synchronously inside the bot's AI update, on a **map-update thread**. There is
  no detached worker thread in this subsystem — every DB query here is a
  **blocking** `CharacterDatabase.Query` on that thread. This is acceptable only
  because the queries are heavily cached: the schema probe runs once per process;
  the personality JOIN runs at most once per bot (permanently cached on success)
  or once per 5 minutes (empty/default result).
- **Why the mutex.** AzerothCore updates maps with a thread pool, so bots on
  different maps can enter `GetBotPlaystyle` concurrently. `playstyleCache` and
  `playstyleCacheMutex` guard the shared `unordered_map` against concurrent
  read/insert. Both the cache-read and cache-write critical sections take the
  lock; the DB query itself runs **outside** the lock (so a slow query on one
  thread doesn't serialise every other bot's cache read).
- **`hasPlaystyleColumn` race.** The `static int` init is not mutex-protected.
  Two threads reaching the first call simultaneously may both run the probe;
  the operation is idempotent (same result, same assignment) and the only
  visible effect is that the one-time `LOG_INFO` could print twice. Benign by
  design.
- **No dangling pointers.** `GetBotPlaystyle` takes `Player*` but uses it only
  within the call (name + guid counter); nothing stores a `Player*`/`Unit*`
  across the tick. The cache key is the guid counter, not a pointer, so a bot
  that logs out and back in is re-resolved correctly. The config weight tables
  are immutable after `Initialize()`, so concurrent reads of
  `RpgStatusProbWeight[Profiles]` need no lock.

---

## 6. Config keys

All read via `sConfigMgr->GetOption<int32>` (weights) / `<bool>` (master switch)
in `PlayerbotAIConfig::Initialize()`. **Config loads at startup — restart
worldserver after changes.** Built-in defaults documented in
`server/etc/modules/playerbots.conf`.

**Master switch**

| key | type | default |
|---|---|---|
| `AiPlayerbot.EnableNewRpgStrategy` | bool | `true` |

**Global / default weight table** (`AiPlayerbot.RpgStatusProbWeight.<Status>`)

| key | default |
|---|---|
| `…WanderRandom` | 15 |
| `…WanderNpc` | 20 |
| `…GoGrind` | 15 |
| `…GoCamp` | 10 |
| `…DoQuest` | 60 |
| `…TravelFlight` | 15 |
| `…Rest` | 5 |
| `…OutdoorPvp` | 10 |

**Per-profile weights** (`AiPlayerbot.RpgStatusProbWeight.<Profile>.<Status>`,
Profile ∈ {`Grinder`,`Quester`,`Socializer`,`Explorer`,`Pvper`,`Idler`}, Status
∈ {`GoGrind`,`GoCamp`,`WanderRandom`,`WanderNpc`,`DoQuest`,`TravelFlight`,`Rest`,`OutdoorPvp`}
— note: no `Idle` key). Built-in defaults:

| profile | GoGrind | GoCamp | WanderRandom | WanderNpc | DoQuest | TravelFlight | Rest | OutdoorPvp |
|---|---|---|---|---|---|---|---|---|
| Grinder | 70 | 15 | 5 | 5 | 25 | 10 | 5 | 5 |
| Quester | 5 | 5 | 5 | 15 | 90 | 15 | 5 | 5 |
| Socializer | 5 | 15 | 10 | 60 | 25 | 15 | 15 | 5 |
| Explorer | 10 | 10 | 50 | 15 | 30 | 40 | 5 | 5 |
| Pvper | 15 | 5 | 10 | 15 | 30 | 10 | 5 | 60 |
| Idler | 5 | 20 | 10 | 25 | 15 | 5 | 40 | 5 |

Example override: `AiPlayerbot.RpgStatusProbWeight.Grinder.DoQuest = 40`.

Related (indirect) — the roll cadence is gated by `AiPlayerbot.RpgDelay` (the
non-combat action interval); the personality→playstyle *mapping* is data, not
config (the `playstyle` column, set by the two migrations and `personalities.sql`).

---

## 7. Failure modes & gotchas

- **Chat module / migration absent → vanilla behaviour.** The one-time
  `information_schema` probe drives everything. Column missing ⇒
  `GetBotPlaystyle` always returns `""` ⇒ every bot uses the global
  `RpgStatusProbWeight`. The personality JOIN (which would error against
  non-existent tables) is never issued. This is the primary graceful-degradation
  path.
- **Unrecognised playstyle string → silent default.** If a template row holds a
  `playstyle` value that is not one of the six profile names (typo, e.g.
  `"griner"`, or a future value the binary predates),
  `RpgStatusProbWeightProfiles.find(playstyle)` misses and `RandomChangeStatus`
  falls back to the global table. No warning is logged — a mistyped playstyle
  looks like "default" behaviour, not an error. Verify mappings with the
  distribution SQL in [BOT-BEHAVIOR Section 9](../BOT-BEHAVIOR.md).
- **`default` is treated as empty.** `GetBotPlaystyle` clears `"default"` to
  `""`, so `default`-mapped personalities (and any personality without a row
  yet) use the global table and are re-queried every 5 minutes for the whole
  session. Real profiles are cached once and never re-queried — changing a live
  bot's mapping in the DB won't take effect until the process restarts (or, for
  a currently-`default` bot, at the next 5-minute retry).
- **All-zero weights → forced rest.** If every candidate's weight is 0 (or none
  is available), the bot `ChangeToRest()` + sits rather than idling silently.
  The `default:` arm of the commit switch does the same for an unexpected
  `chosenStatus` (e.g. `RPG_STATUS_END`, which cannot normally be reached).
- **Availability can flip between gate and commit.** A status can pass
  `CheckRpgStatusAvailable` and then fail its commit (grind/camp/flight/quest
  selection re-rolls and comes up empty) — `RandomChangeStatus` returns `false`
  and the bot re-rolls next tick. Not a bug; a consequence of the double random
  evaluation. It also means "select random grind pos" log lines are emitted
  during *availability probes* (once per roll, chosen or not), so they must not
  be used as per-bot activity counts.
- **PvP weight cannot express at low level.** `RPG_OUTDOOR_PVP` requires
  `bot->IsPvP()`, a non-Nagrand zone, and a live `OutdoorPvP` for the zone; the
  `pvper` profile's weight-60 simply drops out of `probSum` until bots are
  flagged and standing in an outdoor-PvP zone.
- **Blocking DB on the map thread.** The queries here run on the world/map
  thread. The caching keeps this cheap, but note that a **new** DB-mapping for a
  `default` bot still costs one synchronous JOIN per 5 minutes; do not lower the
  retry window casually.
- **Guid counters, not GUIDs.** The cache key and the JOIN's `p.guid` are guid
  *counters* (`GetCounter()`), consistent with the sentiment and gear-give
  tables. A raw 64-bit GUID will not match.

---

## 8. Cross-references

- [BOT-BEHAVIOR Section 3 — Playstyles: personality-driven gameplay](../BOT-BEHAVIOR.md)
  — operator-level framing, live-verified distribution numbers, personality→profile spread.
- [BOT-BEHAVIOR Section 2 — Personalities](../BOT-BEHAVIOR.md) — the
  `mod_ollama_chat_personality` / `..._templates` tables and how personalities
  are assigned (the input this subsystem reads).
- [BOT-BEHAVIOR Section 9 — Verification & debugging cookbook](../BOT-BEHAVIOR.md) —
  the "count roll outcomes, not downstream events" methodology and the
  distribution SQL.
- [BOT-ECONOMY Section 3 — Organic bot auction house](../BOT-ECONOMY.md) — a downstream
  consumer of the `WanderNpc` activity that the `socializer` profile favours,
  and another example of the same "read the chat module's tables, probe
  `information_schema` first" loose-coupling pattern.
- Source: `modules/mod-playerbots/src/Ai/World/Rpg/Action/NewRpgBaseAction.cpp`,
  `…/Action/NewRpgAction.cpp`, `…/Strategy/NewRpgStrategy.cpp`,
  `modules/mod-playerbots/src/PlayerbotAIConfig.{cpp,h}`, and the migration
  `modules/mod-ollama-chat/data/sql/characters/base/2026_07_03_personality_playstyle.sql`.
