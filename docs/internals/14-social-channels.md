# 14 — Group-join, who-me disambiguation & channel system (developer internals)

Deep, function-by-function reference for the social subsystem of
`mod-ollama-chat`. Behavior-level framing lives in
[BOT-BEHAVIOR](../BOT-BEHAVIOR.md) Section 10–12 and
[BOT-ECONOMY](../BOT-ECONOMY.md) Section 4; this doc explains how the code actually
works for someone about to modify or debug it. Terminology is kept consistent
with those docs (personalities, playstyles, sentiment, proximity chat,
"session memory", "announcer").

**Source of record**

| file | owns |
|---|---|
| `modules/mod-ollama-chat/src/mod-ollama-chat_groupjoin.cpp` (+ `.h`) | group-request detection, join chance, who-me pending map, real invite packet, `EvaluateGroupRequest` |
| `modules/mod-ollama-chat/src/mod-ollama-chat_channels.cpp` (+ `.h`) | RAM channel session log, channel reply gating, per-bot channel cooldown, periodic announcer |
| `modules/mod-ollama-chat/src/mod-ollama-chat_events_mail.cpp` | quest-completion reward mail |
| `modules/mod-ollama-chat/src/mod-ollama-chat_handler.cpp` | `ProcessChat` orchestration, proximity gate, channel-log hookup, `OllamaChat_SpeakSituation` |

---

## 1. Purpose

When a real player near a bot asks to group ("inv me", "need a healer"), the
group-join layer detects the intent, rolls a personality/playstyle/sentiment
chance, and — on success — sends a **real** `CMSG_GROUP_INVITE` packet through
the bot's own session, while telling the LLM what it just did so the spoken
reply matches the action. The channel layer turns public channels
(General/Trade) into throttled, relevance-gated, personality-driven
conversation spaces with a RAM session log and a periodic announcer that posts
level-appropriate LFG / real-item WTS / real-guild recruitment lines. The
event-mail layer mails a small reward after a real player finishes a quest
alongside random bots. All spoken output is produced asynchronously off the
world thread via the LLM query queue.

---

## 2. Entry points & call graph

Three independent entry points, all funneling their spoken output through the
same async `SubmitQuery` path.

**A. Player chats (the main path).** Hook → `ProcessChat` → per-bot
group-join + channel gating → detached worker thread that speaks.

```
PlayerScript hook OnPlayerCanUseChat(... )            [handler.cpp:184-260, world thread]
  └─ PlayerBotChatHandler::ProcessChat(...)           [handler.cpp:827]
       ├─ RecordChannelLine(channelId, speaker, msg)  [if SRC_GENERAL_LOCAL]  → channels.cpp
       ├─ build eligibleBots / candidateBots (zone/faction/channel/distance filters)
       ├─ chance = g_*ReplyChance_<source>
       ├─ per candidate bot:
       │    ├─ effChance = chance * GetPersonalityReplyChanceMultiplier(...)
       │    ├─ if SRC_GENERAL_LOCAL: ShouldBotReplyInChannel(bot, msg, effChance)  → channels.cpp
       │    └─ roll urand(0,99) < effChance  → finalCandidates
       └─ per final bot:
            ├─ proximityChat = (SAY|YELL|WHISPER) && bot->IsInMap(player) && dist <= g_SayDistance
            ├─ if (!senderIsBot && proximityChat):
            │     groupJoinOutcome = EvaluateGroupRequest(bot, player, msg, finalCandidates.size(), groupJoinContext)  → groupjoin.cpp
            │        ├─ SendGroupInvite(bot, player)  → bot->GetSession()->HandleGroupInviteOpcode(p)   [Invited]
            │        ├─ RegisterWhoMe(bot, player)                                                       [AskWhoMe]
            │        └─ ClearWhoMe / returns Declined / NotAGroupRequest
            ├─ if SRC_GENERAL_LOCAL: promptContext += BuildChannelHistory(channelId)  → channels.cpp
            ├─ prompt = GenerateBotPrompt(bot, msg, player, promptContext, isWhoMe)
            └─ std::thread([...]{                          ← DETACHED WORKER THREAD
                   response = SubmitQuery(prompt, opts).get()
                   reacquire botPtr/senderPtr by GUID
                   if isWhoMe: sleep urand(800,5000) ms   ← staggered who-me
                   route: Channel::Say | Say/Yell/Party/Guild/Whisper
                   MarkChannelReply(botPtr)  [channel case]  → channels.cpp
                   ProcessBotChatMessage(botPtr, response, ...)   ← re-enters ProcessChat
               }).detach()
```

**B. Periodic channel announcer (timer).** WorldScript update tick.

```
OllamaChatChannelAnnouncer::OnUpdate(diff)   [channels.cpp:473, world thread, WorldScript]
  └─ RunTick()  every ChannelAnnounceIntervalSec
       └─ per in-world random bot:
            ├─ OnCooldown(bot)?  skip
            ├─ intent = PickAnnounceIntent(bot, personality)   (LFG / Trade / Recruit / none)
            ├─ RealPlayerInBotGeneral(bot)?  else skip
            ├─ roll_chance_f(ChannelAnnounceChance)
            └─ std::thread([...]{                       ← DETACHED WORKER THREAD
                   response = SubmitQuery(BuildAnnouncePrompt(...)).get()
                   botAI->SayToChannel(response, ChatChannelId::GENERAL)
                   MarkChannelReply(botPtr)
                   ProcessBotChatMessage(botPtr, response, SRC_GENERAL_LOCAL, nullptr)
               }).detach()
```

**C. Quest completion reward mail (hook).**

```
OllamaChatCompletionMailScript::OnPlayerCompleteQuest(player, quest)  [events_mail.cpp:196, world thread]
  └─ per bot group member (random bot, same map+zone):
       ├─ CanEventMailNow(bot, player)?
       ├─ effectiveChance = g_PersonalityTemplates[personality].gearGiveChance * EventMailChanceMultiplier
       ├─ roll_chance_f(effectiveChance)
       ├─ DeliverReward(bot, player)  → MailItemReward | MailGoldReward
       ├─ RegisterEventMail(bot, player)
       └─ OllamaChat_SpeakSituation(bot, player, "...mailed them a thank-you reward", false)  → handler.cpp
```

`OllamaChat_SpeakSituation` (handler.cpp:2304) is the shared async persona-line
generator used by C (above) and by mod-playerbots callers (trade/quest-help).

---

## 3. Function-by-function

### 3.1 Group-join (`mod-ollama-chat_groupjoin.cpp`)

#### `bool LooksLikeGroupRequest(std::string const& msg)`  (line 67)
Keyword heuristic, two tiers.
1. Lowercases via local `ToLower`.
2. **Strong phrases** (`kStrongPhrases`): `"inv me"`, `"invite me"`, `"join me"`,
   `"carry me"`, `"help me"`, `"give me a hand"`, `"wanna group"`,
   `"want to group"`, `"can you help"`, `"could you help"`, `"group up"`,
   `"lets group"`, `"let's group"`, `"party up"`, `"lfg"`,
   `"looking for group"`, `"need a group"`, `"need a healer"`, `"need a tank"` —
   any substring hit returns `true` immediately.
3. **Weak keywords** (`kWeakKeywords`): `"invite"`, `"party"`, `"group"`,
   `"help"`. A weak hit only counts if paired with a first/second-person **cue**:
   the message contains `'?'`, or the whole word `"me"`, or the whole word
   `"us"`. So "the party was fun" is ignored; "invite the group?" passes.

Whole-word matching is `ContainsWholeWord(lowerMsg, word)` (line 41): a
substring find bounded by non-alphanumeric neighbors, so `"us"` does not match
`"focus"` and `"me"` does not match `"game"`. Output: bool. No side effects.

#### `float GroupJoinChanceFor(Player* bot, Player* player)`  (line 151)
Base chance from personality, else playstyle, else 40; plus a sentiment bonus.
1. `chance = 40.0f`.
2. `GetBotPersonality(bot)` → look up `kByPersonality` (static table): e.g.
   `LFG_SPAMMER 95`, `GUILD_RECRUITER 85`, `HEALER_MAIN`/`WOW_MOM`/`MENTOR` 75,
   `CHILL_DAD`/`HEROIC_LEADER` 70, `EGIRL` 65, `SCARED_NEWBIE` 55, down to
   `ELITE_ARENA_PVPER 5`, `PVP_TRASHTALKER 10`, `SILENT_TYPE`/`ONE_WORD_GRUNTER`
   15, `HARDCORE_RAIDLEAD` 15, `MIN_MAXER`/`GRUMPY_VETERAN` 20,
   `UNHINGED_TROLL` 25.
3. **Only if the personality is not in the table**, fall back to
   `GetBotPlaystyle(bot)` and `kByPlaystyle`: `socializer 65`, `quester 55`,
   `idler 40`, `explorer 40`, `grinder 25`, `pvper 20`.
4. If `player` non-null: `GetBotPlayerSentiment(bot->GetGUID().GetRawValue(),
   player->GetGUID().GetRawValue())`; when `sentiment >= 0.6f`, add
   `OllamaChat.GroupJoinSentimentBonus` (default 20).
5. `return std::clamp(chance, 5.0f, 95.0f)`.

Note the sentiment lookup uses **raw** GUID values, matching the sentiment
table convention (BOT-BEHAVIOR Section 4).

#### `std::string GetBotPlaystyle(Player* bot)`  (anonymous, line 112)
Resolves a bot's playstyle by joining its chat personality to the
personality-templates table — mirrors mod-playerbots' own `GetBotPlaystyle`.
- One-time `information_schema.COLUMNS` probe (static `hasPlaystyleColumn`)
  checking whether `mod_ollama_chat_personality_templates` has a `playstyle`
  column. If absent → return `""` (graceful degrade: `GroupJoinChanceFor` then
  keeps 40).
- Per-guid cache `g_playstyleCache` (keyed by `GetGUID().GetCounter()`), guarded
  by `g_playstyleCacheMutex`. A cached **non-empty** value is returned forever;
  a cached **empty** value is retried after
  `5 * MINUTE * IN_MILLISECONDS` (so late personality assignment is picked up).
- On miss: `SELECT t.playstyle FROM mod_ollama_chat_personality p JOIN
  mod_ollama_chat_personality_templates t ON t.`key` = p.personality WHERE
  p.guid = {}`. A result of `"default"` is normalized to `""`. Stores
  `{playstyle, now}` in the cache.

#### Who-me pending map — `RegisterWhoMe` / `HasPendingWhoMe` / `ClearWhoMe`  (lines 214–244)
State: `std::map<std::pair<ObjectGuid::LowType, ObjectGuid::LowType>, uint32>
g_pendingWhoMe` keyed by `(bot counter, player counter)` → `getMSTime()`, guarded
by `g_whoMeMutex`. `WhoMeKey(bot, player)` builds the pair from `GetCounter()`.
- `RegisterWhoMe` — stamps `now` for the pair (records "I just asked who-me").
- `HasPendingWhoMe` — returns false if no entry; if the entry is older than
  `kWhoMeTtlMs = 60 * IN_MILLISECONDS`, it **erases** the stale entry and
  returns false; else true. TTL is 60 s.
- `ClearWhoMe` — erases the pair.

#### `void SendGroupInvite(Player* bot, Player* player)`  (anonymous, line 253)
Builds the invite packet exactly like mod-playerbots'
`InviteToGroupAction::Invite`:
```cpp
WorldPacket p;
uint32 roles_mask = 0;
p << player->GetName();
p << roles_mask;
bot->GetSession()->HandleGroupInviteOpcode(p);
```
Guards `bot`/`player`/`bot->GetSession()`. This is the real
`CMSG_GROUP_INVITE` handler, so the player receives a genuine invite dialog.
Runs on the world thread only (see Section 5).

#### `MessageNamesBot` / `MessageHasAffirmative`  (anonymous, lines 264–277)
- `MessageNamesBot(bot, lowerMsg)` — substring find of the lowercased bot name
  in the (already-lowercased) message.
- `MessageHasAffirmative(lowerMsg)` — whole-word match of any of
  `"you"`, `"yes"`, `"yeah"`, `"yep"`, `"u"` (used to accept a bare "yeah you"
  confirmation of a pending who-me).

#### `GroupJoinOutcome EvaluateGroupRequest(Player* bot, Player* player, std::string const& msg, uint32_t candidateBotsInRange, std::string& outExtraContext)`  (line 283)
The decision routine, called once per candidate bot on the world thread.
Returns a `GroupJoinOutcome` enum (`NotAGroupRequest`, `AskWhoMe`, `Invited`,
`Declined`) and fills `outExtraContext` with a directive appended to the prompt.
1. Clears `outExtraContext`. If `OllamaChat.EnableGroupJoin` is false, or
   `bot`/`player` null → `NotAGroupRequest`.
2. `affirmedPending = HasPendingWhoMe(bot, player) && MessageHasAffirmative(...)`.
   If **not** `LooksLikeGroupRequest(msg)` **and** not `affirmedPending` →
   `NotAGroupRequest`. (A bare "yes, you" only continues an ask I recently made.)
3. If already grouped together (`botGroup->IsMember(player->GetGUID())`) →
   `NotAGroupRequest`.
4. If the bot's group is full (`botGroup->IsFull()`) → set a "let them down
   briefly" context, return `Declined`.
5. `named = MessageNamesBot(...)`;
   `resolved = named || candidateBotsInRange <= 1 || affirmedPending`.
   - **Resolved** → `ClearWhoMe`, roll `roll_chance_f(GroupJoinChanceFor(...))`:
     - success → `SendGroupInvite`, log `[GroupJoin] X invited Y (chance ..)`,
       context "You just sent them a group invite — tell them it's on the way",
       return `Invited`.
     - failure → log `[GroupJoin] X declined Y`, context "decline in your own
       words, true to your personality", return `Declined`.
   - **Unresolved** (2+ candidate bots, none named, no pending affirm) →
     `RegisterWhoMe`, log `[GroupJoin] X asks who-me to Y`, context "there are
     several of you nearby … ask briefly whether they mean you", return
     `AskWhoMe`.

The `candidateBotsInRange` argument is `finalCandidates.size()` from the
handler — the count of bots about to reply, i.e. `1` means unambiguous.

### 3.2 Channels (`mod-ollama-chat_channels.cpp`)

Config accessors `CfgEnabled` / `CfgReplyCooldownSec` / `CfgAnnounceIntervalSec`
/ `CfgAnnounceChance` (lines 54–69) read `sConfigMgr` directly with defaults
(see Section 6). String helpers `ToLower`, `ContainsWord` (whole-word, falls back to
substring for multi-word needles), `ContainsAny`.

#### Personality tables
- `float TalkChanceForKey(std::string const& personality)`  (line 115) —
  channel talkativeness 0..100. `kByPersonality`: `LFG_SPAMMER 90`,
  `GUILD_RECRUITER 85`, `TRADE_COMEDIAN`/`TRADER 70`, `GOBLIN_MERCHANT 65`,
  `GOLD_FARMER 60`, `DRAMA_QUEEN 50`, `UNHINGED_TROLL 45`,
  `CONSPIRACY_THEORIST 40`. **Everyone else returns `8.0f`** — most personas
  barely use General because they quest with you in person.
- `bool IsHighChatter(personality)` — `TalkChanceForKey(...) > 8.0f` (the
  "chatty set").
- `bool IsTrader(personality)` — set `{TRADER, TRADE_COMEDIAN, GOBLIN_MERCHANT,
  GOLD_FARMER}`.
- `bool IsRecruiter(personality)` — `== "GUILD_RECRUITER"`.
- `bool IsGuildOfficer(Player* bot)` — `bot->GetGuildId() != 0 &&
  bot->GetRank() <= GR_OFFICER` (guild leaders/officers legitimately recruit).

#### Relevance keyword sets
`kGroupKeywords` (lfg/group/dungeon/instance/quest/heroic/tank/healer/dps/run/
carry/boost/…), `kTradeKeywords` (wts/wtb/wtt/price/pricecheck/selling/buying/
auction/trade/mats/enchant), `kGuildKeywords` (guild/recruit/recruiting/
guildless). `MessageNamesBot(bot, lowerMsg)` uses whole-word `ContainsWord`.

#### Channel session log
```cpp
struct ChannelLine { std::string speaker; std::string message; };
constexpr size_t kMaxLoggedLines = 30;
std::mutex g_channelLogMutex;
std::unordered_map<uint32_t, std::deque<ChannelLine>> g_channelLog;   // channelId -> lines
```
- `void RecordChannelLine(uint32_t channelId, std::string const& speaker, std::string const& msg)`
  (line 352) — ignores empty channelId/speaker/msg; pushes `{speaker,msg}` and
  pops the front until `<= 30`. RAM only, no DB.
- `std::string BuildChannelHistory(uint32_t channelId, uint32_t maxLines = 10)`
  (line 364) — returns `""` if unknown/empty; otherwise builds
  `"Recent channel conversation:\n<speaker>: <msg>\n..."` for the last
  `maxLines` lines, truncating each message to 200 chars to keep the prompt
  bounded. `maxLines` defaults to 10 at the call site.

#### Per-bot channel cooldown
```cpp
std::mutex g_cooldownMutex;
std::unordered_map<uint64_t, uint32_t> g_lastChannelReply;   // raw guid -> getMSTime()
```
- `bool OnCooldown(Player* bot)`  (line 187) — true if within
  `CfgReplyCooldownSec()` seconds of the last reply. `cd == 0` disables the
  throttle (returns false). No-entry → not on cooldown.
- `void MarkChannelReply(Player* bot)`  (line 457) — stamps `getMSTime()` for
  the bot's **raw** GUID. Shared by replies (marked at the handler send site)
  and announcements (marked in the announcer thread).

#### Reply gating
- `float ChannelTalkChanceFor(Player* bot)`  (line 388) — thin wrapper over
  `TalkChanceForKey(GetBotPersonality(bot))`.
- `bool ChannelMessageRelevantTo(Player* bot, std::string const& msg)`  (line 395):
  1. named → true; 2. `ContainsAny(kGroupKeywords)` → true (relevant to any
  channel-active bot); 3. trader + `kTradeKeywords` → true; 4. recruiter/officer
  + `kGuildKeywords` → true; else false.
- `bool ShouldBotReplyInChannel(Player* bot, std::string const& msg, uint32_t& effChance)`
  (line 420) — the single gate the handler calls, scaling `effChance` in place:
  1. `!CfgEnabled()` → **return true, effChance untouched** (legacy behavior).
  2. `OnCooldown(bot)` → false.
  3. `!ChannelMessageRelevantTo`: if `!IsHighChatter` → false; else set
     `effChance = 10` (small flat banter chance) and return true.
  4. Relevant: `effChance = min(100, round(effChance * TalkChanceForKey/100))`,
     return true. So a General reply chance is scaled down hard for the 8-value
     majority (8%) and kept high for the channel-native set.

#### Announcer helpers
- `struct DungeonBracket { uint32 lo; uint32 hi; char const* name; }` and the
  `kDungeons[]` table (Deadmines 13–18 … Halls of Lightning 77–80), matching
  `finetune/generate_dataset.py`.
- `std::string PickDungeonForLevel(uint32 level)`  (line 212) — collects
  brackets where `d.lo <= level+3 && d.hi+3 >= level`, returns a random match
  or `""`.
- `std::string PickSellableItemName(Player* bot)`  (line 225) — bag scan for the
  highest-item-level spare that is not soulbound, quality UNCOMMON..EPIC, class
  ARMOR/WEAPON/TRADE_GOODS, not a quest item, not conjured. Returns `Name1` or
  `""`. Mirrors `FindSpareItem` (events_mail) and `GenerateGearContext` (handler).
- `struct AnnounceIntent { char const* label; std::string text; }` — `label`
  nullptr means "do not announce".
- `AnnounceIntent PickAnnounceIntent(Player* bot, std::string const& personality)`
  (line 267) — order matters, personality-specific wins over guild-officer:
  - `LFG_SPAMMER` → LFG line for `PickDungeonForLevel(bot->GetLevel())` (empty
    dungeon → no intent).
  - `IsTrader` → WTS/price-check for `PickSellableItemName(bot)` (empty → none).
  - `IsRecruiter || IsGuildOfficer` → recruitment line naming `GetGuild()->
    GetName()` (no guild → none).
  - else → empty intent (drama/troll/conspiracy still **reply** on relevance,
    they just don't announce).
- `bool RealPlayerInBotGeneral(Player* bot)`  (line 313) — true if any non-bot
  player is in-world with the bot's **team and zone** (don't announce to an
  empty local General).
- `std::string BuildAnnouncePrompt(Player* bot, personality, AnnounceIntent)`
  (line 329) — persona + intent prompt, independent of the chat template; asks
  for "ONE short in-character public-channel line (max ~20 words), plain text".

#### `class OllamaChatChannelAnnouncer : public WorldScript`  (line 468)
- `void OnUpdate(uint32 diff)` — bails on `!g_Enable || !CfgEnabled()`;
  computes `intervalMs = CfgAnnounceIntervalSec() * IN_MILLISECONDS` (0
  disables); counts down `m_timer` and calls `RunTick()` when it elapses,
  resetting `m_timer = intervalMs`.
- `void RunTick()` — `chance = CfgAnnounceChance()` (≤0 → return). For each
  in-world random bot (`ai->IsBotAI()`): skip if `OnCooldown`; compute
  `PickAnnounceIntent`; skip if no label; skip if `!RealPlayerInBotGeneral`;
  `roll_chance_f(chance)`; then spawn a **detached** worker thread that
  `SubmitQuery(BuildAnnouncePrompt(...))`, reacquires the bot by GUID,
  `botAI->SayToChannel(response, ChatChannelId::GENERAL)`, `MarkChannelReply`,
  and re-routes via `ProcessBotChatMessage(..., SRC_GENERAL_LOCAL, nullptr)` so
  the announcement lands in the session log and can draw replies. All
  exceptions swallowed.
- `void AddSC_mod_ollama_chat_channels()`  (line 572) — registers the WorldScript.

### 3.3 Event mail (`mod-ollama-chat_events_mail.cpp`)

Anti-spam: `constexpr uint32 EVENT_MAIL_COOLDOWN_MIN = 60` (one mail per
(bot,player) pair per hour), `std::map<std::pair<uint64_t,uint64_t>, uint32>
g_lastEventMail` guarded by `g_eventMailMutex`, keyed by **raw** GUIDs.
- `bool CanEventMailNow(Player* bot, Player* player)` / `void RegisterEventMail(...)`
  (lines 57–71) — standard `getMSTime()` cooldown check/stamp.
- `Item* FindSpareItem(Player* bot)`  (line 80) — same bag scan as
  `PickSellableItemName`: highest-ilvl spare, non-soulbound, UNCOMMON..EPIC, not
  quest item, not conjured. Returns the `Item*` (not the name) or nullptr.
- `std::string MailItemReward(Player* bot, Player* player, Item* item)`  (line 108) —
  one `CharacterDatabaseTransaction`: `SetNotRefundable` →
  `MoveItemFromInventory` → `DeleteFromInventoryDB` → force `ITEM_CHANGED` →
  `SetOwnerGUID(player)` → `SaveToDB`; then a `MailDraft("A token of thanks",
  "Good adventuring with you out there. Keep this - you earned it. - <bot>")`
  with `AddItem` and `SendMailTo(..., MAIL_CHECK_MASK_COPIED)`;
  `SaveInventoryAndGoldToDB`; commit. Returns the item name (mirrors
  `MailBagItemTo` in the handler).
- `std::string MailGoldReward(Player* bot, Player* player, uint32 copper)`  (line 136) —
  guards `copper != 0 && bot->GetMoney() >= copper`; `ModifyMoney(-copper)`,
  `SaveGoldToDB`, `MailDraft("A little something", "...Here's a bit of coin.")`
  + `AddMoney(copper)` + `SendMailTo`; returns `"<n> silver"` (`max(copper/100,1)`).
- `std::string DeliverReward(Player* bot, Player* player)`  (line 157) — computes
  `gold = min(level * urand(50,150), 50000)` copper; prefers an item **~40%** of
  the time when one exists (or always if the bot can't afford gold):
  `if (spare && (!botCanPayGold || roll_chance_i(40)))` mail the item; else mail
  gold; last-ditch fallback mails the item if gold failed. Returns a short
  description or `""`.

#### `class OllamaChatCompletionMailScript : public PlayerScript`  (line 191)
`void OnPlayerCompleteQuest(Player* player, Quest const* quest)`:
1. Guards `g_Enable`, `player`, `quest`, `OllamaChat.EnableEventMail`.
2. `mailMultiplier = OllamaChat.EventMailChanceMultiplier` (default 0.5); `<= 0`
   → return.
3. **Reward real players only** — return if the completer has a `PlayerbotAI`.
4. Require a group. Iterate `group->GetMemberSlots()`, skipping the player.
5. For each member: reacquire `bot` by `slot.guid`; must have `PlayerbotAI`,
   must be `sRandomPlayerbotMgr.IsRandomBot(bot)` (not a human, not a
   hand-summoned alt-bot), must share `GetMapId()` **and** `GetZoneId()` with the
   player, must pass `CanEventMailNow`.
6. `baseChance = g_PersonalityTemplates[personality].gearGiveChance` (default
   2.0 if the key is missing); `effectiveChance = baseChance * mailMultiplier`;
   `roll_chance_f`.
7. `DeliverReward`; if empty (nothing to give) `continue` — lets another member
   try. On success: `RegisterEventMail`, log `[Ollama Chat] [EventMail] ...`,
   `OllamaChat_SpeakSituation(bot, player, "you just finished a quest together
   and mailed them a thank-you reward", false)`, then **`break`** — only one bot
   rewards per event.
- `void AddSC_mod_ollama_chat_events_mail()`  (line 269) — registration.

### 3.4 Handler integration (`mod-ollama-chat_handler.cpp`)

Only the pieces in this subsystem's focus; the rest of `ProcessChat`
(eligibility filtering, gear context, prompt assembly) is background.

#### Channel-log capture — `ProcessChat` line 882
```cpp
if (sourceLocal == SRC_GENERAL_LOCAL && channelId != 0)
    RecordChannelLine(channelId, player->GetName(), msg);
```
Fires near the top of `ProcessChat` for every General/Trade line — players
**and** bots, because bot replies re-enter here via `ProcessBotChatMessage`.
One hook captures both sides of channel conversation.

#### Channel reply roll — `ProcessChat` line 1363
Inside the non-mention candidate loop:
```cpp
if (sourceLocal == SRC_GENERAL_LOCAL && !ShouldBotReplyInChannel(bot, msg, effChance))
    continue;
uint32_t roll = urand(0, 99);
if (roll < effChance) finalCandidates.push_back(bot);
```
`effChance` starts as `chance * GetPersonalityReplyChanceMultiplier(...)` and is
then scaled in place by the channel gate. A named bot short-circuits this whole
block (mention path, lines 1328–1343) and always replies.

#### Proximity-chat-only group-join gate — `ProcessChat` lines 1448–1456
```cpp
bool proximityChat = (sourceLocal == SRC_SAY_LOCAL || sourceLocal == SRC_YELL_LOCAL ||
                      sourceLocal == SRC_WHISPER_LOCAL) &&
                     bot->IsInMap(player) && bot->GetDistance(player) <= g_SayDistance;
GroupJoinOutcome groupJoinOutcome = GroupJoinOutcome::NotAGroupRequest;
if (!senderIsBot && proximityChat)
    groupJoinOutcome = EvaluateGroupRequest(bot, player, msg, finalCandidates.size(), groupJoinContext);
bool isWhoMe = (groupJoinOutcome == GroupJoinOutcome::AskWhoMe);
```
This is the **fix for the distant-invite bug**: channel messages
(`SRC_GENERAL_LOCAL`) reach bots anywhere in the world, so acting on them
produced invites from bots the player couldn't see. Group-join now requires
say/yell/whisper **and** an in-map, within-`g_SayDistance` check. The `isWhoMe`
flag becomes `suppressGearContext` for `GenerateBotPrompt`, so an ambiguous bot
just asks "who, me?" without gear commentary.

#### Channel history into the prompt — `ProcessChat` lines 1461–1466
```cpp
if (sourceLocal == SRC_GENERAL_LOCAL && channel) {
    std::string channelLog = BuildChannelHistory(channel->GetChannelId());
    if (!channelLog.empty())
        promptContext += (promptContext.empty() ? "" : "\n") + channelLog;
}
```
`promptContext` starts as `groupJoinContext`; the channel history is appended
after any group-join directive and passed as `extraContext` to
`GenerateBotPrompt`, which appends it raw as the final instruction (handler
line 2283).

#### Worker-thread routing, who-me stagger, channel cooldown — `ProcessChat` lines 1472–1748
Each final bot spawns a **detached** worker thread capturing raw GUIDs. Inside:
- `SubmitQuery(prompt, queryOpts).get()`; reacquire `botPtr`/`senderPtr` by
  GUID; bail if null or empty response.
- Optional typing-simulation sleep (`g_EnableTypingSimulation`), then reacquire.
- **Who-me stagger** (line 1540): `if (isWhoMe)
  std::this_thread::sleep_for(std::chrono::milliseconds(urand(800, 5000)))`, then
  reacquire — so several nearby bots don't answer "who, me?" in unison. The
  sleep is in this bot's own worker thread, so it delays only this reply.
- Routing: channel case → `targetChannel->Say(...)` + `MarkChannelReply(botPtr)`
  + `ProcessBotChatMessage(botPtr, response, SRC_GENERAL_LOCAL, targetChannel)`;
  otherwise a `switch (sourceLocal)` over guild/party/raid/say/yell/whisper (say
  and yell re-check that someone is within `g_SayDistance`/`g_YellDistance`).
- Then `UpdateBotPlayerSentiment`, `AppendBotConversation`.

#### `void OllamaChat_SpeakSituation(Player* bot, Player* target, std::string const& situation, bool whisper)`  (line 2304)
The shared async persona-line generator. Fire-and-forget; never blocks the
caller. Used by the event-mail hook (C above) and, via a `[[gnu::weak]]`
re-declaration, by mod-playerbots' `TradeStatusAction`/`InviteToGroupAction`
(see BOT-ECONOMY Section 4).
1. Guards `g_Enable`, `bot`, live `PlayerbotAI`.
2. Builds a one-shot prompt: `"You're a Wrath-era WoW player. Name: <name>, a
   level <lvl> <class>. MAKE SURE YOU RESPOND USING YOUR PERSONALITY, WHICH IS:
   <key>: <prompt>. Situation: <situation> (speaking to <target>). Say one short
   in-character line about it, under 15 words. No narration, no quotes, just the
   line."`
3. `GetPersonalityQueryOptions(bot)` — per-personality `num_predict`/
   `temperature` overrides apply.
4. Captures **raw GUIDs, not pointers**, into a detached `std::thread`:
   `SubmitQuery` → `future.get()` → reacquire via `ObjectAccessor::FindPlayer` →
   `botPtr->Whisper(response, LANG_UNIVERSAL, targetPtr)` when `whisper` and the
   target is online, else `ai->Say(response)`. All exceptions swallowed.

`ProcessBotChatMessage(Player* bot, const std::string& msg, ChatChannelSourceLocal
sourceLocal, Channel* channel)` (handler line 323) is the re-entry used by the
routing code and the announcer: it validates the bot can send to the given
source (channel object present, real player in guild/party/general, etc.),
converts the source back to a `CHAT_MSG_*` type, and re-invokes `ProcessChat`
with the **bot** as sender — which is why channel bot replies get logged and can
draw further replies, and why group-join is gated to `!senderIsBot`.

---

## 4. Data structures & DB

**In-RAM state (no DB):**

| symbol | file | type / key | guarded by |
|---|---|---|---|
| `g_playstyleCache` | groupjoin | `unordered_map<ObjectGuid::LowType, pair<string,uint32>>` (guid counter → {playstyle, ms}) | `g_playstyleCacheMutex` |
| `g_pendingWhoMe` | groupjoin | `map<pair<LowType,LowType>, uint32>` ((bot,player) counters → ms), 60 s TTL | `g_whoMeMutex` |
| `g_channelLog` | channels | `unordered_map<uint32_t, deque<ChannelLine>>` (channelId → last 30) | `g_channelLogMutex` |
| `g_lastChannelReply` | channels | `unordered_map<uint64_t, uint32_t>` (raw guid → ms) | `g_cooldownMutex` |
| `g_lastEventMail` | events_mail | `map<pair<uint64_t,uint64_t>, uint32>` (raw guids → ms), 60 min TTL | `g_eventMailMutex` |
| `OllamaChatChannelAnnouncer::m_timer` | channels | `uint32` countdown | world thread only |

**Static lookup tables:** `kByPersonality`/`kByPlaystyle` (join chances),
`kStrongPhrases`/`kWeakKeywords` (group detection), `TalkChanceForKey`'s
`kByPersonality`, `kTraders`, `kGroup/Trade/GuildKeywords`, `kDungeons[]`.

**DB reads:**
- `information_schema.COLUMNS` — one-time probe in `GetBotPlaystyle` for the
  `playstyle` column (graceful degrade).
- `mod_ollama_chat_personality` (`guid`, `personality`) JOIN
  `mod_ollama_chat_personality_templates` (`key`, `playstyle`) — playstyle
  resolution in `GetBotPlaystyle`. The templates' `gearGiveChance` field is read
  from the in-memory `g_PersonalityTemplates` map (loaded at startup / `ollama
  reload`), not re-queried per event.

**DB writes (event-mail only; via the core mail path inside one
`CharacterDatabaseTransaction`):** `item_instance` (`SaveToDB` /
`DeleteFromInventoryDB`), `character_inventory` (`SaveInventoryAndGoldToDB`),
`characters` money (`SaveGoldToDB`), and `mail` / `mail_items` (`MailDraft::
SendMailTo`). The channel and who-me layers write **no** DB.

Sentiment reads go through `GetBotPlayerSentiment` on **raw** GUIDs
(`mod_ollama_chat_bot_player_sentiments`, guid counters — BOT-BEHAVIOR Section 4).

---

## 5. Concurrency & threading

**World thread (safe to touch `Player*`, packets, DB):**
`ProcessChat`'s synchronous body (eligibility, gating, `EvaluateGroupRequest`,
`SendGroupInvite`/`HandleGroupInviteOpcode`, `GenerateBotPrompt`,
`RecordChannelLine` at the top hook), `OllamaChatChannelAnnouncer::OnUpdate`/
`RunTick` (a `WorldScript` update), and `OnPlayerCompleteQuest` (a `PlayerScript`
hook, including the reward mail transaction). The real invite packet is only
built here, and only on the original player-sent call (`!senderIsBot`), so it
never races.

**Detached worker threads (must reacquire everything by GUID):**
the per-bot reply thread in `ProcessChat`, the announcer's per-bot thread, and
`OllamaChat_SpeakSituation`'s thread. Each captures **raw GUIDs**, calls
`SubmitQuery(...).get()` (blocking on the LLM off the world tick), then
reacquires `Player*` via `ObjectAccessor::FindPlayer(ObjectGuid(guid))` and
bails on null. The who-me stagger `sleep_for(urand(800,5000)ms)` and the
typing-simulation sleep both live in these worker threads, delaying only the
owning bot's own line.

**Re-entrancy caveat:** worker threads call `ProcessBotChatMessage`, which
re-invokes `ProcessChat` **on the worker thread** with the bot as sender. That
path iterates `ObjectAccessor::GetPlayers()` off the world thread — an existing
design property of the module. Group-join is unaffected because it is gated to
`!senderIsBot`, so the re-entrant bot-sender call never evaluates a request or
sends an invite.

**Mutexes** are all short-lived, cover only their own map, and are never held
across an LLM call or a `Player*` dereference: `g_playstyleCacheMutex`,
`g_whoMeMutex`, `g_channelLogMutex`, `g_cooldownMutex`, `g_eventMailMutex`.
`RecordChannelLine`/`BuildChannelHistory`/`MarkChannelReply`/`OnCooldown` are
therefore safe to call from either thread.

---

## 6. Config keys

All read with `sConfigMgr->GetOption<T>(key, default)`; none are required in the
`.conf.dist` files (defaults keep every feature on). Config loads at startup →
**restart worldserver after changes**.

| key | default | file | meaning |
|---|---|---|---|
| `OllamaChat.EnableGroupJoin` | `true` | groupjoin | master switch for `EvaluateGroupRequest` |
| `OllamaChat.GroupJoinSentimentBonus` | `20.0` (float) | groupjoin | added to join chance when pair sentiment ≥ 0.6 |
| `OllamaChat.EnableChannelOverhaul` | `true` | channels | master switch; off → legacy channel behavior (gate returns true, effChance untouched) |
| `OllamaChat.ChannelReplyCooldownSec` | `90` | channels | one channel line per bot per this many seconds (0 = no throttle); shared by replies + announcements |
| `OllamaChat.ChannelAnnounceIntervalSec` | `180` | channels | announcer tick period (0 = disabled) |
| `OllamaChat.ChannelAnnounceChance` | `2.0` (float) | channels | % per eligible bot per tick to post an announcement (≤0 = disabled) |
| `OllamaChat.EnableEventMail` | `true` | events_mail | master switch for quest-completion reward mail |
| `OllamaChat.EventMailChanceMultiplier` | `0.5` (float) | events_mail | scales `gearGiveChance` down for this passive event (≤0 = disabled) |

**Related globals loaded elsewhere** (from `mod-ollama-chat_config.cpp`, used by
the focus paths): `g_Enable` (module master switch), `g_SayDistance` /
`g_YellDistance` (proximity gate + say/yell audibility re-check),
`g_DisableForCustomChannels` / `g_DisableForSayYell` (early `ProcessChat`
skips), and the `g_*ReplyChance_*` reply-chance bases that seed `effChance`.

---

## 7. Failure modes & gotchas

- **`playstyle` column missing** — `GetBotPlaystyle`'s `information_schema`
  probe returns `""` once and caches it (`hasPlaystyleColumn`); join chance then
  stays at the 40 default for personalities not in `kByPersonality`. The whole
  playstyle join degrades gracefully to a pre-migration DB.
- **Empty-playstyle retry** — a cached **empty** playstyle is retried every 5
  min (`getMSTimeDiff < 5*MINUTE*IN_MILLISECONDS`), so a bot whose personality is
  assigned after first lookup still picks up its profile; a cached non-empty
  value is permanent for the session.
- **Who-me TTL is lazy** — `g_pendingWhoMe` entries are only expired when
  `HasPendingWhoMe` next reads them (60 s). A resolved ask is cleared eagerly by
  `ClearWhoMe`; an unanswered one lingers until the next read.
- **Null reacquire-by-GUID** — every worker thread re-fetches `botPtr`/
  `senderPtr` (and `targetPtr`) after each blocking step (query, typing sleep,
  who-me sleep) and returns silently if the player logged out or despawned. No
  stale `Player*` is ever dereferenced.
- **Cooldowns reset on restart** — `g_lastChannelReply`, `g_lastEventMail`,
  `g_playstyleCache`, `g_pendingWhoMe` are all in-memory `getMSTime()`-based; a
  worldserver restart clears them. Accepted (no table for 60–90 s / 60 min
  windows).
- **GUID-key inconsistency** — who-me and playstyle maps key on
  `GetGUID().GetCounter()` (low guid); the channel cooldown and event-mail maps
  key on `GetGUID().GetRawValue()` (raw 64-bit); sentiment lookups use raw. Match
  the exact accessor when adding lookups or you will silently miss entries.
- **Invite only fires face-to-face** — the proximity gate means a group request
  typed into General/Trade is never acted on (it fixed distant invites from
  unseen bots). Group-request keywords in a channel still feed the normal channel
  reply, just not an invite.
- **Mail sent, line may not appear** — event-mail (and gear-give) commit the
  transfer on the world thread **before** the async `OllamaChat_SpeakSituation`
  query; if the LLM errors, the player still gets the mail with no chat line.
  Same invariant as BOT-ECONOMY: the item moves first, the words follow.
- **`OllamaChat_SpeakSituation` weak-symbol coupling** — defined strong here,
  declared `[[gnu::weak]]` in mod-playerbots. In a build with the chat module
  disabled the symbol resolves to null and the playerbots call sites no-op (see
  BOT-ECONOMY Section 4). Within this module it is a plain `extern` call.
- **Announcer talks only to a populated local General** —
  `RealPlayerInBotGeneral` requires a same-team, same-zone human; a bot with no
  sellable item / no dungeon at its level / no guild yields a null-`label` intent
  and is skipped. `SayToChannel` failing (bot not actually in a General channel)
  aborts the thread quietly.
- **Reply de-dup** — `MarkChannelReply` is called both at the handler send site
  and in the announcer thread; a bot that just announced is on cooldown for its
  next reply and vice-versa, so the two paths can't double-post inside one window.

---

## 8. Cross-references

- [`../BOT-BEHAVIOR.md`](../BOT-BEHAVIOR.md) — Section 2 personalities, Section 3 playstyles,
  Section 4 sentiment, Section 10 group-join, Section 11 channel system, Section 12 guild recruitment
  (behavior-level framing this doc deepens).
- [`../BOT-ECONOMY.md`](../BOT-ECONOMY.md) — Section 4 `OllamaChat_SpeakSituation` and
  the `[[gnu::weak]]` cross-module mechanism; Section 1 the mail-transfer pattern that
  `MailItemReward`/`MailGoldReward` mirror.
- Headers: `mod-ollama-chat_groupjoin.h` (the `GroupJoinOutcome` enum + public
  surface), `mod-ollama-chat_channels.h` (channel public surface).
- Related source in the same module (not covered here): `GetBotPersonality` /
  `GetPersonalityPromptAddition` / `GetPersonalityQueryOptions` /
  `g_PersonalityTemplates` in `mod-ollama-chat_personality.cpp`;
  `GetBotPlayerSentiment` / `UpdateBotPlayerSentiment` in
  `mod-ollama-chat_sentiment.cpp`; `SubmitQuery` / `OllamaQueryOptions` in
  `mod-ollama-chat_api.cpp`; `GenerateGearContext` / `MailBagItemTo` /
  `ProcessChat` prompt assembly in `mod-ollama-chat_handler.cpp`.
