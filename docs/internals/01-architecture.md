# Internals 01 — Architecture, cross-module linkage & build

Developer-internals reference for the two-module split, the `[[gnu::weak]]`
cross-module call pattern, script registration, the CMake `file(GLOB)` build,
and the world-thread / detached-worker-thread model that every feature rides on.
This is the *function-by-function* companion to the behavior-level docs
[`../BOT-BEHAVIOR.md`](../BOT-BEHAVIOR.md) (Section 1 "who does what") and
[`../BOT-ECONOMY.md`](../BOT-ECONOMY.md) (Section 4 `OllamaChat_SpeakSituation`, Section 5 "How
it compiles"); it uses their terminology and goes one layer deeper into the code.

---

## 1. Purpose

Two AzerothCore modules cooperate inside **one** worldserver binary:
**mod-playerbots** is the C++ decision engine (what a bot *does* — quest, grind,
trade, form guilds), **mod-ollama-chat** is the LLM voice layer (what a bot
*says*). They share only the `acore_characters` database and a handful of
deliberately-loose, weakly-linked C function symbols. This subsystem is the glue:
how a mod-playerbots `.cpp` reaches into mod-ollama-chat without a shared header
or a link-time dependency, how self-contained chat files register themselves, how
the GLOB build stitches it all together, and why the async LLM calls never stall
the world tick.

---

## 2. Entry points & call graph

There is no single entry point — this is *plumbing* exercised from several
independent triggers. The three canonical entries in the focus files:

**A. Script registration (server boot, world thread)**

```
worldserver Main.cpp
  sScriptMgr->SetModulesLoader(AddModulesScripts)      // Main.cpp:266
    AddModulesScripts()                                // generated ModulesLoader.cpp
      Addmod_playerbotsScripts()                       // playerbots_loader.cpp
      Addmod_ollama_chatScripts()                      // mod-ollama-chat_main.cpp:9
        new OllamaChatConfigWorldScript() ... new SentimentOnGroup()
        new OllamaChatConfigCommand()
        AddSC_mod_ollama_chat_events_mail()            // self-contained files,
        AddSC_mod_ollama_chat_memory()                 //   each forward-declared
        AddSC_mod_ollama_chat_guildnames()             //   locally then called
        AddSC_mod_ollama_chat_channels()
```

**B. In-person gear hand-over (trade packet, world thread → detached worker)**

```
SMSG_TRADE_STATUS received by a bot
  TradeStatusAction::Execute(Event)                    // "accept trade" action
    PendingGiveItemGuid(bot, trader, &cod)             // information_schema probe + SELECT
    HandlePendingGearGive(event, trader, itemGuid, cod)
      ├─ BEGIN_TRADE     → face + HandleBeginTradeOpcode   (or vanished-item apology)
      ├─ OPEN_WINDOW     → SetItem(slot0) + HandleAcceptTradeOpcode
      ├─ TRADE_ACCEPT    → payment hold, then HandleAcceptTradeOpcode
      └─ TRADE_COMPLETE  → ClearPendingGive + SpeakSituation(...)
                             → SpeakSituation()  → OllamaChat_SpeakSituation() [weak]
                                → std::thread(...).detach()  → SubmitQuery → Whisper
    (normal path) BeginTrade() / CheckTrade() / CalculateCost()
```

**C. Personality guild formation (last bot login, world thread → detached worker)**

```
RandomPlayerbotMgr::OnBotLoginInternal(bot)            // when population fully online
  PersonalityGuildFactory::FormPersonalityGuilds()     // static `formed` one-shot
    LoadPersonalities(map)                             // CharacterDatabase
    LoadOnlineGuildlessBots(pool, map)                 // PlayerbotsDatabase prepared stmt
    formEliteGuild(Raid|Pvp, leaders, fit)   ×N        // lambda
      findLeader(...) → MakeThemedName(...) → PlayerbotGuildMgr::CreateGuild
      guild->AddMember(...) ×members
      SpeakGuildName(leader, guildId, archetype)
        → OllamaChat_RenameGuildInVoice() [weak]
           → std::thread(...).detach() → SubmitQuery → SanitizeGuildName
             → push PendingGuildRename onto g_GuildNameQueue
    formCasualGuild()                        ×N        // lambda
    GuildRecruitmentEvent::instance().Begin(recruitLeaders)

OllamaChatGuildNameWorldScript::OnUpdate(diff)         // world thread, every tick
  drains g_GuildNameQueue → guild->SetName(...)        // actual rename here
```

---

## 3. Function-by-function

### 3.1 `mod-ollama-chat_main.cpp`

#### `void Addmod_ollama_chatScripts()`
The module's registration entry point, invoked once at boot by the
CMake-generated `AddModulesScripts()`. It performs two kinds of registration:

1. **`ScriptObject` subclasses via `new`** — the AzerothCore pattern where a
   script registers itself in its constructor: `new OllamaChatConfigWorldScript()`,
   `new PlayerBotChatHandler()`, `new OllamaBotRandomChatter()`, the eleven
   `ChatOn*` / `SentimentOnGroup` event scripts, and `new OllamaChatConfigCommand()`.
   The returned pointers are intentionally leaked — `ScriptMgr` owns their
   lifetime for the process.
2. **`AddSC_*` free functions for self-contained files** — for each newer,
   single-file feature the function *forward-declares the loader locally* and
   calls it:
   ```cpp
   void AddSC_mod_ollama_chat_events_mail();
   AddSC_mod_ollama_chat_events_mail();
   void AddSC_mod_ollama_chat_memory();
   AddSC_mod_ollama_chat_memory();
   void AddSC_mod_ollama_chat_guildnames();
   AddSC_mod_ollama_chat_guildnames();
   void AddSC_mod_ollama_chat_channels();
   AddSC_mod_ollama_chat_channels();
   ```
   Each `AddSC_mod_ollama_chat_<x>()` lives in its own `.cpp`, does its own
   `new SomeScript()` internally, and is the *only* symbol `main.cpp` needs from
   that file. **This is the extension pattern for a new chat feature**: write
   `mod-ollama-chat_<x>.cpp` with a `void AddSC_mod_ollama_chat_<x>()` at the
   bottom, then add the two-line forward-declare + call here. Because the
   declaration is inline (not from a shared header), a mismatch or a missing file
   surfaces as a **link-time undefined reference**, not a compile error — see Section 7.

**Side effects:** two `LOG_INFO("server.loading", ...)` lines; allocates the
script singletons. **Inputs/outputs:** none. Runs on the world/loader thread
before any player logs in.

### 3.2 `TradeStatusAction.cpp` (mod-playerbots)

This file is the mod-playerbots consumer of the weak `OllamaChat_SpeakSituation`
symbol and the completion half of the in-person gear-give (economy Section 2).

#### `[[gnu::weak]] void OllamaChat_SpeakSituation(Player*, Player*, std::string const&, bool)` + `static void SpeakSituation(...)`
```cpp
[[gnu::weak]] void OllamaChat_SpeakSituation(Player* bot, Player* target,
                                             std::string const& situation, bool whisper);
static void SpeakSituation(Player* bot, Player* target,
                           std::string const& situation, bool whisper)
{
    if (OllamaChat_SpeakSituation)
        OllamaChat_SpeakSituation(bot, target, situation, whisper);
}
```
A weak **declaration** with no definition in this translation unit. At the final
static link the symbol either binds to the strong definition in
`mod-ollama-chat_handler.cpp` (Section 3.4) or, if the chat module was excluded
(`-DDISABLED_AC_MODULES="mod-ollama-chat"`), resolves to **null**. The `static`
wrapper null-checks the pointer, so every call degrades to a silent no-op when
chat is absent. No header is shared and no link dependency is declared — this is
the whole cross-module contract. (ELF/GCC-Clang behavior; not portable to MSVC —
acceptable for this Linux-only realm.)

#### `static ObjectGuid::LowType PendingGiveItemGuid(Player* bot, Player* trader, uint32* codOut)`
Looks up a parked in-person give for this `(bot, trader)` pair.
1. **One-time schema probe** cached in a function-local `static int hasTable = -1`:
   queries `information_schema.TABLES` for `mod_ollama_chat_pending_gives` in the
   current `DATABASE()`; caches `1`/`0`. If absent, returns `0` forever after —
   graceful degrade when the chat module's migration hasn't run.
2. Selects `item_guid, cod ... WHERE bot_guid = {} AND player_guid = {} AND
   created_at > NOW() - INTERVAL 10 MINUTE` (GUID **counters** via
   `GetGUID().GetCounter()`). No fresh row → `0`.
3. Writes the COD into `*codOut` (if non-null) and returns the item's low GUID.
**Output:** parked item guid counter, `0` = none.

#### `static void ClearPendingGive(Player* bot, Player* trader)`
`DELETE FROM mod_ollama_chat_pending_gives WHERE bot_guid = {} AND player_guid = {}`.
Called after a completed or aborted hand-over. Fire-and-forget `Execute` (async
write, no result).

#### `bool TradeStatusAction::Execute(Event event)`
The action body, run on **every** `SMSG_TRADE_STATUS` the bot receives (the
action is wired as `"accept trade"` in the module's ActionContext).
1. `bot->GetTrader()`; bail `false` if none. `GET_PLAYERBOT_AI(trader)` decides
   whether the trader is another bot (`traderBotAI`) or a real player.
2. **Pending-give shortcut (before all other gating):** if the trader is a real
   player (`!traderBotAI`) and `PendingGiveItemGuid` returns a live row, jump
   straight to `HandlePendingGearGive(...)` — so a *stranger* can complete this
   one trade even on realms where `enableRandomBotTrading == 0` locks trading
   down.
3. Normal gating: rejects traders that aren't the master or a group member
   (whispers a `PlayerbotTextMgr` "busy now" line), honors
   `sPlayerbotAIConfig.enableRandomBotTrading == 0` for random/addclass bots, and
   otherwise `HandleCancelTradeOpcode`s unauthorized openers.
4. Reads `status` off the packet (`event.getPacket()`, `rpos(0)`). On
   `TRADE_STATUS_TRADE_ACCEPT` (or `BACK_TO_TRADE` with an accepted counterpart)
   it snapshots given/taken item ids into two `std::map<uint32,uint32>`, calls
   `HandleAcceptTradeOpcode`, then feeds `CraftData` (`AI_VALUE(CraftData&,
   "craft")`) and `GuildTaskMgr::CheckItemTask`. On `TRADE_STATUS_BEGIN_TRADE` it
   faces the trader and calls `BeginTrade()`.
**Returns** `true` when it handled the packet meaningfully, else `false`.

#### `void TradeStatusAction::BeginTrade()`
Opens a trade session against a real-player trader only (bails if the trader is a
bot). `HandleBeginTradeOpcode`, iterates inventory with a `ListItemsVisitor`,
`TellMaster`s an "=== Inventory ===" dump, and for random bots reports the
available trade discount (`sRandomPlayerbotMgr.GetTradeDiscount`).

#### `bool TradeStatusAction::CheckTrade()`
Pricing/acceptance policy for a *normal* trade (not a pending give). Two regimes:
bot-to-bot with no active player master (accept if receiving anything, thank the
trader), and the account-list-gated player regime that validates each traded slot
(`SellPrice`, `IsConjuredConsumable`, `ItemUsage` via `AI_VALUE2(ItemUsage,
"item usage", ...)`), honors `enableRandomBotTrading` sell/buy locks (`== 2`/`==
3`), and settles money against `sRandomPlayerbotMgr.GetTradeDiscount` /
`AddTradeDiscount`. Emits `PlayerbotTextMgr` lines and `PlaySound` emotes.
**Returns** whether the trade is acceptable.

#### `int32 TradeStatusAction::CalculateCost(Player* player, bool sell)`
Sums the copper value of the items `player` has in the trade window. Skips items
below `ITEM_QUALITY_NORMAL` (returns `0` — poor items abort valuation), applies
`CraftData` craft-fee logic, and multiplies `SellPrice`/`BuyPrice` by
`sRandomPlayerbotMgr.GetSellMultiplier`/`GetBuyMultiplier`. **Output:** total
copper.

#### `bool TradeStatusAction::HandlePendingGearGive(Event& event, Player* trader, ObjectGuid::LowType itemGuidLow, uint32 cod)`
The trade-window state machine that completes a parked in-person give. **The item
is re-resolved from its low GUID on every packet** via
`bot->GetItemByGuid(ObjectGuid::Create<HighGuid::Item>(itemGuidLow))` — never
cached across packets, because the bot may have equipped/auctioned/mailed it
between statuses. Branches on `status`:

| `status` | action |
|---|---|
| `TRADE_STATUS_BEGIN_TRADE` | item present → face trader + `HandleBeginTradeOpcode`. **Item vanished** → `ClearPendingGive` + `SpeakSituation(bot, trader, "you promised them a piece of gear but no longer have it", true)` + `HandleCancelTradeOpcode`, return `false`. |
| `TRADE_STATUS_OPEN_WINDOW` | `myTrade->SetItem(TradeSlots(0), item)`; if COD, `SpeakSituation(...)` with an `Acore::StringFormat` price line (`cod / 100` silver); `HandleAcceptTradeOpcode` (pre-accept). |
| `TRADE_STATUS_TRADE_ACCEPT` / `TRADE_STATUS_BACK_TO_TRADE` | **payment hold**: if `cod` and `trader->GetTradeData()->GetMoney() < cod`, return `true` *without* accepting — sits until the player adds gold; else `HandleAcceptTradeOpcode`. |
| `TRADE_STATUS_TRADE_COMPLETE` | `ClearPendingGive` + `SpeakSituation(...)` (sold / gave-free) + `LOG_INFO("playerbots", "[GearGive] {} handed item over to {} in trade (cod {} copper)", ...)`. |
| default | `false`. |

### 3.3 `PersonalityGuildFactory.cpp` (mod-playerbots)

Consumes the second weak symbol and forms personality-themed guilds when the bot
population comes fully online.

#### `[[gnu::weak]] void OllamaChat_RenameGuildInVoice(Player*, uint32, std::string const&)` + `static void SpeakGuildName(...)`
Same weak-declaration/null-check-wrapper pattern as `SpeakSituation`. Binds to the
strong definition in `mod-ollama-chat_guildnames.cpp` (Section 3.5) or resolves null and
no-ops. Called right after a guild is formed to let the leader's LLM voice pick a
better name asynchronously.

#### `char const* ArchetypeName(GuildArchetype)`
Anonymous-namespace helper mapping the `GuildArchetype` enum
(`Raid`/`Pvp`/`Casual`) to the literals `"raid"`/`"pvp"`/`"casual"` used both in
logs and as the `archetype` string handed to the LLM.

#### `void LoadPersonalities(std::unordered_map<uint32, std::string>& out)`
Single bulk `SELECT guid, personality FROM mod_ollama_chat_personality`
(CharacterDatabase). Fills a `guid-counter → personality-key` map. Empty result
→ leaves `out` untouched (all candidates fall back to `""` personality, matching
nothing).

#### `void LoadOnlineGuildlessBots(std::vector<Candidate>& out, std::unordered_map<uint32,std::string> const& personalities)`
Builds the working candidate pool. Runs the **PlayerbotsDatabase** prepared
statement `PLAYERBOTS_SEL_RANDOM_BOTS_BOT` (arg `"add"`), and for each row:
resolves `ObjectAccessor::FindConnectedPlayer(guid)`, **skips offline or
already-guilded** bots (`bot->GetGuildId()`), and records level, `GetTeamId()`,
and the personality from the map (default `""`). Note the two-database split: pool
membership comes from PlayerbotsDatabase, personality tags from CharacterDatabase.

#### `std::string MakeThemedName(GuildArchetype archetype)`
Themed prefix/suffix generator (static per-archetype word lists). Up to **20**
attempts: `Acore::StringFormat("{} {}", prefix, suffix)` with `urand`-picked
words; rejects names `> 24` chars (WoW guild-name limit) or already taken
(`sGuildMgr->GetGuildByName`). On exhaustion falls back to
`RandomPlayerbotFactory::CreateRandomGuildName()`.

#### `void PersonalityGuildFactory::FormPersonalityGuilds()`  *(the orchestrator)*
1. **One-shot guard**: function-local `static bool formed`; returns immediately on
   any call after the first.
2. Reads counts/thresholds from config (Section 6) and `maxMembers =
   max(2, sPlayerbotAIConfig.randomBotGuildSizeMax)`. Bails if all three counts
   are zero.
3. `LoadPersonalities` + `LoadOnlineGuildlessBots`; logs and returns if the pool
   is empty.
4. Maintains `std::unordered_set<uint32> committed` (bots already placed or found
   stale) and `std::vector<std::pair<ObjectGuid,std::string>> recruitLeaders`.
5. Defines three **lambdas** (capture-by-reference):
   - **`findLeader(leaderSet, outLeader) -> Player*`** — first pool candidate that
     is uncommitted, `level >= leaderMinLevel`, and whose personality is in
     `leaderSet`. Re-resolves via `FindConnectedPlayer`; a now-offline/guilded
     candidate is **marked committed** ("never reconsider") and skipped. Returns
     the `Player*` or `nullptr`.
   - **`formEliteGuild(archetype, leaderSet, fitSet) -> bool`** — find leader →
     `MakeThemedName` → `PlayerbotGuildMgr::CreateGuild`. Commits the leader, then
     admits **same-faction** pool bots whose personality is in `fitSet` (no
     randomness) up to `maxMembers`, via `guild->AddMember(guid,
     urand(GR_OFFICER, GR_INITIATE))`, committing each. `OnGuildUpdate`, log,
     `SpeakGuildName(leaderPlayer, guild->GetId(), ArchetypeName(archetype))`,
     record the leader for recruitment. Returns `false` when no leader remains.
   - **`formCasualGuild() -> bool`** — same shape but leader from
     `CASUAL_LEADERS`; members are any remaining same-faction bot admitted with a
     `roll_chance_f(casualJoinChance)` gate.
6. Runs `formEliteGuild(Raid...)` up to `raidCount`, `formEliteGuild(Pvp...)` up
   to `pvpCount`, `formCasualGuild()` up to `casualCount`, each loop breaking on
   the first failure (leader pool exhausted). Logs the totals.
7. `GuildRecruitmentEvent::instance().Begin(recruitLeaders)` — the fresh-realm
   recruitment event (no-op unless it's a fresh realm; see BOT-BEHAVIOR Section 12).

### 3.4 `OllamaChat_SpeakSituation` — strong definition (`mod-ollama-chat_handler.cpp:2304`)

```cpp
void OllamaChat_SpeakSituation(Player* bot, Player* target,
                               std::string const& situation, bool whisper)
```
The bound target of the weak references in Section 3.2 and `InviteToGroupAction.cpp`.
Deliberately **not declared in any shared header** — mod-playerbots re-declares
it weakly, this module declares the sibling `.cpp` consumers with a plain
`extern` (e.g. `mod-ollama-chat_events_mail.cpp:44`).
1. Guards: `g_Enable` (module master switch) and a live `PlayerbotAI`
   (`PlayerbotsMgr::instance().GetPlayerbotAI(bot)`).
2. Builds a one-shot prompt from `GetBotPersonality` +
   `GetPersonalityPromptAddition` + the class name + the caller's `situation`,
   capped "under 15 words. No narration, no quotes, just the line."
3. `GetPersonalityQueryOptions(bot)` for per-personality `num_predict` /
   `temperature`.
4. **Captures raw GUID values** (`GetGUID().GetRawValue()`), *not* pointers, into
   a **detached `std::thread`**: `SubmitQuery(prompt, opts)` → `fut.get()` →
   reacquire `Player*` via `ObjectAccessor::FindPlayer(ObjectGuid(botGuid))` →
   `botPtr->Whisper(response, LANG_UNIVERSAL, targetPtr)` when `whisper` and the
   target is still online, else `ai->Say(response)`. Every exception is swallowed
   (`catch (...) {}`). Fire-and-forget; the caller (world thread) returns
   immediately.

### 3.5 `mod-ollama-chat_guildnames.cpp`

#### `void OllamaChat_RenameGuildInVoice(Player* leader, uint32 guildId, std::string const& archetype)`
Strong definition bound by Section 3.3's weak ref. Guards on `g_Enable`, a non-null
leader/`guildId`, and `sConfigMgr->GetOption<bool>("OllamaChat.EnableGuildNameGen",
true)`. Builds a persona prompt asking for "a memorable 2008-era WoW guild name,
2 to 4 words," bumps `temperatureOverride` by `+0.15` (capped `1.5`) for variety,
then in a **detached thread** runs `SubmitQuery` → `SanitizeGuildName`, rejects
names `< 3` chars / reserved / profane / already-taken, and **pushes a
`PendingGuildRename` onto `g_GuildNameQueue` under `g_GuildNameQueueMutex`.** It
does *not* rename the guild on the worker thread.

#### `std::string SanitizeGuildName(std::string s)`
Pure string cleanup: keep only the first line, strip edge junk (quotes,
punctuation, brackets, dashes, whitespace), collapse internal whitespace runs to
single spaces, hard-cap at 24 chars without leaving a trailing space. No
title-casing.

#### `class OllamaChatGuildNameWorldScript : WorldScript` / `void OnUpdate(uint32 diff)`
The world-thread consumer of the queue. Each tick: locks the mutex, `swap`s the
queue into a local vector (returns early if empty), then for each entry re-fetches
`sGuildMgr->GetGuildById`, **re-checks name uniqueness on the world thread**
(another guild may have taken it since the worker's check), and applies
`guild->SetName(entry.name)` — the same validated, DB-persisting path
`.guild rename` uses. Logs each rename.

#### `void AddSC_mod_ollama_chat_guildnames()`
`new OllamaChatGuildNameWorldScript();` — the `AddSC_*` loader called from
`Addmod_ollama_chatScripts()` (Section 3.1).

---

## 4. Data structures & DB

**Structs / enums / globals (real names):**
- `enum class GuildArchetype : uint8 { Raid, Pvp, Casual }` and
  `using PersonalitySet = std::unordered_set<std::string>` — with the const sets
  `RAID_LEADERS`, `PVP_LEADERS`, `CASUAL_LEADERS`, `RAID_FIT`, `PVP_FIT`
  (leader personalities unioned into the fit sets).
- `struct Candidate { ObjectGuid guid; uint32 lowGuid; uint32 level; uint8 team;
  std::string personality; }` — one pool entry.
- `struct PendingGuildRename { uint32 guildId; std::string name; std::string
  leaderName; }`; `std::vector<PendingGuildRename> g_GuildNameQueue` guarded by
  `std::mutex g_GuildNameQueueMutex`.
- Trade-path locals `std::map<uint32,uint32> givenItemIds, takenItemIds`.
- Globals referenced across the boundary: `g_Enable`, `g_OllamaTemperature`,
  `g_GuildNameQueue`; `OllamaQueryOptions` (defined in
  `mod-ollama-chat_querymanager.h`, carries `temperatureOverride` and
  `numPredictOverride`). `mod-ollama-chat_api.h` only *declares* the free functions
  that take an `OllamaQueryOptions&` (`QueryOllamaAPI`, `SubmitQuery`) — it does not
  define the struct.

**Databases & tables read/written by the focus code:**

| DB | table / object | access | by |
|---|---|---|---|
| `information_schema` | `TABLES` (probe for `mod_ollama_chat_pending_gives`) | read (cached) | `PendingGiveItemGuid` |
| CharacterDatabase | `mod_ollama_chat_pending_gives` (`bot_guid`, `player_guid`, `item_guid`, `cod`, `created_at`) | read / delete | `PendingGiveItemGuid`, `ClearPendingGive` |
| CharacterDatabase | `mod_ollama_chat_personality` (`guid`, `personality`) | read | `LoadPersonalities` |
| PlayerbotsDatabase | prepared stmt `PLAYERBOTS_SEL_RANDOM_BOTS_BOT` | read | `LoadOnlineGuildlessBots` |
| CharacterDatabase (via core) | guild tables (`guild`, membership) | write | `PlayerbotGuildMgr::CreateGuild`, `guild->AddMember`, `guild->SetName` |

All `*_guid` columns are GUID **counters** (`GetGUID().GetCounter()` /
`ObjectGuid::Create<...>(low)`), never raw 64-bit GUIDs. The pending-gives table
and column set is created by the module migration
`2026_07_03_personality_gear_give.sql` (auto-applied when `Updates.AutoSetup = 1`;
see BOT-ECONOMY Section 5).

---

## 5. Concurrency & threading

Two execution contexts, and every feature here is deliberately split across them:

**World thread (single, authoritative for game state):**
- `Addmod_ollama_chatScripts` and all script construction.
- `TradeStatusAction::Execute` / `HandlePendingGearGive` and all trade opcode
  handling — mutating `TradeData`, moving items, accepting trades.
- `PersonalityGuildFactory::FormPersonalityGuilds` and its lambdas — creating
  guilds, `AddMember`, all `Player*`/`Guild*` access.
- `OllamaChatGuildNameWorldScript::OnUpdate` — the **only** place a guild is
  actually renamed. Core guild state is touched exclusively here.

**Detached worker threads (`std::thread(...).detach()`), for LLM latency only:**
- `OllamaChat_SpeakSituation` and `OllamaChat_RenameGuildInVoice` each spawn one.
  They run `SubmitQuery` (the module's queue-managed HTTP call to Ollama) and read
  only immutable copies.

**Why it's safe:**
- Workers capture **raw GUID values, never `Player*`/`Guild*` pointers** (which
  can dangle on logout/despawn). Players are reacquired at use time via
  `ObjectAccessor::FindPlayer`; guilds via `sGuildMgr->GetGuildById` — both
  null-checked.
- A worker that must mutate world state does **not** — it enqueues. The guild
  rename is the model: worker pushes `PendingGuildRename` under
  `g_GuildNameQueueMutex`; `OnUpdate` drains and applies on the world thread, and
  **re-checks name uniqueness there** to close the check-then-act race.
- `SpeakSituation`'s worker only calls `Whisper`/`Say`, and swallows all
  exceptions, so a mid-flight logout or an Ollama error degrades to silence.
- Mutex scope is minimal: `OnUpdate` locks only to `swap` the queue into a local,
  then processes unlocked.
- Two feature-level caches are function-local statics, safe because they're only
  touched on the world thread: `static int hasTable` (schema probe) and
  `static bool formed` (one-shot guard).

The invariant across the whole subsystem: **the world tick never blocks on
Ollama.** Any LLM call is fire-and-forget on a detached thread.

---

## 6. Config keys

Read via `sConfigMgr->GetOption<T>` with in-code defaults (add to
`playerbots.conf` / `mod_ollama_chat.conf` to override; config loads at startup →
**restart to apply**):

| key | default | read in |
|---|---|---|
| `AiPlayerbot.PersonalityGuild.RaidCount` | `4` | `FormPersonalityGuilds` |
| `AiPlayerbot.PersonalityGuild.PvpCount` | `3` | `FormPersonalityGuilds` |
| `AiPlayerbot.PersonalityGuild.CasualCount` | `8` | `FormPersonalityGuilds` |
| `AiPlayerbot.PersonalityGuild.LeaderMinLevel` | `10` | `FormPersonalityGuilds` |
| `AiPlayerbot.PersonalityGuild.CasualJoinChance` | `40.0` (float) | `FormPersonalityGuilds` |
| `OllamaChat.EnableGuildNameGen` | `true` | `OllamaChat_RenameGuildInVoice` |

Referenced (loaded elsewhere, in `PlayerbotAIConfig.cpp`, not via `sConfigMgr`
here): `sPlayerbotAIConfig.randomBotGuildSizeMax` (member cap),
`sPlayerbotAIConfig.enableRandomBotTrading` (`0` off … `3` buying-disabled),
`sPlayerbotAIConfig.sightDistance`. Module master switch `g_Enable` gates both
weak entry points. The gear-give cooldown keys (`OllamaChat.GearGiveBotCooldownMin`
default `30`, `OllamaChat.GearGivePairCooldownMin` default `1440`) are loaded in
`mod-ollama-chat_config.cpp`; the enforcement globals
(`g_GearGiveBotCooldownMin` / `g_GearGivePairCooldownMin`), the `getMSTime`-based
cooldown maps (`g_lastGiveByBot` / `g_lastGiveByPair`), and the `REPLACE INTO
mod_ollama_chat_pending_gives` parking all live in `mod-ollama-chat_handler.cpp`
(not the events-mail file) — see BOT-ECONOMY Section 6.

---

## 7. Build: CMake GLOB, static link, and the reconfigure gotcha

**One binary, static link.** The tree is configured `-DMODULES=static
-DSCRIPTS=static`, so every module under `modules/*` compiles into one static
blob linked **into worldserver itself**. `modules/CMakeLists.txt` GLOBs the module
directory list (`GetModuleSourceList` → `file(GLOB ... modules/*)`), and for each
static module runs **`CollectSourceFiles(${MODULE_SOURCE_PATH}
PRIVATE_SOURCES_MODULES)`** (`src/cmake/macros/AutoCollect.cmake`), which
recursively `file(GLOB ...)`s `*.cpp/*.h/...`. That single final link is exactly
what lets the `[[gnu::weak]]` reference in one module bind to the strong
definition in the other — same linker invocation, no dlopen, no plugin.

**Generated loader.** `ConfigureScriptLoader` turns each module directory name
into its loader function by `string(REGEX REPLACE - "_" ...)` then
`Add${name}Scripts()` — so `mod-ollama-chat` → `Addmod_ollama_chatScripts()` and
`mod-playerbots` → `Addmod_playerbotsScripts()`. It `configure_file`s
`ModulesLoader.cpp.in.cmake` into `gen_scriptloader/.../ModulesLoader.cpp`, whose
generated `AddModulesScripts()` forward-declares and calls each module's
top-level loader. `worldserver` wires it with
`sScriptMgr->SetModulesLoader(AddModulesScripts)` (`Main.cpp:266`). The optional
per-module `mod-ollama-chat.cmake` (included via `include(... OPTIONAL)`,
`modules/CMakeLists.txt:314`) adds this module's extra deps: bundled
`nlohmann/json`, `fmt`, header-only `cpp-httplib`, and OpenSSL.

**Why a NEW `.cpp` needs a `cmake` reconfigure before `make`.** `file(GLOB)` is
evaluated at **configure** time, so the source list is frozen after the last
`cmake` run. Add `mod-ollama-chat_channels.cpp` (with its
`AddSC_mod_ollama_chat_channels()`) and wire the call into `main.cpp`, then run
`make` **without** re-running `cmake`: the new file isn't in the GLOB, its
`AddSC_*` is never compiled, and the reference from `main.cpp` becomes an
**undefined reference at link time** (not a compile error, because the loader is
inline-declared, not header-declared). The fix is a reconfigure:

```bash
cd azerothcore-wotlk/build
cmake ..            # re-run GLOB so the new .cpp is picked up
make -j$(nproc) install
```

Incremental `make install` is fine for editing an *existing* file; only
*adding/removing* files needs the reconfigure.

**Header blast radius.** `mod-ollama-chat_config.h` is included by nearly every
TU in the chat module, and `PlayerbotAIConfig.h` similarly on the playerbots side
— changing a struct field there (e.g. a new config member) recompiles most of the
module. Expect a long build after touching either. A cross-module **signature**
change (to `OllamaChat_SpeakSituation` / `OllamaChat_RenameGuildInVoice`) is the
sharp edge: there's no shared header to force agreement, so the two sides can
silently disagree — keep the weak declaration and the strong definition
byte-identical.

---

## 8. Failure modes & gotchas

- **Weak symbol → null no-op.** Build with `-DDISABLED_AC_MODULES="mod-ollama-chat"`
  and every `OllamaChat_SpeakSituation` / `OllamaChat_RenameGuildInVoice` call
  becomes a silent no-op; playerbots compiles, links, and runs vanilla. No
  behavior other than the missing chat line.
- **`information_schema` probe** (`PendingGiveItemGuid`): cached in `static int
  hasTable`; a database that never ran the gear-give migration returns `0` and the
  in-person hand-over path is simply never entered — no error.
- **Null reacquire-by-GUID.** Every detached worker (and `findLeader`) reacquires
  the `Player*`/`Guild*` by GUID and null-checks; a bot that logged out mid-flight
  just drops the action. Never store the pointer across the async boundary.
- **Item re-resolved per packet.** `HandlePendingGearGive` re-fetches the item by
  GUID every status; if the bot equipped/auctioned/mailed it, the `BEGIN_TRADE`
  branch fires the whispered apology and cancels — this is the intended cover for
  "a bot can auction the item it parked for a stranger" (BOT-ECONOMY Section 3).
- **Payment hold can sit forever.** On a COD give, `TRADE_ACCEPT` returns without
  accepting until the player's trade money `>= cod`; if they never pay, the trade
  never completes and the row eventually lapses (10-min read window; rows aren't
  GC'd, just become invisible and get overwritten by the next `REPLACE`).
- **One-shot guild formation.** `static bool formed` means `FormPersonalityGuilds`
  runs at most once per process (fired from `OnBotLoginInternal` when the
  population is fully online). A stale/guilded leader candidate is permanently
  `committed` so it's never reconsidered within that run.
- **Guild-rename race** is closed by re-checking `GetGuildByName` on the world
  thread in `OnUpdate`; a name taken between the worker's check and apply is
  dropped (guild keeps its themed name).
- **Themed-name exhaustion** falls back to
  `RandomPlayerbotFactory::CreateRandomGuildName()` after 20 failed attempts.
- **GLOB staleness** (Section 7): a new file without a `cmake` reconfigure → link-time
  undefined reference, not a compile error. The most common self-inflicted build
  failure when adding a chat feature.
- **In-memory / static-life state resets on restart.** The gear-give cooldown maps
  (`g_lastGiveByBot` / `g_lastGiveByPair` in `mod-ollama-chat_handler.cpp`,
  BOT-ECONOMY) use `getMSTime`, and `hasTable`/`formed` are process statics — all
  reset on worldserver restart.

---

## 9. Cross-references

- [`../BOT-BEHAVIOR.md`](../BOT-BEHAVIOR.md) — behavior-level framing: Section 1
  architecture overview, Section 3 playstyles, Section 6 quest-help invites (the other
  `OllamaChat_SpeakSituation` call site), Section 12 realm-start guild recruitment (the
  `GuildRecruitmentEvent` this doc's `FormPersonalityGuilds` kicks off).
- [`../BOT-ECONOMY.md`](../BOT-ECONOMY.md) — the economy layer: Section 2 in-person trade
  hand-over (this doc's `HandlePendingGearGive`), Section 4 `OllamaChat_SpeakSituation`
  design, Section 5 "How it compiles" (the static-link / migration companion to Section 7 here).
- [`../BUILD-NOTES.md`](../BUILD-NOTES.md) — chronological build/deploy journal,
  including the `-DMODULES=static -DSCRIPTS=static` rationale.
- Sibling internals docs live in this directory (`docs/internals/`); this is `01`,
  the architecture/linkage/build layer that the feature-specific internals build
  on.
