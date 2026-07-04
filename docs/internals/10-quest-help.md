# Internals: Sentiment-gated quest-help invites

Developer-level reference for the subsystem that lets an ungrouped bot
proactively offer to group with a nearby **real** player it likes. This is the
function-by-function companion to the behavior-level write-ups in
[BOT-BEHAVIOR.md](../BOT-BEHAVIOR.md) Section 4 (sentiment) and Section 6 (quest-help invites)
and [BOT-ECONOMY.md](../BOT-ECONOMY.md) Section 4 (the `OllamaChat_SpeakSituation`
bridge). Terminology matches those docs; this one explains the code.

Source of truth (read these, do not trust this doc if it drifts):

- `modules/mod-playerbots/src/Ai/World/Rpg/Trigger/NewRpgTrigger.cpp`
- `modules/mod-playerbots/src/Ai/Base/Actions/InviteToGroupAction.cpp`
- headers: `.../Trigger/NewRpgTriggers.h`, `.../Actions/InviteToGroupAction.h`,
  `src/PlayerbotAIConfig.{h,cpp}`, `src/Ai/World/Rpg/NewRpgInfo.h`

---

## 1. Purpose

When a bot is idling in the New-RPG activity loop (not grouped, not in combat,
not in a BG), it occasionally offers to party up with a nearby real player it
has positive **sentiment** toward — most eagerly when it can confirm the player
shares the bot's own active quest. On success it says one in-character,
LLM-generated line and fires a real party invite. The whole thing is a thin,
loosely-coupled bridge into mod-ollama-chat's sentiment table: if the chat
module (or its schema) is absent, sentiment reads default to neutral `0.5`,
which sits below the default `0.6` threshold, so the feature self-disables
gracefully and the bot behaves vanilla.

## 2. Entry points & call graph

Two registrations wire this into the New-RPG strategy
(`NewRpgStrategy.cpp`, `NewRpgStrategy::InitTriggers`):

```
TriggerNode "quest help offer"
    └─ NextAction "offer quest help"  (relevance 12.0f)
```

- Trigger name `"quest help offer"` → `QuestHelpOfferTrigger`
  (registered in `TriggerContext.h`: `quest_help_offer`).
- Action name `"offer quest help"` → `OfferQuestHelpAction`
  (registered in `ActionContext.h`: `offer_quest_help`).

Runtime flow, per bot AI tick, on the bot's update thread:

```
Engine::DoNextAction (per-tick)
  └─ Trigger::needCheck(now)                 // 30s gate, see Section 5
       └─ QuestHelpOfferTrigger::IsActive()  // NewRpgTrigger.cpp:10
            ├─ OfferQuestHelpAction::FindQuestHelpTarget(bot, botAI, tier)
            │     └─ GetBotPlayerSentiment(bot, player)   // DB bridge, cached
            └─ roll_chance_f(chance for tier)             // tier-selected %
  └─ (if IsActive) NextAction "offer quest help" @ 12.0f
       └─ OfferQuestHelpAction::Execute(event)
            ├─ FindQuestHelpTarget(bot, botAI, tier)      // recomputed
            │     └─ GetBotPlayerSentiment(...)           // cache hit
            ├─ sObjectMgr->GetQuestTemplate(questId)      // tier 2 only
            ├─ SpeakSituation(bot, target, situation, false)
            │     └─ [[gnu::weak]] OllamaChat_SpeakSituation(...)  // async, chat module
            └─ InviteToGroupAction::Invite(bot, target)   // real party-invite packet
```

Note the target search runs **twice** per fire — once in `IsActive` (to pick a
tier and roll), once in `Execute` (to invite). The roll is not repeated; see
Section 7 for the consequences.

## 3. Function-by-function

### `QuestHelpOfferTrigger::IsActive()`

```cpp
bool QuestHelpOfferTrigger::IsActive()
```
`NewRpgTrigger.cpp:10`. The gate. Steps:

1. `int tier = 0;` then call
   `OfferQuestHelpAction::FindQuestHelpTarget(bot, botAI, tier)`. If it returns
   `nullptr` (no eligible target), return `false` — no roll, no cost beyond the
   search.
2. Select the chance by the out-param `tier`:
   - `tier == 2` → `sPlayerbotAIConfig.questHelpConfirmedChance`
   - `tier == 1` → `sPlayerbotAIConfig.questHelpNearbyChance`
   - else (`tier == 0`) → `sPlayerbotAIConfig.questHelpRandomChance`
3. `return roll_chance_f(chance);` — `roll_chance_f` treats the value as a
   **percent** (`0..100`), so `2.0f` is a 2% pass. Uses the project RNG
   (`Random.h`), not `std::rand`.

Inputs: `bot`, `botAI` (Trigger members). Output: bool (fire the action).
Side effects: none beyond the sentiment cache write inside `FindQuestHelpTarget`.
The tier value computed here is thrown away; `Execute` recomputes it.

Construction — `NewRpgTriggers.h:26`:
```cpp
QuestHelpOfferTrigger(PlayerbotAI* botAI) : Trigger(botAI, "quest help offer", 30) {}
```
The `30` is the check interval (see Section 5 for how it becomes 30 seconds).

### `GetBotPlayerSentiment(Player* bot, Player* player)`

```cpp
static float GetBotPlayerSentiment(Player* bot, Player* player)
```
`InviteToGroupAction.cpp:476`. File-local (internal linkage). The cross-module
bridge into mod-ollama-chat's shared `acore_characters` DB. Returns a
relationship score `0.0..1.0`, defaulting to **neutral `0.5`** whenever it
cannot do better. Steps:

1. **One-time schema probe** — `static int hasTable = -1;`. On first call
   (`hasTable == -1`) it runs:
   ```sql
   SELECT 1 FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 'mod_ollama_chat_bot_player_sentiments'
   ```
   and caches `1`/`0`. If the table is absent, **returns `0.5f`** immediately —
   permanently, for the process lifetime, no per-call query cost.
2. **Cache key** — `std::pair<ObjectGuid::LowType, ObjectGuid::LowType>` built
   from `bot->GetGUID().GetCounter()` and `player->GetGUID().GetCounter()`.
   These are guid **counters**, matching the column convention (see gotchas).
   The key is directional (`bot` first, `player` second).
3. **Cache read** — under `sentimentCacheMutex`, look up `sentimentCache`. On a
   hit newer than `5 * MINUTE * IN_MILLISECONDS` (5 min), return the cached
   float. `now` is `getMSTime()` (server-uptime ms).
4. **DB read on miss** — synchronous `CharacterDatabase.Query` of
   `sentiment_value FROM mod_ollama_chat_bot_player_sentiments
   WHERE bot_guid = {} AND player_guid = {}`. If a row exists,
   `value = (*result)[0].Get<float>()`; otherwise `value` stays `0.5f`.
5. **Cache write** — under the mutex, `sentimentCache[key] = {value, now};`
   return `value`.

Inputs: two live `Player*` (used only for GUID counters — safe, no storage).
Output: float sentiment. Side effects: populates the static cache; may issue
one blocking `CharacterDatabase.Query` on a cache miss. **This is a synchronous
DB read on the bot update thread** — acceptable because it is gated behind the
30s trigger interval and the 5-minute cache, so it fires rarely.

### `OfferQuestHelpAction::FindQuestHelpTarget(Player* bot, PlayerbotAI* botAI, int& tier)`

```cpp
static Player* OfferQuestHelpAction::FindQuestHelpTarget(Player* bot, PlayerbotAI* botAI, int& tier)
```
`InviteToGroupAction.cpp:509`. `static` (called from both the trigger and the
action). Picks the first eligible nearby real player and classifies the offer
into a tier via the out-param `tier`. Steps:

1. **Bot self-gate** — bail (`nullptr`) unless `bot->IsAlive()` and NOT
   (`bot->GetGroup()` or `bot->InBattleground()` or `bot->IsInCombat()`).
2. **Active quest** — `uint32 questId = 0;` and, if
   `botAI->rpgInfo.GetStatus() == RPG_DO_QUEST`, read
   `questId = std::get<NewRpgInfo::DoQuest>(botAI->rpgInfo.data).questId;`.
   The `std::get` is only reached when the status is `RPG_DO_QUEST`, which
   guarantees the `RpgData` variant currently holds a `DoQuest` — so it cannot
   throw `bad_variant_access`.
3. **Candidate scan** — `GuidVector nearGuids =
   botAI->GetAiObjectContext()->GetValue<GuidVector>("nearest friendly players")->Get();`
   For each `guid`, `ObjectAccessor::FindPlayer(guid)` and skip if:
   - `!player || player == bot || !player->IsAlive() || player->GetGroup()`
   - `GET_PLAYERBOT_AI(player)` — **skip other bots** (offers target real
     players only; bot↔bot grouping is upstream's `RandomBotGroupNearby`).
   - `player->isDND() || player->IsBeingTeleported()`
   - `bot->GetDistance(player) > 30.0f` — hardcoded 30-yard radius (3D
     distance), distinct from `sightDistance` used by the sibling invite
     actions.
   - `GetBotPlayerSentiment(bot, player) < sPlayerbotAIConfig.questHelpSentimentThreshold`
     — the sentiment gate.
4. **Tier classification** for the first survivor:
   - `questId && player->GetQuestStatus(questId) == QUEST_STATUS_INCOMPLETE`
     → `tier = 2` (**confirmed**: same quest, accepted-but-unfinished, in both
     logs).
   - `questId` (but player not on it) → `tier = 1` (**nearby**: bot is
     questing, cannot confirm the player is).
   - else → `tier = 0` (**random**: bot isn't questing — the rare generic
     offer).
   Return that `player`.
5. If the loop exhausts, `return nullptr`.

Inputs: `bot`, `botAI`, `int& tier` (out). Output: `Player*` target or
`nullptr`, and `tier` set. Side effects: sentiment cache writes. Non-obvious:
returns the **first** eligible candidate in `nearGuids` order (whatever order
the value produces); no "best" selection.

### `OfferQuestHelpAction::Execute(Event)`

```cpp
bool OfferQuestHelpAction::Execute(Event /*event*/)
```
`InviteToGroupAction.cpp:545`. Runs when the trigger fired. Steps:

1. Recompute: `int tier = 0; Player* target = FindQuestHelpTarget(bot, botAI, tier);`
   If `!target`, return `false` (target moved/grouped/out-of-range since
   `IsActive`; the fire is wasted, no invite).
2. Build the `situation` string handed to the LLM:
   - `tier == 2`: re-read `questId` from
     `std::get<NewRpgInfo::DoQuest>(botAI->rpgInfo.data).questId`,
     `Quest const* quest = sObjectMgr->GetQuestTemplate(questId)`,
     `title = quest ? quest->GetTitle() : "the same quest"`, then
     `Acore::StringFormat("you and this player are both on the quest '{}'; you're inviting them to group up and finish it faster", title)`.
   - `tier == 1`: `"this player is questing near you; you're inviting them to group up"`.
   - `tier == 0`: `"you're offering this nearby player a hand with whatever they're doing"`.
3. `SpeakSituation(bot, target, situation, false)` — the `false` is `whisper`,
   so the line is **said** (public /say), not whispered. Fire-and-forget; if
   the chat module is out, this is a silent no-op (Section 4/Section 7).
4. `bool invited = Invite(bot, target);` (see below).
5. On `invited`, emit
   `LOG_DEBUG("playerbots", "[QuestHelp] Bot {} offered quest help to {} (tier {})", ...)`.
6. `return invited;`

Non-obvious: the `tier` used for the say-line is the one computed **here**, not
the one the trigger rolled against. If the player's quest state changes between
`IsActive` and `Execute`, the flavor text can mismatch the tier that passed the
roll (Section 7).

### `InviteToGroupAction::Invite(Player* inviter, Player* player)`

```cpp
virtual bool InviteToGroupAction::Invite(Player* inviter, Player* player)
```
`InviteToGroupAction.cpp:30`. The shared party-invite path reused by every
invite action in the file (`JoinGroupAction`, `InviteNearbyToGroupAction`,
`LfgAction`, and here). For the quest-help path, `inviter = bot`,
`player = target` (a real player). Steps relevant to this caller:

1. `if (!player) return false;` and `if (inviter == player) return false;`.
2. **Security gate for real players** — because `!GET_PLAYERBOT_AI(player)` is
   true for a real player, it evaluates
   `!botAI->GetSecurity()->CheckLevelFor(PLAYERBOT_SECURITY_INVITE, true, player)`
   and returns `false` if the player's security level forbids being invited by
   this bot. **A rolled, spoken offer can still be silently refused here.**
3. The raid-conversion branch (`if (Group* group = inviter->GetGroup())`) is
   dead for this caller — a quest-help bot is always ungrouped (gated in
   `FindQuestHelpTarget`).
4. Build and dispatch the invite packet:
   ```cpp
   WorldPacket p;
   uint32 roles_mask = 0;
   p << player->GetName();
   p << roles_mask;
   inviter->GetSession()->HandleGroupInviteOpcode(p);
   return true;
   ```
   This drives the same opcode handler a real client's invite would, so the
   target sees a normal party invitation dialog.

Output: `true` if the invite opcode was dispatched (not that it was accepted).
Side effect: sends a group-invite to `player`.

### `SpeakSituation` / `OllamaChat_SpeakSituation` (the weak bridge)

```cpp
[[gnu::weak]] void OllamaChat_SpeakSituation(Player* bot, Player* target, std::string const& situation, bool whisper);
static void SpeakSituation(Player* bot, Player* target, std::string const& situation, bool whisper)
{
    if (OllamaChat_SpeakSituation)
        OllamaChat_SpeakSituation(bot, target, situation, whisper);
}
```
`InviteToGroupAction.cpp:23`. The strong definition lives in mod-ollama-chat
(`mod-ollama-chat_handler.cpp`); both modules static-link into the one
worldserver binary, so the linker binds this weak reference to it. Built with
the chat module disabled, the weak symbol resolves to `null` and the wrapper's
`if` makes every call a no-op — playerbots links and runs untouched. Full
mechanics, prompt construction, and the detached-thread async model are in
[BOT-ECONOMY.md](../BOT-ECONOMY.md) Section 4. For this subsystem the contract is:
fire-and-forget, never blocks the world tick, generates one `<15`-word
in-character line from the bot's personality prompt + the `situation` string.

## 4. Data structures & DB

**In-code (all file-local statics in `GetBotPlayerSentiment`):**

- `static int hasTable = -1;` — tri-state schema-probe cache
  (`-1` unknown, `0` absent, `1` present).
- `static std::mutex sentimentCacheMutex;` — guards the cache.
- `static std::map<std::pair<ObjectGuid::LowType, ObjectGuid::LowType>,
  std::pair<float, uint32>> sentimentCache;` — key = `(bot counter, player
  counter)`, value = `(sentiment_value, cached-at ms)`. TTL
  `5 * MINUTE * IN_MILLISECONDS`.

**Bot RPG state (read-only here):**

- `botAI->rpgInfo` — a `NewRpgInfo` (`NewRpgInfo.h`). `GetStatus()` returns a
  `NewRpgStatus` enum (`PlayerbotAIConfig.h:50`; `RPG_DO_QUEST = 5`).
- `NewRpgInfo::RpgData data;` — a `std::variant`; the `RPG_DO_QUEST` alternative
  is `struct DoQuest { const Quest* quest; uint32 questId; ... }`. This code
  reads only `questId`.

**Context value:**

- `"nearest friendly players"` → `GuidVector` — the candidate list, via
  `GetAiObjectContext()->GetValue<GuidVector>(...)`.

**DB — `acore_characters` (CharacterDatabase), read-only:**

| object | columns touched | how |
|---|---|---|
| `information_schema.TABLES` | `TABLE_SCHEMA`, `TABLE_NAME` | one-time existence probe |
| `mod_ollama_chat_bot_player_sentiments` | `sentiment_value` (filtered on `bot_guid`, `player_guid`) | per cache-miss `SELECT` |

Both `bot_guid` and `player_guid` are guid **counters**, not raw 64-bit GUIDs.
This subsystem never **writes** the DB. The table is owned and populated by
mod-ollama-chat (see [BOT-BEHAVIOR.md](../BOT-BEHAVIOR.md) Section 4). The party-invite
itself mutates group state via the core opcode handler, not via a direct DB
write here.

## 5. Concurrency & threading

- **Trigger cadence** — `QuestHelpOfferTrigger` is constructed with interval
  `30`. `Trigger`'s constructor
  (`src/Bot/Engine/Trigger/Trigger.cpp`) rescales it:
  `checkInterval == 1 ? 1 : (checkInterval < 100 ? checkInterval * 1000 : checkInterval)`,
  so `30` → `30000` ms. `Engine.cpp` computes `now = getMSTime()` and calls
  `Trigger::needCheck(now)`, which fires when
  `now - lastCheckTime >= checkInterval`. Net: **`IsActive` runs at most once
  per 30 seconds per bot.**
- **Which thread** — `IsActive`, `FindQuestHelpTarget`, `GetBotPlayerSentiment`,
  `Execute`, and `Invite` all run on the bot's AI-update thread (a map-update
  worker). All `Player*` access, `ObjectAccessor::FindPlayer`, the quest-status
  read, and the invite opcode happen there — no cross-thread object access, and
  no `Player*` is stored past the call.
- **Why the mutex** — the sentiment cache statics are **shared across all
  bots**, and bot AI updates can run concurrently on multiple map threads, so
  `sentimentCacheMutex` serializes reads/writes of `sentimentCache`. Two bots
  querying different pairs simultaneously is the common case; the mutex keeps
  the `std::map` from being corrupted. `hasTable` is a benign racing scalar
  (all racers compute the same value).
- **The one detached thread** is *inside* `OllamaChat_SpeakSituation` (chat
  module), which captures **raw GUIDs**, calls Ollama off-thread, then
  reacquires `Player*` via `ObjectAccessor::FindPlayer` before speaking. Nothing
  in the quest-help path waits on it; the invite is dispatched immediately after
  the call returns. See [BOT-ECONOMY.md](../BOT-ECONOMY.md) Section 4.
- **DB blocking** — the one `CharacterDatabase.Query` on a cache miss is
  synchronous on the update thread. It is not moved to the async query
  processor, but the 30s trigger gate + 5-min cache keep its frequency low
  enough to be acceptable. (A high-traffic realm could migrate this to
  `AsyncQuery`; today it is a deliberate simplification.)

## 6. Config keys

All read in `PlayerbotAIConfig::Initialize` (`PlayerbotAIConfig.cpp:718-721`)
via `sConfigMgr->GetOption<float>`. None ship in `playerbots.conf.dist`; add the
line to `server/etc/modules/playerbots.conf` to override, then restart
worldserver (config loads at startup).

| key | default | member | used in |
|---|---|---|---|
| `AiPlayerbot.QuestHelpSentimentThreshold` | `0.6f` | `questHelpSentimentThreshold` | `FindQuestHelpTarget` sentiment gate |
| `AiPlayerbot.QuestHelpConfirmedChance` | `2.0f` | `questHelpConfirmedChance` | tier 2 roll (`IsActive`) |
| `AiPlayerbot.QuestHelpNearbyChance` | `0.5f` | `questHelpNearbyChance` | tier 1 roll (`IsActive`) |
| `AiPlayerbot.QuestHelpRandomChance` | `0.1f` | `questHelpRandomChance` | tier 0 roll (`IsActive`) |

Chances are **percent per 30-second check**. There is no config for the
30-yard radius (hardcoded `30.0f`), the 30s interval (constructor literal
`30`), the 5-minute sentiment-cache TTL, or the action relevance (`12.0f` in
`NewRpgStrategy.cpp`). The whole subsystem only fires when the New-RPG strategy
is active — `AiPlayerbot.EnableNewRpgStrategy = 1` — since the trigger lives in
`NewRpgStrategy::InitTriggers`.

## 7. Failure modes & gotchas

- **Chat module / table absent → feature self-disables.** The
  `information_schema` probe caches "table missing", so every
  `GetBotPlayerSentiment` returns `0.5`. With the default threshold `0.6`,
  `0.5 < 0.6` fails the sentiment gate for every candidate — no bot ever
  cold-invites. Lowering `QuestHelpSentimentThreshold` below `0.5` would let
  it fire even with no sentiment data (all pairs treated as friendly).
- **Strangers stay neutral.** A pair with no row is `0.5` (below `0.6`), so a
  bot never invites someone it has never interacted with — by design.
- **Weak symbol null → silent invite.** If mod-ollama-chat is compiled out,
  `SpeakSituation` no-ops; the bot still sends the party invite with no chat
  line. The invite path has no dependency on the chat module.
- **Double search, single roll.** `FindQuestHelpTarget` runs in both `IsActive`
  and `Execute`. The roll happens only in `IsActive`. If the target grouped,
  moved out of 30 yd, entered combat, or the bot's own state changed between the
  two calls, `Execute`'s search returns `nullptr` and the fire is silently
  wasted (`return false`). No re-roll.
- **Tier can drift between roll and say-line.** The chance rolled in `IsActive`
  is tied to that call's tier; the flavor text uses `Execute`'s freshly computed
  tier. E.g. the player advances the shared quest to
  `QUEST_STATUS_COMPLETE` after a tier-2 (2%) roll passes — `Execute` then sees
  no `QUEST_STATUS_INCOMPLETE`, drops to tier 1, and speaks the "questing near
  you" line. Cosmetic only; the invite still goes out.
- **`std::get` safety.** Both `std::get<NewRpgInfo::DoQuest>(botAI->rpgInfo.data)`
  accesses are guarded by `GetStatus() == RPG_DO_QUEST` (directly in
  `FindQuestHelpTarget`, and transitively in `Execute` where the `tier == 2`
  branch is only reached because the just-run search saw `RPG_DO_QUEST`). No
  yield happens between guard and access on this thread, so the variant cannot
  change underneath it. Removing the guard, or reading `data` when tier could be
  `0/1` without a quest, would risk `bad_variant_access`.
- **Security refusal.** `Invite` may still return `false` at
  `CheckLevelFor(PLAYERBOT_SECURITY_INVITE, ...)` for a real player whose
  account/settings forbid bot invites — after the line was already spoken.
- **Sentiment cache staleness.** A sentiment change is not seen for up to 5
  minutes (cache TTL). The probe cache (`hasTable`) is set on first call and
  never re-evaluated, so a table created *after* the first probe won't be
  noticed until worldserver restart.
- **Log visibility.** Success emits `[QuestHelp] Bot X offered quest help to Y
  (tier N)` at **debug** level in `Playerbots.log`. A failed roll, a rejected
  security check, or an empty second search produce no log line.

## 8. Cross-references

- [BOT-BEHAVIOR.md](../BOT-BEHAVIOR.md) — Section 4 sentiment table & how scores move,
  Section 6 the behavior-level quest-help framing this doc deepens, Section 3 the New-RPG
  playstyle loop the trigger lives inside.
- [BOT-ECONOMY.md](../BOT-ECONOMY.md) — Section 4 `OllamaChat_SpeakSituation`
  (the weak cross-module LLM-dialogue bridge, prompt + async model), Section 5 how the
  two static modules link into one binary (why the weak symbol resolves).
- Sibling New-RPG trigger in the same file: `AhSellSparesTrigger` /
  `AhSellSparesAction` (organic auction house), documented in
  [BOT-ECONOMY.md](../BOT-ECONOMY.md) Section 3.
- Related invite actions sharing `InviteToGroupAction::Invite`:
  `InviteNearbyToGroupAction`, `InviteGuildToGroupAction`, `JoinGroupAction`,
  `LfgAction` (same file) — upstream bot↔bot grouping, out of scope here.
