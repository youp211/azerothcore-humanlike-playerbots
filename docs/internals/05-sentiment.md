# Internals: Sentiment — relationship scoring

*Subsystem of `mod-ollama-chat`. Source of truth:
`modules/mod-ollama-chat/src/mod-ollama-chat_sentiment.{cpp,h}` and the
world-event hooks in `mod-ollama-chat_events.{cpp,h}`. This document is the
developer-level companion to [`../BOT-BEHAVIOR.md`](../BOT-BEHAVIOR.md) Section 4
("Sentiment: evolving relationships"); it explains how the code actually works,
function by function.*

---

## 1. Purpose

Every ordered `(bot, player)` pair carries a single **relationship sentiment**
float in `[0.0, 1.0]` (0.0 = hostile, 0.5 = neutral, 1.0 = friendly, default
0.5). It moves two ways: an **extra per-exchange LLM classification call** that
nudges the pair when a player talks to a bot, and **direct world-event nudges**
(duel end, group add, guild join) applied with no LLM. The current value is
injected into the bot's chat/event prompts as `{sentiment_info}` so the model
steers its tone, and it is read by the quest-help invite gate (see
`../BOT-BEHAVIOR.md` Section 6). Values live in an in-memory map, are periodically
flushed to `acore_characters.mod_ollama_chat_bot_player_sentiments`, and are
reloaded on startup / config reload.

---

## 2. Entry points & call graph

There are three independent producers and two consumers. All state funnels
through the `g_SentimentMutex`-guarded map `g_BotPlayerSentiments`.

**Startup / reload (world thread)**
```
OllamaChatConfigWorldScript (config load) ── mod-ollama-chat_config.cpp:785
  └─ InitializeSentimentTracking()                       [sentiment.cpp:216]
       └─ LoadBotPlayerSentimentsFromDB()                [sentiment.cpp:156]
            └─ CharacterDatabase.Query("SELECT ... mod_ollama_chat_bot_player_sentiments")
.ollama reload command ── command.cpp:64
  └─ InitializeSentimentTracking()   (same path)
```

**Producer A — chat exchange (detached worker thread)**
```
PlayerBotChatHandler bot-response thread ── handler.cpp:1720
  └─ UpdateBotPlayerSentiment(bot, player, msg)          [sentiment.cpp:112]
       ├─ GetBotPlayerSentiment(botGuid, playerGuid)     [sentiment.cpp:12]
       ├─ AnalyzeMessageSentiment(msg)                   [sentiment.cpp:64]
       │    └─ QueryOllamaAPI(prompt)   ← blocking HTTP, GLOBAL sampling params
       └─ SetBotPlayerSentiment(botGuid, playerGuid, new) [sentiment.cpp:33]
```

**Producer B — world events (world thread, via script hooks)**
```
ChatOnDuel::OnPlayerDuelEnd(winner, loser)               [events.cpp:612]
  ├─ NudgeSentimentPair(winner, loser, -g_SentimentEventDuelAdjustment)  [sentiment.cpp:51]
  └─ RecordMemoryForPair(winner, loser, "duel", ...)     (memory subsystem)

SentimentOnGroup::OnAddMember(group, guid)               [events.cpp:743]  (GroupScript)
  └─ for each existing member:
       ├─ NudgeSentimentPair(newMember, member, +g_SentimentEventGroupAdjustment)
       └─ RecordMemoryForPair(newMember, member, "group", ...)

ChatOnGuildMemberChange::OnGuildMemberJoin(player, guild) [events.cpp:674]
  └─ for each online guildmate:
       ├─ NudgeSentimentPair(player, member, +g_SentimentEventGuildAdjustment)
       └─ RecordMemoryForPair(player, member, "guild", ...)
```
`NudgeSentimentPair` internally does a symmetric read-modify-write:
`Get(a,b)+delta → Set(a,b)` **and** `Get(b,a)+delta → Set(b,a)`.

**Consumer — prompt assembly**
```
handler.cpp:2210  sentimentInfo = GetSentimentPromptAddition(bot, player)   [sentiment.cpp:139]
handler.cpp:2265  fmt::arg("sentiment_info", sentimentInfo) → g_ChatPromptTemplate

events.cpp:452    sentimentInfo = GetSentimentPromptAddition(bot, actorPlayer)
events.cpp:473    fmt::arg("sentiment_info", sentimentInfo) → g_EventChatterPromptTemplate
```
`GetSentimentPromptAddition` = `SafeFormat(g_SentimentPromptTemplate,
player_name, sentiment_value)`.

**Persistence (world thread, periodic)**
```
OllamaBotRandomChatter world-update ── random.cpp:52
  └─ if difftime(now, g_LastSentimentSaveTime) >= g_SentimentSaveInterval*60:
       SaveBotPlayerSentimentsToDB()                     [sentiment.cpp:188]
         └─ REPLACE INTO mod_ollama_chat_bot_player_sentiments (per pair)
```

**Inspection (world thread, GM command)**
`OllamaChatConfigCommand::HandleOllamaSentimentViewCommand` (`command.cpp:69`)
reads the live map under the mutex — `.ollama sentiment view [<bot>] [<player>]`.

---

## 3. Function-by-function

### `mod-ollama-chat_sentiment.cpp`

#### `float GetBotPlayerSentiment(uint64_t botGuid, uint64_t playerGuid)`
The single read accessor.
1. If `!g_EnableSentimentTracking`, returns `g_SentimentDefaultValue`
   immediately (no lock).
2. Locks `g_SentimentMutex`.
3. Two-level lookup in `g_BotPlayerSentiments[botGuid][playerGuid]`.
4. Returns the stored value, else `g_SentimentDefaultValue` when either level
   misses.

**Note:** the map is never pre-populated per pair; unseen pairs simply read the
default. Order matters — `Get(a,b)` and `Get(b,a)` are distinct keys.

#### `void SetBotPlayerSentiment(uint64_t botGuid, uint64_t playerGuid, float sentimentValue)`
The single write accessor.
1. No-op if tracking disabled.
2. **Clamps** to `[0.0, 1.0]` via `std::max(0.0f, std::min(1.0f, …))` —
   this is the *only* clamp in the subsystem; callers add raw deltas and rely on
   it.
3. Locks the mutex and assigns `g_BotPlayerSentiments[botGuid][playerGuid]`
   (auto-creates both map levels).
4. Debug log when `g_DebugEnabled`.

#### `void NudgeSentimentPair(Player* a, Player* b, float delta)`
Direct, LLM-free adjustment used by the world-event hooks.
- Guards: tracking enabled, both non-null, `a != b`, `delta != 0.0f`.
- Reads raw GUID values via `a->GetGUID().GetRawValue()` /
  `b->GetGUID().GetRawValue()`.
- Applies **symmetrically**: `Set(a,b, Get(a,b)+delta)` and
  `Set(b,a, Get(b,a)+delta)`. Clamping happens inside `SetBotPlayerSentiment`.
- The read-modify-write is *not* atomic across the pair — see Section 5.
- Does **not** check whether either side is a bot; it happily nudges bot↔bot and
  real↔real pairs. (Only bots ever *consume* the value via
  `GetSentimentPromptAddition`, and `RecordMemoryForPair` — a separate call —
  does enforce exactly-one-bot.)

#### `float AnalyzeMessageSentiment(const std::string& message)`
The extra per-message LLM classification call.
- Returns `0.0f` if tracking disabled or `message` empty.
- Builds the prompt with `SafeFormat(g_SentimentAnalysisPrompt,
  fmt::arg("message", message))`.
- Calls `QueryOllamaAPI(prompt)` — **one argument**, so `opts` defaults to
  `OllamaQueryOptions{}` (`numPredictOverride = 0`, `temperatureOverride =
  -1.0f`), i.e. this call uses the **global** `g_OllamaNumPredict` /
  `g_OllamaTemperature` sampling params, unlike the personality-flavoured
  chat/event calls that pass `GetPersonalityQueryOptions(bot)`. This is the one
  Ollama call in the system that keeps global sampling.
- Empty response → `0.0f`.
- Uppercases the response (`std::transform … ::toupper`) and does **substring**
  matching: `"POSITIVE"` → `+g_SentimentAdjustmentStrength`; else `"NEGATIVE"`
  → `-g_SentimentAdjustmentStrength`; anything else (incl. `NEUTRAL`) → `0.0f`.
- POSITIVE is checked first, so a response containing both words scores
  positive.
- **Output** is the *delta*, not a new value.

#### `void UpdateBotPlayerSentiment(Player* bot, Player* player, const std::string& message)`
Producer A's driver; called once per handled exchange (`handler.cpp:1720`).
1. No-op if tracking disabled or either pointer null.
2. `currentSentiment = GetBotPlayerSentiment(botGuid, playerGuid)`.
3. `adjustment = AnalyzeMessageSentiment(message)` (the blocking HTTP call).
4. `newSentiment = currentSentiment + adjustment`; store via
   `SetBotPlayerSentiment` (which clamps).
5. Debug log only when `adjustment != 0.0f`.
- **Directional:** only updates `bot→player`, never `player→bot`.

#### `std::string GetSentimentPromptAddition(Player* bot, Player* player)`
The consumer that turns a stored value into prompt text.
- Returns `""` if tracking disabled, either pointer null, **or**
  `g_SentimentPromptTemplate` is empty.
- `SafeFormat(g_SentimentPromptTemplate, fmt::arg("player_name",
  player->GetName()), fmt::arg("sentiment_value", sentimentValue))`.
- No `bot == player` guard: for a bot's *self* event the caller in
  `BuildPrompt` resolves the actor to the bot itself, producing a benign
  "sentiment with <self> is 0.50" line.

#### `void LoadBotPlayerSentimentsFromDB()`
- No-op if disabled. Locks the mutex, `clear()`s the map, then
  `CharacterDatabase.Query("SELECT bot_guid, player_guid, sentiment_value FROM
  mod_ollama_chat_bot_player_sentiments")`.
- Null result → logs "No existing sentiment data" and returns.
- Iterates rows: `fields[0].Get<uint64_t>()`, `[1]` player guid, `[2]
  .Get<float>()` → `g_BotPlayerSentiments[botGuid][playerGuid]`. Logs the count.

#### `void SaveBotPlayerSentimentsToDB()`
- No-op if disabled; early-returns if the map is empty.
- Locks the mutex, then for every `(botGuid → (playerGuid → value))` issues
  `CharacterDatabase.Execute(SafeFormat("REPLACE INTO
  mod_ollama_chat_bot_player_sentiments (bot_guid, player_guid, sentiment_value)
  VALUES ({}, {}, {:.3f})", …))`.
- Writes only the three logical columns; `id`/`last_updated`/`created_at` are
  left to MySQL defaults. Value is serialised at **3 decimals**.
- Holds the mutex across *all* `Execute` calls (see Section 5).

#### `void InitializeSentimentTracking()`
- If disabled, logs "Sentiment tracking is disabled" and returns.
- Calls `LoadBotPlayerSentimentsFromDB()` and seeds
  `g_LastSentimentSaveTime = time(nullptr)`. Idempotent — safe to re-run on
  reload (Load clears first).

### `mod-ollama-chat_events.cpp` (sentiment-relevant paths)

#### `OllamaBotEventChatter::BuildPrompt(Player* bot, std::string promptTemplate, std::string eventType, std::string eventDetail, std::string actorName)`
Assembles the event-chatter prompt. The sentiment-specific block (`events.cpp
:444-454`):
```cpp
std::string sentimentInfo = "";
if (g_EnableSentimentTracking && !actorName.empty())
{
    Player* actorPlayer = ObjectAccessor::FindPlayerByName(actorName);
    if (actorPlayer)
        sentimentInfo = GetSentimentPromptAddition(bot, actorPlayer);
}
```
- The actor is resolved **by name** (`FindPlayerByName`), not GUID — if the
  actor has logged out, `sentimentInfo` stays `""`.
- The result is passed to the template as `fmt::arg("sentiment_info",
  sentimentInfo)`.

#### `ChatOnDuel::OnPlayerDuelEnd(Player* winner, Player* loser, DuelCompleteType /*type*/)`
Registered as a `PlayerScript` hook. On a completed duel:
```cpp
NudgeSentimentPair(winner, loser, -g_SentimentEventDuelAdjustment);
RecordMemoryForPair(winner, loser, "duel", "dueled them");
eventChatter.DispatchGameEvent(winner, g_EventTypeWonDuel, loser->GetName());
```
- **Negative** delta — duels breed rivalry; both directions drop by
  `g_SentimentEventDuelAdjustment` (default 0.03).
- Fires only at duel *end*, not on request/start (those only drive event
  chatter).

#### `SentimentOnGroup::OnAddMember(Group* group, ObjectGuid guid)`
A `GroupScript` (not a `PlayerScript`). On any group add:
- Guards: `group` non-null, tracking enabled, `g_SentimentEventGroupAdjustment
  != 0.0f`.
- Resolves `newMember = ObjectAccessor::FindPlayer(guid)`; bails if null.
- Iterates `group->GetMemberSlots()`, skips the joining `guid`, resolves each
  existing member via `FindPlayer`, and for each:
  `NudgeSentimentPair(newMember, member, +g_SentimentEventGroupAdjustment)`
  (default 0.02) plus `RecordMemoryForPair(newMember, member, "group", "grouped
  up with them")`.

#### `ChatOnGuildMemberChange::OnGuildMemberJoin(Player* player, Guild* /*guild*/)`
`PlayerScript` hook. The sentiment block runs before event chatter:
- Guards: `player` and `player->GetGuild()` non-null; tracking enabled;
  `g_SentimentEventGuildAdjustment != 0.0f`.
- Reads `guildId = player->GetGuildId()`, then scans **all** online players
  (`ObjectAccessor::GetPlayers()`), keeping those with `IsInWorld()` and
  `GetGuildId() == guildId` (excluding the joiner), and for each:
  `NudgeSentimentPair(player, member, +g_SentimentEventGuildAdjustment)`
  (default 0.01) plus `RecordMemoryForPair(player, member, "guild", "became
  guildmates with them")`.
- The sibling hooks `OnGuildMemberLeave`, `OnGuildMemberRankChange`,
  `OnGuildMemberLogin` only drive event chatter — no sentiment nudge.

> **Registration bug-fix.** Neither `ChatOnGuildMemberChange` nor
> `SentimentOnGroup` was instantiated in upstream `mod-ollama-chat_main.cpp`;
> `git diff HEAD` shows `new ChatOnGuildMemberChange();` and `new
> SentimentOnGroup();` as local additions to `Addmod_ollama_chatScripts()`
> (`main.cpp:26-27`). Without those two `new …;` calls the guild-join and
> group-add nudges (and their memories) are dead code — the classes compile and
> exist but their hooks never fire. This is why the duel nudge (registered via
> the already-present `ChatOnDuel`) worked upstream but the group/guild nudges
> did not until this patch.

---

## 4. Data structures & DB

### In-memory (defined in `mod-ollama-chat_config.cpp:151-153`, extern in `_config.h:256-258`)
| Symbol | Type | Meaning |
|---|---|---|
| `g_BotPlayerSentiments` | `std::unordered_map<uint64_t, std::unordered_map<uint64_t, float>>` | `botGuid → (playerGuid → value)`. Nested, directional. |
| `g_SentimentMutex` | `std::mutex` | Guards every read/write of the map. |
| `g_LastSentimentSaveTime` | `time_t` | Wall-clock of the last DB flush; drives the periodic save. |

Keys are `ObjectGuid::GetRawValue()` (full 64-bit raw GUID). For `Player`
objects `HighGuid::Player == 0x0000` (`ObjectGuid.h:61`), so the raw value
equals the low counter — which is why `../BOT-BEHAVIOR.md` and the
`.ollama sentiment view` output describe these as guid *counters*. Both bots and
real players are `Player` objects, so the same key space covers all four pair
kinds.

### DB table — `acore_characters.mod_ollama_chat_bot_player_sentiments`
Created by `data/sql/characters/base/2025_07_24_sentiment_tracking.sql`:

| Column | Type | Written by code? |
|---|---|---|
| `id` | `BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY` | No (MySQL) |
| `bot_guid` | `BIGINT UNSIGNED` | Yes |
| `player_guid` | `BIGINT UNSIGNED` | Yes |
| `sentiment_value` | `FLOAT DEFAULT 0.5` | Yes (`{:.3f}`) |
| `last_updated` | `DATETIME … ON UPDATE CURRENT_TIMESTAMP` | No (MySQL) |
| `created_at` | `DATETIME DEFAULT CURRENT_TIMESTAMP` | No (MySQL) |

Constraints: `UNIQUE KEY unique_sentiment (bot_guid, player_guid)` (the target
of `REPLACE INTO`), plus indexes `idx_bot_guid`, `idx_player_guid`,
`idx_sentiment_value`. Only the three logical columns are ever read
(`LoadBotPlayerSentimentsFromDB`) or written (`SaveBotPlayerSentimentsToDB`).

---

## 5. Concurrency & threading

**Two threads mutate the map.**
- **World thread:** the world-event hooks (`ChatOnDuel::OnPlayerDuelEnd`,
  `SentimentOnGroup::OnAddMember`, `ChatOnGuildMemberChange::OnGuildMemberJoin`)
  and the periodic `SaveBotPlayerSentimentsToDB` (driven from
  `OllamaBotRandomChatter`'s world-update in `random.cpp`), plus
  `LoadBotPlayerSentimentsFromDB` / `InitializeSentimentTracking` at
  startup/reload and the `.ollama sentiment view` command.
- **Detached worker thread:** `UpdateBotPlayerSentiment` (hence
  `AnalyzeMessageSentiment`) runs inside the bot-response thread spawned by the
  chat handler (`handler.cpp`, the block whose catch logs *"Exception in bot
  response thread"*). It performs a **blocking HTTP** `QueryOllamaAPI`, so it
  *must not* run on the world thread — the design deliberately keeps the LLM
  classification off-tick.

**Mutex discipline.** Every touch of `g_BotPlayerSentiments` takes
`g_SentimentMutex` — `GetBotPlayerSentiment`, `SetBotPlayerSentiment`, `Load*`,
`Save*`, and the view command all lock. Each individual `Get`/`Set` is
therefore data-race-free.

**Non-atomic read-modify-write (known race).** `NudgeSentimentPair` and
`UpdateBotPlayerSentiment` both do `Get → compute → Set`, releasing the lock
between the read and the write. If the world thread nudges a pair (e.g. a duel)
while the worker thread is mid-`UpdateBotPlayerSentiment` on the same pair, one
delta can be lost. In practice deltas are tiny (≤0.1) and eventually re-applied,
so this is tolerated rather than fixed; it is the main thing to be aware of when
modifying this code.

**Lock held across DB writes.** `SaveBotPlayerSentimentsToDB` holds
`g_SentimentMutex` while issuing one `CharacterDatabase.Execute` per pair. The
`Execute` calls are async (queued to the DB worker, non-blocking on the world
thread), but the map stays locked for the whole loop — a large sentiment table
briefly blocks chat-thread reads. No cache beyond the map itself; the DB is
write-through-on-timer, not per-change.

---

## 6. Config keys

Loaded in `LoadOllamaChatConfig` (`mod-ollama-chat_config.cpp:497-505`). All
under the `OllamaChat.` prefix.

| Key | Default | Global |
|---|---|---|
| `OllamaChat.EnableSentimentTracking` | `true` | `g_EnableSentimentTracking` |
| `OllamaChat.SentimentDefaultValue` | `0.5` | `g_SentimentDefaultValue` |
| `OllamaChat.SentimentAdjustmentStrength` | `0.1` | `g_SentimentAdjustmentStrength` |
| `OllamaChat.SentimentSaveInterval` | `10` (minutes) | `g_SentimentSaveInterval` |
| `OllamaChat.SentimentAnalysisPrompt` | `Analyze the sentiment of this message: "{message}". Respond only with: POSITIVE, NEGATIVE, or NEUTRAL.` | `g_SentimentAnalysisPrompt` |
| `OllamaChat.SentimentPromptTemplate` | `Your relationship sentiment with {player_name} is {sentiment_value} (0.0=hostile, 0.5=neutral, 1.0=friendly). Use this to guide your tone and response.` | `g_SentimentPromptTemplate` |
| `OllamaChat.SentimentEventGroupAdjustment` | `0.02` | `g_SentimentEventGroupAdjustment` |
| `OllamaChat.SentimentEventGuildAdjustment` | `0.01` | `g_SentimentEventGuildAdjustment` |
| `OllamaChat.SentimentEventDuelAdjustment` | `0.03` | `g_SentimentEventDuelAdjustment` |

Notes: `SentimentAnalysisPrompt` must contain `{message}`;
`SentimentPromptTemplate` may use `{player_name}` and `{sentiment_value}` (and
if set empty, `GetSentimentPromptAddition` short-circuits to `""`). The three
`SentimentEvent*Adjustment` values are the raw magnitudes; the duel hook applies
the **negation** (`-g_SentimentEventDuelAdjustment`).

---

## 7. Failure modes & gotchas

- **Master kill-switch.** `g_EnableSentimentTracking == false` makes every
  reader return `g_SentimentDefaultValue`, every mutator a no-op, and
  `GetSentimentPromptAddition` return `""`. `Initialize`/`Load`/`Save` also
  no-op. Toggling it off then on at runtime (via `.ollama reload`) triggers a
  fresh `LoadBotPlayerSentimentsFromDB` (which `clear()`s first).
- **No decay.** Sentiment only moves on messages and the three events; it never
  drifts back toward 0.5. A single rude message leaves a lasting −0.1 until
  something pushes back.
- **Clamp is the only bound.** Callers add unbounded deltas; correctness depends
  entirely on `SetBotPlayerSentiment`'s `[0,1]` clamp. A future caller that
  writes the map directly would bypass it.
- **Substring classification.** `AnalyzeMessageSentiment` matches `"POSITIVE"` /
  `"NEGATIVE"` as substrings of the uppercased response; a chatty model that
  answers "This is not positive" would still score POSITIVE. Empty message,
  empty API response, or `NEUTRAL`/unrecognised all yield `0.0` (no change) —
  the graceful path when Ollama is down or terse.
- **Actor resolved by name, not GUID.** `BuildPrompt` uses
  `ObjectAccessor::FindPlayerByName(actorName)`; if the actor logged out between
  the event and the (threaded, delayed) prompt build, `{sentiment_info}` is
  simply blank rather than an error.
- **GUID reacquire in the threaded event path.** The event-chatter worker
  (`QueueEvent`) never stores a raw `Player*` across the thread hop or typing
  delay — it re-resolves via `ObjectAccessor::FindPlayer(ObjectGuid(botGuid))`
  and bails on null. The sentiment functions themselves take live `Player*` and
  are always called on the thread that already holds a valid pointer.
- **Registration was the real bug.** As in Section 3, group/guild nudges depended on
  `new SentimentOnGroup();` and `new ChatOnGuildMemberChange();` being added to
  `main.cpp`. If someone reverts those lines the code still builds but silently
  stops recording group/guild relationships (and their memories).
- **No `information_schema` probing / weak symbols here.** Unlike the
  playerbots-integration glue elsewhere in this module, the sentiment subsystem
  has no schema sniffing or weak-symbol fallbacks — its graceful degradation is
  entirely the `g_EnableSentimentTracking` guards, null checks, empty-template
  checks, and GUID-reacquire in the shared event thread.
- **Symmetric nudge covers non-consumed pairs.** `NudgeSentimentPair` writes
  real↔real and bot↔bot rows that nothing ever reads back (only bots consume
  sentiment). Harmless, but it inflates the table; `RecordMemoryForPair` (a
  separate call at each event site) is stricter and only records when exactly
  one side is a bot.

---

## 8. Cross-references

- [`../BOT-BEHAVIOR.md`](../BOT-BEHAVIOR.md) Section 4 — behavior-level framing of
  sentiment (terminology this doc mirrors); Section 6 for the quest-help invite gate
  that reads sentiment.
- [`../BOT-ECONOMY.md`](../BOT-ECONOMY.md) — personality-driven gameplay that
  shares the same event hooks and prompt-template plumbing.
- Companion internals docs in this directory (as they are written): the
  **event-chatter** pipeline (`OllamaBotEventChatter::DispatchGameEvent` /
  `QueueEvent` in `mod-ollama-chat_events.cpp`), the **memory** subsystem
  (`RecordMemoryForPair` / `BuildMemoryInfo` in `mod-ollama-chat_memory.cpp`,
  which is invoked alongside every sentiment nudge), and the **prompt
  assembly** path in `mod-ollama-chat_handler.cpp` that consumes
  `{sentiment_info}`.
