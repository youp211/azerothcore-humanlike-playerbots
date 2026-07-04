# Internals 02 — Inbound Chat Pipeline & Prompt Assembly

Developer reference for the *inbound* half of mod-ollama-chat: the path from a
player (or bot) sending a chat line, through candidate-bot selection and the
reply-chance roll, to prompt assembly and the detached worker thread that calls
Ollama and speaks the reply. Terminology matches
[BOT-BEHAVIOR.md](../BOT-BEHAVIOR.md) (personalities Section 2, sentiment Section 4, gear
context Section 5, group-join Section 10, channels Section 11) and
[BOT-ECONOMY.md](../BOT-ECONOMY.md); this doc goes one level deeper — function
by function, for someone modifying or debugging the code.

All code below lives in
`azerothcore-wotlk/modules/mod-ollama-chat/src/mod-ollama-chat_handler.cpp`
unless another file is named. Config globals and their loaders live in
`mod-ollama-chat_config.cpp` / `.h`.

---

## 1. Purpose

When a real player types in say/yell/party/raid/guild/officer/whisper or a
public channel, this subsystem decides which nearby playerbots should answer,
builds a per-bot LLM prompt packed with world/personality/relationship context,
and — on a detached thread so the world tick never blocks on Ollama — delivers
each bot's generated reply through the correct chat channel. Bot replies
re-enter the same path so bots can converse with each other, bounded by
per-channel-type reply chances.

---

## 2. Entry points & call graph

Registration: `Addmod_ollama_chatScripts()` in `mod-ollama-chat_main.cpp`
constructs `new PlayerBotChatHandler();` (a `PlayerScript`) and
`new OllamaChatConfigWorldScript();` (a `WorldScript`). The handler subscribes
to the `PLAYERHOOK_CAN_PLAYER_USE_*` chat hooks (see the constructor in
`mod-ollama-chat_handler.h`).

Every inbound line lands in one of five `OnPlayerCanUseChat` overloads, all of
which funnel into the static `ProcessChat`:

```
Core chat hook (world thread)
  └─ PlayerBotChatHandler::OnPlayerCanUseChat(...)        [5 overloads]
       └─ GetChannelSourceLocal(type)  ── uint32 chat type → SRC_*_LOCAL enum
       └─ ProcessChat(player, type, lang, msg, sourceLocal, channel, receiver)
            ├─ blacklist / channel-disable / language guards
            ├─ RecordChannelLine(...)          (General/Trade session log — channels.cpp)
            ├─ gather eligibleBots  ── per-source: whisper / channel / say-yell-guild-party
            │     └─ IsBotEligibleForChatChannelLocal(...)   (non-channel sources)
            ├─ candidateBots  ── combat filter + eligibility
            ├─ pick reply chance  ── g_{Player,Bot}ReplyChance_{Say,Party,Guild,Channel}
            ├─ finalCandidates:
            │     ├─ whisper → candidateBots[0]
            │     ├─ name mentioned → first-mentioned bot (skips the roll)
            │     └─ else per-bot roll:
            │           GetPersonalityReplyChanceMultiplier(GetBotPersonality(bot))
            │           ShouldBotReplyInChannel(...)  (General only — channels.cpp)
            │           urand(0,99) < effChance
            ├─ cap to g_MaxBotsToPick (shuffle + urand)
            └─ for each finalCandidate (still world thread):
                 ├─ EvaluateGroupRequest(...)        (proximity chat — groupjoin.cpp)
                 ├─ BuildChannelHistory(...)         (General — channels.cpp)
                 ├─ prompt = GenerateBotPrompt(bot, msg, player, promptContext, isWhoMe)
                 │     ├─ GetBotHistoryPrompt(...)   {chat_history}
                 │     ├─ GetSentimentPromptAddition(...)  {sentiment_info}   (sentiment.cpp)
                 │     ├─ BuildMemoryInfo(...)        {memory_info}           (memory.cpp)
                 │     ├─ GenerateGearContext(...)    {gear_context}   (unless suppressed)
                 │     ├─ g_RAGSystem->RetrieveRelevantInfo(...)  (rag.cpp, optional)
                 │     └─ GenerateBotGameStateSnapshot(...)  (optional, appended)
                 ├─ queryOpts = GetPersonalityQueryOptions(bot)   (personality.cpp)
                 └─ std::thread([...]{ ... }).detach()      ◄── leaves the world thread
                      ├─ SubmitQuery(prompt, queryOpts)   (querymanager/api)
                      ├─ future.get()                     ── blocks the WORKER, not the world
                      ├─ reacquire Player* by GUID (ObjectAccessor::FindPlayer)
                      ├─ typing-sim sleep + reacquire     (g_EnableTypingSimulation)
                      ├─ who-me stagger sleep + reacquire (isWhoMe)
                      ├─ deliver: targetChannel->Say / botAI->Say/Yell/SayToGuild/…/Whisper
                      ├─ MarkChannelReply(...) + ProcessBotChatMessage(...)  (re-enters ProcessChat)
                      ├─ UpdateBotPlayerSentiment(...)     (sentiment.cpp)
                      └─ AppendBotConversation(...)        (in-RAM history)
```

A parallel, self-contained entry point exists for scripted lines:
`ProcessBotChatMessage(bot, msg, sourceLocal, channel)` validates that the bot
is really in the relevant chat group, then calls `ProcessChat` with the **bot**
as sender — this is how a bot's own reply triggers *other* bots. And
`OllamaChat_SpeakSituation(...)` is a standalone async one-liner generator
called cross-module by mod-playerbots (see Section 3 and BOT-ECONOMY Section 4).

---

## 3. Function-by-function

### `ChatChannelSourceLocal GetChannelSourceLocal(uint32_t type)`

Maps a core `CHAT_MSG_*` type to the module's local source enum. Collapses
leader/warning variants: `CHAT_MSG_PARTY`/`_PARTY_LEADER` → `SRC_PARTY_LOCAL`;
`CHAT_MSG_RAID`/`_RAID_LEADER`/`_RAID_WARNING` → `SRC_RAID_LOCAL`;
`CHAT_MSG_WHISPER`/`_WHISPER_FOREIGN`/`_WHISPER_INFORM` → `SRC_WHISPER_LOCAL`;
`CHAT_MSG_CHANNEL` → `SRC_GENERAL_LOCAL`. Anything else →
`SRC_UNDEFINED_LOCAL`. The enum (in `mod-ollama-chat_handler.h`) is sparse:
Say=1, Party=2, Raid=3, Guild=4, Officer=5, Yell=6, Whisper=7, **General=17**
(values 8–16 unused). `ChatChannelSourceLocalStr[]` is the debug-label array;
it is indexed 0..17, so it is only safe for the values the enum actually
produces.

### `PlayerBotChatHandler::OnPlayerCanUseChat(...)` — 5 overloads

Signatures (from `mod-ollama-chat_handler.h`):

```cpp
bool OnPlayerCanUseChat(Player* player, uint32_t type, uint32_t lang, std::string& msg);
bool OnPlayerCanUseChat(Player* player, uint32_t type, uint32_t lang, std::string& msg, Group* group);
bool OnPlayerCanUseChat(Player* player, uint32_t type, uint32_t lang, std::string& msg, Guild* guild);
bool OnPlayerCanUseChat(Player* player, uint32_t type, uint32_t lang, std::string& msg, Channel* channel);
bool OnPlayerCanUseChat(Player* player, uint32_t type, uint32_t lang, std::string& msg, Player* receiver);
```

Each short-circuits `return true` if `!g_Enable`, computes `sourceLocal =
GetChannelSourceLocal(type)`, and calls `ProcessChat`. The `Group*`/`Guild*`
overloads ignore their extra argument (`/*group*/`, `/*guild*/`) — source is
derived from `type`, not the object. The `Channel*` overload forwards the
channel; the `Player* receiver` overload forwards the receiver.

The **whisper (`receiver`) overload** does extra gating *before* `ProcessChat`:
for `type == CHAT_MSG_WHISPER` it requires a valid, distinct `receiver`; it
bails (`return true`, no bot reply) if the **sender** is a bot
(`senderAI->IsBotAI()`), and only proceeds if the **receiver** is a bot. All
overloads always `return true` (the message is never suppressed). **Gotcha:**
the comment above the final `return true` in the receiver overload reads
"Return false to prevent the message from being processed again in
OnPlayerChat" but the code returns `true` — the intended double-process guard
is actually that only these `CanUseChat` hooks are registered (no
`OnPlayerChat`), so there is no second pass to suppress.

### `void PlayerBotChatHandler::ProcessChat(Player* player, uint32_t type, uint32_t lang, std::string& msg, ChatChannelSourceLocal sourceLocal, Channel* channel, Player* receiver)`

The core dispatcher. Runs entirely on the **world thread**. Steps:

1. **Guards**: null `player` (logs error), empty `msg`, `lang == LANG_ADDON`
   (addon traffic silently ignored).
2. **Blacklist**: `rtrim(msg)` (trims trailing ` \t\n\r,.!?;:`) then, for each
   prefix in `g_BlacklistCommands` (defaults `.playerbots`, `playerbot`, plus
   `OllamaChat.BlacklistCommands`), a word-boundary `startsWithWord` check —
   skip the whole message if it is a command.
3. **Channel-disable toggles**: `g_DisableForCustomChannels`
   (`SRC_GENERAL_LOCAL`), `g_DisableForSayYell`, `g_DisableForGuild`,
   `g_DisableForParty` each early-return.
4. **Channel session log**: if `SRC_GENERAL_LOCAL` and `channelId != 0`,
   `RecordChannelLine(channelId, player->GetName(), msg)` records the line
   (RAM-only, `mod-ollama-chat_channels.cpp`) — for players *and* bots, since
   bot replies re-enter here.
5. **Sender classification**: `senderAI = PlayerbotsMgr::instance().GetPlayerbotAI(player)`;
   `senderIsBot = senderAI && senderAI->IsBotAI()`.
6. **Gather `eligibleBots`** (`std::vector<Player*>`) — three disjoint branches:
   - **Whisper** (`SRC_WHISPER_LOCAL && receiver`): bail if
     `!g_EnableWhisperReplies` or `senderIsBot`; if the receiver has bot AI,
     `eligibleBots = { receiver }`.
   - **Channel** (`channel != nullptr`): iterate `ObjectAccessor::GetPlayers()`,
     keep only bots that pass **local-vs-global** classification (name contains
     `"General -"`/`"Trade -"`/`"LocalDefense -"` = local, needs same
     `GetZoneId()`; `"World"`/`"LookingForGroup"` = global, no zone check),
     `candidate->IsInChannel(channel)`, faction match (unless global channel),
     and a **real-player-in-channel** check. (The `IsInChannel(channel)` test
     appears twice — the second is redundant dead code.)
   - **Everything else** (say/yell/guild/officer/party/raid): iterate all
     in-world bots; for guild/officer require a real online guildmate, for
     party/raid require a real group member, for say/yell require a real player
     within `g_SayDistance`/`g_YellDistance`.
7. **`candidateBots`**: for channel sources (or `SRC_GENERAL_LOCAL` even with
   `channel == nullptr`, the bot-initiated case) bots pass through unchecked
   (they already survived the strict gathering); for non-channel sources each
   bot must pass `IsBotEligibleForChatChannelLocal(bot, player, sourceLocal,
   channel, receiver)`.
8. **Reply chance** selection by source, keyed on `senderIsBot`:

   | source | player-sender | bot-sender |
   |---|---|---|
   | Say/Yell | `g_PlayerReplyChance_Say` | `g_BotReplyChance_Say` |
   | Party/Raid | `g_PlayerReplyChance_Party` | `g_BotReplyChance_Party` |
   | Guild/Officer | `g_PlayerReplyChance_Guild` | `g_BotReplyChance_Guild` |
   | General | `g_PlayerReplyChance_Channel` | `g_BotReplyChance_Channel` |
   | default (incl. whisper) | `g_PlayerReplyChance_Say` | `g_BotReplyChance_Say` |

9. **`finalCandidates`** selection:
   - **Whisper**: take `candidateBots[0]` (there is only one), unless
     `g_DisableRepliesInCombat && whisperBot->IsInCombat()`. **The reply-chance
     roll is bypassed for whispers** — a whispered bot always answers.
   - **Non-whisper**: first scan `candidateBots` for a **name mention** —
     `isBotNameMentioned` does a case-insensitive, word-boundary search of the
     bot's name in `trimmedMsg`, returning the match position. If any bot is
     mentioned, sort by position and select only the **first-mentioned** bot
     (combat-gated) — **this bypasses the chance roll, the personality
     multiplier, and the channel gate**. If no bot is mentioned, each candidate
     rolls:
     ```cpp
     float personalityMult = GetPersonalityReplyChanceMultiplier(GetBotPersonality(bot));
     uint32_t effChance = std::min<uint32_t>(100u, static_cast<uint32_t>(chance * personalityMult + 0.5f));
     if (sourceLocal == SRC_GENERAL_LOCAL && !ShouldBotReplyInChannel(bot, msg, effChance))
         continue;                       // channel gate can also *lower* effChance in place
     uint32_t roll = urand(0, 99);
     if (roll < effChance) finalCandidates.push_back(bot);
     ```
     Combat-gated throughout via `g_DisableRepliesInCombat`.
10. **Cap**: if `finalCandidates.size() > g_MaxBotsToPick`, `std::shuffle`
    (with a local `std::mt19937` seeded by `std::random_device`) then resize to
    `urand(1, g_MaxBotsToPick)` — a *random count up to the cap*, not exactly
    the cap.
11. **Per-bot dispatch loop** (still world thread) — for each final bot:
    - `proximityChat` = say/yell/whisper AND `bot->IsInMap(player)` AND
      `bot->GetDistance(player) <= g_SayDistance`.
    - If `!senderIsBot && proximityChat`, `EvaluateGroupRequest(bot, player,
      msg, finalCandidates.size(), groupJoinContext)` runs the group-join
      decision (sends a real invite on `Invited`; `groupJoinContext` is the
      prompt directive). `isWhoMe = (outcome == GroupJoinOutcome::AskWhoMe)`.
    - `promptContext = groupJoinContext`; for General channels, append
      `BuildChannelHistory(channel->GetChannelId())` (recent channel lines).
    - `prompt = GenerateBotPrompt(bot, msg, player, promptContext, isWhoMe)`
      (the last arg suppresses `{gear_context}` on who-me).
    - `queryOpts = GetPersonalityQueryOptions(bot)`.
    - Spawn the detached worker (see Section 5) capturing `botGuid`, `senderGuid`,
      `prompt`, `queryOpts`, `isWhoMe`, `sourceLocal`, `channelId`,
      `channelName`, `msg` — **all by value; no raw `Player*` crosses the
      thread boundary.**

Inputs: the raw chat event. Outputs: none (side effects only — spawns worker
threads, records channel lines, may fire group invites). `type` is unused
(`/*type*/`).

### `void ProcessBotChatMessage(Player* bot, const std::string& msg, ChatChannelSourceLocal sourceLocal, Channel* channel)`

Bridge that lets a bot's *own* utterance trigger other bots. Called from the
worker thread after a bot speaks (and from random/event chatter code). Bails on
null bot / empty msg. If `channel == nullptr && SRC_GENERAL_LOCAL`, looks up the
`"General"` channel for the bot's team. Then a `canSendMessage` switch validates
the bot is genuinely in the relevant group: General needs a channel object;
guild/officer needs a guild with a real player online; party/raid needs a group
with a real player; say/yell/whisper pass. On success it converts `sourceLocal`
back to a `CHAT_MSG_*` type, picks language from faction
(`TEAM_ALLIANCE ? LANG_COMMON : LANG_ORCISH`), and calls
`PlayerBotChatHandler::ProcessChat(bot, type, lang, mutableMsg, sourceLocal,
channel, nullptr)` — now with `senderIsBot == true`, so the *bot* reply chances
apply, throttling bot-to-bot cascades.

### `static bool IsBotEligibleForChatChannelLocal(Player* bot, Player* player, ChatChannelSourceLocal source, Channel* channel, Player* receiver)`

Per-bot eligibility for **non-channel** sources (channel sources are pre-filtered
in `ProcessChat`). Returns false on null/self/no-`PlayerbotAI`. Whisper: reject
bot senders, require `bot == receiver`. Non-proximity, non-channel sources
require same `GetTeamId()` (say/yell are proximity and skip the faction rule).
If a `channel` is supplied it re-verifies the exact channel instance via
`ChannelMgr::forTeam(bot->GetTeamId())->GetChannel(name, bot) == channel` and
cross-faction global-channel rules. Final `switch (source)`:

- `SRC_SAY_LOCAL`: both in world, `bot->GetDistance(player) <= g_SayDistance`.
- `SRC_YELL_LOCAL`: `player->GetDistance(bot) <= g_YellDistance`.
- `SRC_GUILD_LOCAL`/`SRC_OFFICER_LOCAL`: same `GetGuildId()`.
- `SRC_PARTY_LOCAL`/`SRC_RAID_LOCAL`: `isInParty` (same `GetGroup()` pointer).
- `SRC_WHISPER_LOCAL`: `bot == receiver`.
- `SRC_GENERAL_LOCAL`: `true` (membership already checked).

A `threshold <= 0.0f` disables the corresponding proximity source entirely
(returns false / no yell).

### `std::string GenerateBotPrompt(Player* bot, std::string playerMessage, Player* player, std::string const& extraContext = "", bool suppressGearContext = false)`

Assembles the full LLM prompt. Runs on the **world thread** (it reads live
`Player`/inventory/map state, and `GenerateGearContext` may mail items — both
require thread safety). Returns `""` on any null `bot`/`player`/`PlayerbotAI`/
`ChatHelper`, or if `g_ChatPromptTemplate` is empty (logs error). Sequence:

1. **Bot facts**: personality via `GetBotPersonality(bot)` →
   `GetPersonalityPromptAddition(personality)`; name, level, gender, area/zone
   (`botAI->GetLocalizedAreaName(...)`), map, class/race (`ChatHelper::FormatClass/FormatRace`),
   spec role (`ChatHelper::FormatClass(bot, AiFactory::GetPlayerSpecTab(bot))`),
   faction, guild, group status, gold (`GetMoney() / 10000`).
2. **Player facts**: symmetric set, plus `playerDistance` (or `-1.0f` if either
   is not in world).
3. **Context blocks**:
   - `chatHistory = GetBotHistoryPrompt(botGuid, playerGuid, playerMessage)` →
     `{chat_history}`
   - `sentimentInfo = GetSentimentPromptAddition(bot, player)` →
     `{sentiment_info}` (`mod-ollama-chat_sentiment.cpp`)
   - `memoryInfo = BuildMemoryInfo(bot, player)` → `{memory_info}`
     (`mod-ollama-chat_memory.cpp`)
   - `ragInfo` if `g_EnableRAG && g_RAGSystem`:
     `RetrieveRelevantInfo(playerMessage, g_RAGMaxRetrievedItems,
     g_RAGSimilarityThreshold)` formatted through `g_RAGPromptTemplate`.
4. **`extra_info`**: `SafeFormat(g_ChatExtraInfoTemplate, ...)` with args
   `bot_race, bot_gender, bot_role, bot_faction, bot_guild, bot_group_status,
   bot_gold, player_race, player_gender, player_role, player_faction,
   player_guild, player_group_status, player_gold, player_distance, bot_area,
   bot_zone, bot_map`.
5. **Main template**: `SafeFormat(g_ChatPromptTemplate, ...)` with the
   placeholders `bot_name, bot_level, bot_class, bot_personality`
   (the prompt text), `bot_personality_name` (the key), `player_level,
   player_class, player_name, player_message, extra_info, chat_history,
   sentiment_info, memory_info,` and `gear_context` —
   `suppressGearContext ? "" : GenerateGearContext(bot, player)`.
6. **Appends** (raw, not through the formatter): `ragInfo`; if
   `g_EnableChatBotSnapshotTemplate`, `GenerateBotGameStateSnapshot(bot)`; then
   `extraContext` (the group-join / who-me directive) last so it is the final
   instruction the model reads.
7. If `g_DebugEnabled && g_DebugShowFullPrompt`, logs the entire prompt.

All substitution goes through `SafeFormat` (`mod-ollama-chat-utilities.h`),
which wraps `fmt::vformat` in a try/catch and returns `"[Format Error]"` on a
bad template instead of throwing — a malformed `.conf` template degrades to a
visible marker, not a crash.

### `std::string GetBotHistoryPrompt(uint64_t botGuid, uint64_t playerGuid, std::string playerMessage)`

Returns `""` if `!g_EnableChatHistory`. Under `g_ConversationHistoryMutex`,
looks up `g_BotConversationHistory[botGuid][playerGuid]`; if absent returns
empty. Resolves the player name (`ObjectAccessor::FindPlayer`), then formats
`g_ChatHistoryHeaderTemplate` + one `g_ChatHistoryLineTemplate` per stored
`(player_message, bot_reply)` pair + `g_ChatHistoryFooterTemplate` (which
carries the *current* `playerMessage`). This is the running transcript fed back
into `{chat_history}`.

### `static std::string GenerateBotGameStateSnapshot(Player* bot)` and helpers

Optional block appended when `g_EnableChatBotSnapshotTemplate`. Formats
`g_ChatBotSnapshotTemplate` with six named sections built by these helpers
(all read-only over live game state, all world-thread):

- `ChatHandler_GetCombatSummary(bot)` — in-combat flag, current victim
  (name/level/HP), and class-appropriate resource (rage/energy/runic
  power/focus/mana).
- `ChatHandler_GetGroupStatus(bot)` — per group member: name, level, class,
  race, HP, distance, and "under attack by" info.
- `ChatHandler_GetBotSpellInfo(bot)` — non-passive, non-generic,
  off-cooldown spells collapsed to highest rank, with resource cost text.
- quest loop over `bot->getQuestStatusMap()` — localized title + readable
  status.
- `ChatHandler_GetVisibleLocations(bot, 40.0f)` — nearby creatures
  (ENEMY/FRIENDLY/NEUTRAL/DEAD) and game objects within LOS.
- `ChatHandler_GetVisiblePlayers(bot, 40.0f)` — nearby non-GM players in LOS.

### `GenerateGearContext(Player* bot, Player* player)` — where it hooks in

Called only from `GenerateBotPrompt` for `{gear_context}` (suppressed on
who-me). It inspects the player's 9 armor slots, may **mail or park a real gear
give**, and returns the inspect string. Full behavior is documented in
BOT-BEHAVIOR Section 5 and BOT-ECONOMY Section 1–2 — not repeated here. What matters for this
pipeline: it runs **synchronously on the world thread inside prompt assembly**
(item mail/DB writes must not be on the worker), it is throttled by
`GearCommentaryAllowed` / `CanGiveGearNow`, and it never claims a give the code
did not actually perform.

### The detached worker lambda (inside `ProcessChat`)

Captured by value: `botGuid, senderGuid, prompt, queryOpts, isWhoMe,
sourceLocal, channelId, channelName, msg`. Body (wrapped in try/catch that logs
`ex.what()` under debug):

1. `SubmitQuery(prompt, queryOpts)` → `std::future<std::string>`; bail if
   `!valid()`. `response = responseFuture.get()` **blocks the worker** (bounded
   by the query manager's concurrency and `g_OllamaRequestTimeout`).
2. **Reacquire by GUID**: `ObjectAccessor::FindPlayer(ObjectGuid(botGuid))` and
   `...(senderGuid)`; bail if either is gone. Bail if `response.empty()` (API
   error). Fetch `botAI = PlayerbotsMgr::instance().GetPlayerbotAI(botPtr)`.
3. **Typing simulation** (`g_EnableTypingSimulation`): sleep
   `g_TypingSimulationBaseDelay + response.length() * g_TypingSimulationDelayPerChar`
   ms, then **reacquire bot/AI/sender again** (they can log out during the
   sleep).
4. **Who-me stagger** (`isWhoMe`): sleep `urand(800, 5000)` ms so several bots
   don't answer "who, me?" in unison, then reacquire again.
5. **Delivery** by route:
   - Channel (`channelId != 0 && !channelName.empty()`):
     `ChannelMgr::forTeam(botPtr->GetTeamId())->GetChannel(channelName,
     botPtr)`, confirm `botPtr->IsInChannel(targetChannel)`, then
     `targetChannel->Say(botPtr->GetGUID(), response, LANG_UNIVERSAL)`,
     `MarkChannelReply(botPtr)` (starts the per-bot channel cooldown), and
     `ProcessBotChatMessage(botPtr, response, SRC_GENERAL_LOCAL,
     targetChannel)`. No Say fallback — if the channel/membership check fails
     the reply is dropped.
   - Otherwise a `switch (sourceLocal)`: `botAI->SayToGuild` (guild/officer),
     `SayToParty`, `SayToRaid`, `botAI->Say` (say — only if a player is within
     `g_SayDistance`), `botAI->Yell` (yell — within `g_YellDistance`),
     `botAI->Whisper(response, originalSender->GetName())` (whisper — reacquires
     the sender by `senderGuid`), default `botAI->Say`. Every branch except
     whisper calls `ProcessBotChatMessage(...)` to let other bots react;
     whispers deliberately do **not** (they're private).
6. **Post-delivery**: `UpdateBotPlayerSentiment(botPtr, senderPtr, msg)`
   (relationship nudge) and `AppendBotConversation(botGuid, senderGuid, msg,
   response)` (RAM history).

### `void OllamaChat_SpeakSituation(Player* bot, Player* target, std::string const& situation, bool whisper)`

External-linkage helper (no shared header) that mod-playerbots calls via a weak
symbol (BOT-ECONOMY Section 4). Guards on `g_Enable` and a live `PlayerbotAI`. Builds
a one-shot prompt from name/level/class + personality prompt + the `situation`
string ("Say one short in-character line ... under 15 words. No narration, no
quotes"). Uses `GetPersonalityQueryOptions(bot)`. Captures **raw GUIDs** into a
detached thread that `SubmitQuery`s, reacquires the players, and either
`bot->Whisper(response, LANG_UNIVERSAL, target)` or `botAI->Say(response)`.
Same fire-and-forget shape as the main worker; all exceptions swallowed.

### `AppendBotConversation` / `SaveBotConversationHistoryToDB`

`AppendBotConversation(botGuid, playerGuid, playerMessage, botReply)` pushes a
pair onto `g_BotConversationHistory[botGuid][playerGuid]` under
`g_ConversationHistoryMutex`, trimming the deque to `g_MaxConversationHistory`.
`SaveBotConversationHistoryToDB()` (called on a timer / shutdown, declared in
the handler header) `INSERT IGNORE`s all in-RAM pairs into
`mod_ollama_chat_history` (escaping strings via
`CharacterDatabase.EscapeString`), then runs a `ROW_NUMBER() OVER (PARTITION BY
bot_guid, player_guid ORDER BY timestamp DESC)` delete keeping only the newest
`g_MaxConversationHistory` rows per pair (the comment notes MariaDB can't do
`WITH ... DELETE`, hence the materialized derived-table join).

---

## 4. Data structures & DB

**Enum / labels** (`mod-ollama-chat_handler.h` / `.cpp`):
- `enum ChatChannelSourceLocal { SRC_UNDEFINED_LOCAL=0, SRC_SAY_LOCAL=1,
  SRC_PARTY_LOCAL=2, SRC_RAID_LOCAL=3, SRC_GUILD_LOCAL=4, SRC_OFFICER_LOCAL=5,
  SRC_YELL_LOCAL=6, SRC_WHISPER_LOCAL=7, SRC_GENERAL_LOCAL=17 }`
- `const char* ChatChannelSourceLocalStr[]` — debug labels, 0..17.

**In-RAM globals** (`mod-ollama-chat_config.cpp`, declared in `_config.h`):
- `g_BotConversationHistory` :
  `unordered_map<uint64_t botGuid, unordered_map<uint64_t playerGuid,
  deque<pair<string playerMsg, string botReply>>>>`, guarded by
  `g_ConversationHistoryMutex`. Keys are **raw 64-bit GUIDs**
  (`GetGUID().GetRawValue()`).
- `g_BotPersonalityList` : `unordered_map<uint64_t guid, string key>` — loaded
  at startup from `mod_ollama_chat_personality`.
- `g_PersonalityPrompts` / `g_PersonalityTemplates` (`BotPersonalityTemplate`:
  `prompt, manualOnly, weight, replyChanceMultiplier, numPredictOverride,
  temperatureOverride, gearGiveChance`), `g_PersonalityKeys`,
  `g_PersonalityKeysRandomOnly`, `g_PersonalityRandomTotalWeight`.
- `g_BotPlayerSentiments` + `g_SentimentMutex` (used by the sentiment doc).
- `struct OllamaQueryOptions { uint32_t numPredictOverride; float
  temperatureOverride; }` (`mod-ollama-chat_querymanager.h`) — carried into the
  worker.
- Gear-give in-file statics (private to the handler): `g_gearGiveMutex`,
  `g_lastGiveByBot`, `g_lastGiveByPair`, `g_gearCtxMutex`,
  `g_lastGearCtxByPair` (see BOT-ECONOMY Section 1).

**Database (all in `acore_characters`, guid columns are *counters* except the
in-RAM history which uses raw GUIDs):**
- `mod_ollama_chat_history` (`bot_guid, player_guid, timestamp, player_message,
  bot_reply`) — read at startup by `LoadBotConversationHistoryFromDB()`, written
  by `SaveBotConversationHistoryToDB()`.
- `mod_ollama_chat_personality` (`guid, personality`) — read by
  `LoadBotPersonalityList()`, guarded by an `information_schema.tables` probe.
- `mod_ollama_chat_personality_templates` (`key, prompt, manual_only`, plus
  behavior columns `weight, reply_chance_multiplier, num_predict_override,
  temperature_override` and `gear_give_chance`) — read by
  `LoadPersonalityTemplatesFromDB()` with `information_schema.COLUMNS` probes
  (see Section 7).
- `mod_ollama_chat_pending_gives` (`bot_guid, player_guid, item_guid, cod`) —
  `REPLACE INTO` by `GenerateGearContext` (economy doc).

---

## 5. Concurrency & threading

**World thread** (never blocks on Ollama): the entire `OnPlayerCanUseChat` →
`ProcessChat` selection logic, `IsBotEligibleForChatChannelLocal`,
`EvaluateGroupRequest` (incl. sending the real invite packet),
`GenerateBotPrompt`, and `GenerateGearContext` (incl. `MailBagItemTo` DB
transaction and item movement). All live-object reads and any packet/inventory/
mail writes stay here, where object lifetimes are stable within the tick.

**Detached worker threads**: one per responding bot (`std::thread(...).detach()`)
and one per `OllamaChat_SpeakSituation`. Only the worker calls the (potentially
slow) `SubmitQuery`/`future.get()`. Safety rules the code follows:

- **No raw pointers cross the boundary.** The lambda captures `botGuid` /
  `senderGuid` (`ObjectGuid::GetRawValue()`) and re-resolves via
  `ObjectAccessor::FindPlayer(ObjectGuid(guid))` after each blocking point.
  Every reacquire is null-checked and bails on logout/despawn — this is the
  "null reacquire-by-GUID" degradation path (matches the CLAUDE.md
  long-lived-reference rule).
- **Reacquire after every sleep.** Typing-sim and who-me staggers each
  re-`FindPlayer` bot, AI, and sender, because the player can vanish during the
  delay.
- **`SubmitQuery` bounds concurrency.** `g_queryManager.setMaxConcurrentQueries(
  g_MaxConcurrentQueries)` (0 = unbounded) caps how many workers hit Ollama at
  once; excess queries queue in the manager, so a burst of chat can't spawn
  unbounded live HTTP requests.
- **Mutexes**: `g_ConversationHistoryMutex` guards the RAM history in
  `AppendBotConversation`/`GetBotHistoryPrompt`/save/load; `g_SentimentMutex`
  (sentiment doc); `g_gearGiveMutex`/`g_gearCtxMutex` guard the gear cooldown
  maps. The channel session log and its per-bot cooldowns are internally
  synchronized in `mod-ollama-chat_channels.cpp`.
- **Delivery from the worker**: `botAI->Say/Yell/SayToGuild/...` and
  `targetChannel->Say` are invoked from the worker after reacquiring — the
  playerbots AI say-methods are the module's chosen thread-safe egress; the
  worker never touches raw inventory/mail.
- **`ProcessBotChatMessage` re-entrancy**: the worker calls it after delivery,
  which calls `ProcessChat` again with `senderIsBot == true`. This is a
  potentially deep re-entry, throttled by the (low) `g_BotReplyChance_*` bot
  chances and per-source group validation; there is no explicit recursion
  depth cap, so keep bot reply chances small.

---

## 6. Config keys

All read in `LoadOllamaChatConfig()` (`mod-ollama-chat_config.cpp`) via
`sConfigMgr->GetOption<T>("Key", default)` unless noted. Only keys touching the
inbound pipeline are listed (event/random-chatter/guild-ambient keys are in
other docs).

**Master / gating**
| key | default |
|---|---|
| `OllamaChat.Enable` | `true` |
| `OllamaChat.DisableRepliesInCombat` | `true` |
| `OllamaChat.EnableWhisperReplies` | `false` |
| `OllamaChat.DisableForCustomChannels` | `false` |
| `OllamaChat.DisableForSayYell` | `false` |
| `OllamaChat.DisableForGuild` | `false` |
| `OllamaChat.DisableForParty` | `false` |
| `OllamaChat.BlacklistCommands` | `""` (comma-separated, appended to `.playerbots`,`playerbot`) |
| `OllamaChat.DebugEnabled` | `false` |
| `OllamaChat.DebugShowFullPrompt` | `false` |

**Distance / selection**
| key | default |
|---|---|
| `OllamaChat.SayDistance` | `30.0` |
| `OllamaChat.YellDistance` | `100.0` |
| `OllamaChat.MaxBotsToPick` | `2` |

**Reply chances (percent)**
| key | default |
|---|---|
| `OllamaChat.PlayerReplyChance.Say` | `90` |
| `OllamaChat.BotReplyChance.Say` | `10` |
| `OllamaChat.PlayerReplyChance.Channel` | `50` |
| `OllamaChat.BotReplyChance.Channel` | `5` |
| `OllamaChat.PlayerReplyChance.Party` | `90` |
| `OllamaChat.BotReplyChance.Party` | `10` |
| `OllamaChat.PlayerReplyChance.Guild` | `70` |
| `OllamaChat.BotReplyChance.Guild` | `5` |

**Prompt templates & context blocks**
| key | default |
|---|---|
| `OllamaChat.ChatPromptTemplate` | `""` (empty ⇒ prompt aborts) |
| `OllamaChat.ChatExtraInfoTemplate` | `""` |
| `OllamaChat.DefaultPersonalityPrompt` | `""` |
| `OllamaChat.EnableChatHistory` | `true` |
| `OllamaChat.ChatHistoryHeaderTemplate` | `""` |
| `OllamaChat.ChatHistoryLineTemplate` | `""` |
| `OllamaChat.ChatHistoryFooterTemplate` | `""` |
| `OllamaChat.MaxConversationHistory` | `5` |
| `OllamaChat.ConversationHistorySaveInterval` | `10` |
| `OllamaChat.EnableChatBotSnapshotTemplate` | `false` |
| `OllamaChat.ChatBotSnapshotTemplate` | `""` |

**RAG (optional, feeds `{`-appended block)**
| key | default |
|---|---|
| `OllamaChat.EnableRAG` | `false` |
| `OllamaChat.RAGMaxRetrievedItems` | `3` |
| `OllamaChat.RAGSimilarityThreshold` | `0.3` |
| `OllamaChat.RAGPromptTemplate` | `"RELEVANT INFORMATION:\n{rag_info}\n..."` |

**Typing simulation (worker-side delay)**
| key | default |
|---|---|
| `OllamaChat.EnableTypingSimulation` | `false` |
| `OllamaChat.TypingSimulationBaseDelay` | `1000` (ms) |
| `OllamaChat.TypingSimulationDelayPerChar` | `250` (ms) |

**Query manager / gear-context throttle (read where used)**
| key | default | read in |
|---|---|---|
| `OllamaChat.MaxConcurrentQueries` | `0` (unbounded) | `LoadOllamaChatConfig` → `g_queryManager.setMaxConcurrentQueries` |
| `OllamaChat.RequestTimeout` | `120` (s) | `LoadOllamaChatConfig` |
| `OllamaChat.GearContextCooldownMin` | `10` | `GearCommentaryAllowed()` (read inline, not cached) |
| `OllamaChat.GearGiveBotCooldownMin` | `30` | `LoadOllamaChatConfig` |
| `OllamaChat.GearGivePairCooldownMin` | `1440` | `LoadOllamaChatConfig` |

Personality reply-multiplier, num_predict/temperature overrides, and per-channel
gating chances come from the `mod_ollama_chat_personality_templates` table, not
`.conf` (see BOT-BEHAVIOR Section 2 and the channels/personality docs).

---

## 7. Failure modes & gotchas

- **Graceful schema degradation.** `LoadBotPersonalityList()` probes
  `information_schema.tables` for `mod_ollama_chat_personality` and logs "Please
  source the required database table first" instead of crashing if it is
  absent. `LoadPersonalityTemplatesFromDB()` probes `information_schema.COLUMNS`
  for `weight` and `gear_give_chance` separately, and selects the widest column
  set the DB actually has — a database missing the
  `2026_07_02_personality_behavior_columns.sql` / `2026_07_03_personality_gear_give.sql`
  migrations still loads with struct defaults (`weight=100`,
  `replyChanceMultiplier=1.0`, `gearGiveChance=2.0`) and a `LOG_WARN`.
- **Weak cross-module symbol.** `OllamaChat_SpeakSituation` is defined here with
  plain external linkage and re-declared `[[gnu::weak]]` in mod-playerbots;
  building without this module resolves the weak reference to null and the
  callers no-op (BOT-ECONOMY Section 4). Linux/ELF only.
- **Null reacquire-by-GUID.** Every worker step after a blocking call
  re-resolves `Player*` from the captured GUID and bails on null. A bot or the
  sender logging out mid-query simply drops the reply — no dangling pointer.
- **Empty response = silent drop.** An Ollama/API error yields an empty
  `response`; the worker returns without speaking (logged only under debug as
  "skipped reply due to API error"). But note (economy doc) a *gear give* mailed
  in `GenerateGearContext` already happened on the world thread — so a
  subsequent LLM failure means the item was sent with no accompanying chat line.
- **Empty `g_ChatPromptTemplate` aborts the prompt.** `GenerateBotPrompt`
  returns `""`, the worker gets an empty prompt; a misconfigured `.conf`
  silences all replies. `SafeFormat` returns `"[Format Error]"` for a malformed
  (but non-empty) template rather than throwing.
- **Whispers and name-mentions bypass the reply-chance roll.** A whispered bot
  always answers (combat-gate aside); a named bot is chosen deterministically as
  the first mention and skips the `urand` roll, the personality multiplier, and
  the General-channel gate — intended, but surprising when debugging "why did
  this bot answer at 5% chance."
- **`MaxBotsToPick` picks a *random* count.** The cap uses `urand(1,
  g_MaxBotsToPick)`, so with the cap at 2 you get 1 or 2 responders, not always
  2. Shuffle uses a fresh `std::mt19937`/`std::random_device` (not the project
  `urand`) — deliberate, but not seeded from the shared RNG.
- **Redundant / dead checks.** In the channel-gathering branch,
  `candidate->IsInChannel(channel)` is tested twice, and a `if (!channel)` guard
  sits inside the `else if (channel != nullptr)` branch (unreachable). Harmless,
  but don't mistake them for load-bearing logic.
- **`OnPlayerChat` is not hooked.** Only the `CanUseChat` hooks are registered;
  the "return false to prevent double processing" comment describes an intent
  that the registration (not the return value) enforces.
- **Bot-to-bot cascades.** `ProcessBotChatMessage` re-enters `ProcessChat` with
  `senderIsBot=true`; only the low `g_BotReplyChance_*` values and per-source
  real-player requirements prevent runaway loops. There is no depth counter.
- **Cooldowns are process-memory.** Gear-give and gear-commentary cooldowns use
  `getMSTime()` (server uptime) and reset on worldserver restart. In-RAM
  conversation history is only durable across the `SaveBotConversationHistoryToDB`
  timer / shutdown.

---

## 8. Cross-references

- [BOT-BEHAVIOR.md](../BOT-BEHAVIOR.md) — behavior-level framing: personalities
  Section 2, playstyles Section 3, sentiment Section 4, gear-inspect Section 5, quest-help Section 6, group-join
  Section 10, channel conversation system Section 11.
- [BOT-ECONOMY.md](../BOT-ECONOMY.md) — gear gives / mail / pending trades Section 1–2,
  `OllamaChat_SpeakSituation` Section 4, static-link/weak-symbol build model Section 5.
- Related module files (each its own subsystem): `mod-ollama-chat_channels.cpp`
  (`RecordChannelLine`, `BuildChannelHistory`, `ShouldBotReplyInChannel`,
  `MarkChannelReply`), `mod-ollama-chat_groupjoin.cpp` (`EvaluateGroupRequest`,
  `GroupJoinOutcome`), `mod-ollama-chat_sentiment.cpp`
  (`GetSentimentPromptAddition`, `UpdateBotPlayerSentiment`),
  `mod-ollama-chat_memory.cpp` (`BuildMemoryInfo`),
  `mod-ollama-chat_personality.cpp` (`GetBotPersonality`,
  `GetPersonalityPromptAddition`, `GetPersonalityReplyChanceMultiplier`,
  `GetPersonalityQueryOptions`), `mod-ollama-chat_querymanager.cpp` /
  `mod-ollama-chat_api.cpp` (`SubmitQuery`, `OllamaQueryOptions`,
  `g_queryManager`), `mod-ollama-chat_rag.cpp` (`OllamaRAGSystem`).
