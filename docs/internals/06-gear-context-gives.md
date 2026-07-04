# 06 — Gear inspect context & real gear gives

Developer internals for the `{gear_context}` prompt block and the "real item
into a real player's hand" give path. Behaviour-level framing lives in
[`../BOT-BEHAVIOR.md`](../BOT-BEHAVIOR.md) Section 5 (gear-inspect chat context) and
[`../BOT-ECONOMY.md`](../BOT-ECONOMY.md) Section 1–2 (mail gives + in-person trade
hand-overs); this doc explains how the C++ actually works, function by
function, for someone who will modify or debug it.

All code below is in
`azerothcore-wotlk/modules/mod-ollama-chat/src/mod-ollama-chat_handler.cpp`
unless noted. Line numbers are as of this writing and are anchors, not
contracts.

---

## 1. Purpose

When a bot is about to reply to a player, it "inspects" that player's armor
and the module injects a parenthetical `{gear_context}` line into the LLM
prompt describing either the player's weakest armor slot (upgrade angle) or a
recognition line for well-geared players. If the bot happens to carry a
genuine tradeable upgrade and its personality rolls the `gear_give_chance`,
the item is **actually transferred** — mailed (guild/group) or parked for an
in-person trade (stranger) — and only then does the prompt tell the bot to
mention it. The hard invariant: **the prompt never references an item that was
not actually sent or parked.**

---

## 2. Entry points & call graph

There is one live entry (the chat hook) and one completion entry (the trade
hook in mod-playerbots, for parked gives only).

**Generation (produces the `{gear_context}` string, may transfer the item):**

```
PlayerScript PlayerBotChatHandler::OnPlayerCanUseChat(...)   [world thread]
  → PlayerBotChatHandler::ProcessChat(player, ..., channel, receiver)   (l.827)
      → (per candidate bot in finalCandidates)                          (l.1428)
          → EvaluateGroupRequest → isWhoMe = (outcome == AskWhoMe)      (l.1454)
          → GenerateBotPrompt(bot, msg, player, promptContext, isWhoMe) (l.1468)
              → SafeFormat(g_ChatPromptTemplate, ...,
                   fmt::arg("gear_context",
                     suppressGearContext ? "" : GenerateGearContext(bot, player)))  (l.2267)
                  → GearCommentaryAllowed(bot, player)      throttle     (l.1950)
                  → MaxArmorSubclass / ClassPrimaryStat / IsGearSeller
                  → consider(...) bag scan → offer
                  → CanGiveGearNow(bot, player) + roll_chance_f          (l.1922/2093)
                  → mailEligible ? MailBagItemTo(...) : REPLACE INTO
                                                 mod_ollama_chat_pending_gives
                  → RegisterGearGive(bot, player)                        (l.1935)
          → std::thread([...]{ SubmitQuery(prompt) ... }).detach()       (l.1472)
```

`GenerateGearContext` runs **synchronously inside the hook**, before the
detached query thread is spawned — the LLM call is the only thing that goes
off-thread here. (Caveat: the bot-to-bot fan-out re-enters `ProcessChat` from
that detached thread — see Section 5.)

**Completion of a *parked* give (strangers only)** — mod-playerbots, when the
player opens a trade window with the bot
(`modules/mod-playerbots/src/Ai/Base/Actions/TradeStatusAction.cpp`):

```
TradeStatusAction::Execute(event)                                        (l.63)
  → PendingGiveItemGuid(bot, trader, &cod)   SELECT ... pending_gives    (l.35)
  → HandlePendingGearGive(event, trader, itemGuid, cod)   state machine  (l.430)
      → SSetItem(TradeSlots(0), item) / HandleAcceptTradeOpcode
      → on TRADE_STATUS_TRADE_COMPLETE → ClearPendingGive(bot, trader)   (l.57)
      → OllamaChat_SpeakSituation(...)   (weak-symbol LLM whisper)       (l.2304 in handler)
```

The **mail** give needs no completion hook — the item is already in the
core's mail system when `GenerateGearContext` returns.

---

## 3. Function-by-function

### `ClassPrimaryStat` — l.1878
```cpp
static char const* ClassPrimaryStat(uint8 playerClass)
```
WotLK-era stat shorthand for the prompt. Warrior/Paladin/DK → `"strength"`;
Hunter/Rogue → `"agility"`; Druid/Shaman → `"agility or spellpower"`;
everything else (default) → `"intellect and spellpower"`. Pure, returns a
static string literal. Used only to fill the `As a class they want {}` clause.

### `MaxArmorSubclass` — l.1894
```cpp
static uint32 MaxArmorSubclass(uint8 playerClass, uint8 level)
```
Highest `ITEM_SUBCLASS_ARMOR_*` value the class can wear **at this level**
(plate/mail training thresholds baked in):
- Warrior/Paladin/DK → `level >= 40 ? PLATE : MAIL`
- Hunter/Shaman → `level >= 40 ? MAIL : LEATHER`
- Rogue/Druid → `LEATHER`
- default (Mage/Priest/Warlock) → `CLOTH`

Feeds the `consider` lambda's upper bound so a bot never offers plate to a
rogue. Note the subclass enum is ordered CLOTH < LEATHER < MAIL < PLATE, so
`proto->SubClass > maxSub` is a valid "too heavy" test.

### `IsGearSeller` — l.1910
```cpp
static bool IsGearSeller(std::string const& personality)
```
Hardcoded `static std::set<std::string>` membership test:
`{"TRADER", "GOBLIN_MERCHANT", "GOLD_FARMER", "LOOTGOBLIN", "BANK_ALT"}`.
Sellers send the item COD; everyone else gifts it (cod = 0). This is the only
place the seller set is defined — it is **not** the same list as the
`gear_give_chance` tuning in the SQL, so a personality can be generous and a
gifter, generous and a seller, etc.

### `CanGiveGearNow` / `RegisterGearGive` — l.1922 / l.1935
```cpp
static bool CanGiveGearNow(Player* bot, Player* player)
static void RegisterGearGive(Player* bot, Player* player)
```
Two-level give cooldown, guarded by `g_gearGiveMutex`, keyed on
**raw GUID values** (`GetGUID().GetRawValue()`):
- `g_lastGiveByBot[botGuid]` — a bot gives at most once per
  `g_GearGiveBotCooldownMin` minutes (default 30). Stops a bot emptying its
  bags into the mailbox.
- `g_lastGiveByPair[{botGuid, playerGuid}]` — the same bot→player pair gets a
  give at most once per `g_GearGivePairCooldownMin` minutes (default 1440 =
  24 h).

`CanGiveGearNow` **checks only** (no side effect). `RegisterGearGive` **stamps
both maps** with `getMSTime()` and is called only after a give actually
succeeds (mail sent or row parked). Timestamps are `getMSTime()` (uint32 ms
since startup); comparisons are `now - stored < window` in unsigned
arithmetic. `MINUTE * IN_MILLISECONDS` converts minutes → ms.

### `GearCommentaryAllowed` — l.1950 (the "recent fix")
```cpp
static bool GearCommentaryAllowed(Player* bot, Player* player)
```
Per-pair throttle for gear **commentary** (not gives), guarded by
`g_gearCtxMutex`, keyed on `{botGuid, playerGuid}` raw values into
`g_lastGearCtxByPair`. Reads `OllamaChat.GearContextCooldownMin` (default 10)
each call. This is the fix that stopped bots turning every reply into a gear
review ("wanna group?" → "nice belt"): commentary only enters the prompt once
per pair per cooldown.

**Check-and-set**: on a cache miss (or expired entry) it stamps `now` and
returns `true`; within the window it returns `false` without updating. So the
first commentary for a pair consumes the cooldown even if the LLM ignores it.
A **real give bypasses this entirely** — see the short-circuit in
`GenerateGearContext` below.

### `MailBagItemTo` — l.1965
```cpp
static bool MailBagItemTo(Player* bot, Player* player, Item* item, uint32 codCopper)
```
Transfers `item` from the bot's inventory into a mail addressed to `player`,
mirroring the core `HandleSendMail` item path inside a single
`CharacterDatabaseTransaction`:
1. `item->SetNotRefundable(bot)` then `bot->MoveItemFromInventory(bag, slot, true)` — pull it out of the bot's bags.
2. `item->DeleteFromInventoryDB(trans)`, force `ITEM_CHANGED` if unchanged, `item->SetOwnerGUID(player->GetGUID())`, `item->SaveToDB(trans)`.
3. Build `MailDraft(item->GetTemplate()->Name1, codCopper ? "Pay the man." : "From one adventurer to another.")`, `AddItem(item)`, `AddCOD(codCopper)`, `SendMailTo(trans, MailReceiver(player, ...), MailSender(bot), MAIL_CHECK_MASK_COPIED)`.
4. `bot->SaveInventoryAndGoldToDB(trans)`, `CommitTransaction(trans)`.

**Always returns `true`** — there is no failure branch, so in the mail path
`gaveSomething` is always set once this is reached. Side effects: mutates the
bot's live inventory and writes `item_instance`, `character_inventory`, `mail`,
`mail_items` (via core mail machinery). COD subject line is the item's `Name1`.

### `GenerateGearContext` — l.1992 (the core)
```cpp
static std::string GenerateGearContext(Player* bot, Player* player)
```
Returns the parenthetical string for `{gear_context}` (or `""` to inject
nothing). Steps:

**a. Armor-slot scan (l.2003–2035).** Iterates `EQUIPMENT_SLOT_START..END`
but `continue`s on anything that is not one of the nine armor slots
(`HEAD, SHOULDERS, CHEST, WAIST, LEGS, FEET, WRISTS, HANDS, BACK`) — weapons
and jewelry are explicitly out of scope. For each armor slot it reads
`player->GetItemByPos(INVENTORY_SLOT_BAG_0, slot)`; `ilvl` is the template
`ItemLevel` or `0` for an empty slot. Tracks:
- `worstIlvl` / `worstSlot` — lowest ilvl seen (empty slot = 0 always wins).
- `epicCount` — items with `Quality >= ITEM_QUALITY_EPIC`.
- `resilienceCount` — items with any `ItemStat[s].ItemStatType == ITEM_MOD_RESILIENCE_RATING` and value > 0 (counts the item once, `break`s the stat loop).
- `hasSetPiece` — any item with `proto->ItemSet != 0`.

If `worstSlot == 255` (no armor slots existed at all) returns `""`.

**b. Recognition tiers (l.2042–2054).** Gate:
`if (worstIlvl > 0 && worstIlvl >= player->GetLevel())` — i.e. *no empty armor
slot* and the weakest piece's ilvl is at least the player's level. These are
commentary-only, so they first call `GearCommentaryAllowed`; if throttled,
return `""`. Otherwise, in priority order:
- `resilienceCount >= 4` → "…serious PvP resilience gear - not someone to lecture about gear."
- `epicCount >= 5` → "…decked in epic raid gear[, set pieces and all]…" (`hasSetPiece` toggles the suffix).
- else → "…their gear is solid for their level - no weak spots worth mentioning."

**Recognized players return here and are therefore never give candidates.**

**c. Bag scan for an upgrade (l.2056–2076).** `maxSub =
MaxArmorSubclass(...)`. A `consider` lambda picks the best eligible item the
**bot** carries; an item qualifies only if all hold:
- not null, not `IsSoulBound()`;
- `proto->Class == ITEM_CLASS_ARMOR`, `SubClass != ITEM_SUBCLASS_ARMOR_MISC`, `SubClass <= maxSub` (wearable weight);
- `proto->RequiredLevel <= player->GetLevel()`;
- `proto->ItemLevel > worstIlvl + 1` (a *real* upgrade — at least 2 ilvls over the weakest slot, so an empty slot with worstIlvl 0 needs ilvl ≥ 2);
- higher ilvl than the current `offer` (keeps the best).

Scanned locations: backpack `INVENTORY_SLOT_ITEM_START..END` on
`INVENTORY_SLOT_BAG_0`, then every equipped bag
`INVENTORY_SLOT_BAG_START..END` via `bot->GetBagByPos(b)` and each
`bag->GetItemByPos(s)`.

**d. Base commentary string (l.2078–2082).** Always built:
`(You inspected {name}: their weakest piece is the {slotName} ({item level N | empty}). As a class they want {ClassPrimaryStat}.` — note the **open paren, no
close paren yet.**

**e. Give roll (l.2084–2142).** Only if `offer != nullptr`:
- `giveChance` starts at `2.0f`, overridden by
  `g_PersonalityTemplates[personality].gearGiveChance` if the personality is
  known.
- Fire condition: `giveChance > 0.0f && CanGiveGearNow(bot, player) && roll_chance_f(giveChance)` (percent roll via the project RNG helper).
- On success: `cod = IsGearSeller(personality) ? std::max<uint32>(SellPrice * 4, 500) : 0` (sellers charge ≥ 5 silver).
- **Relationship gate**: `mailEligible = (bot->GetGuildId() && bot->GetGuildId() == player->GetGuildId()) || (bot->GetGroup() && bot->GetGroup()->IsMember(player->GetGUID()))`.
  - **Mail branch**: `MailBagItemTo(...)` → `RegisterGearGive(...)` → append the COD ("…mailed them your {item} with a {cod/100} silver COD - tell them to check their mailbox and pay up.") or gift ("…as a gift - tell them to check their mailbox.") sentence; `LOG_INFO("server.loading", "[Ollama Chat] [GearGive] ... mailed ...")`; `gaveSomething = true`.
  - **Stranger branch**: `CharacterDatabase.Execute("REPLACE INTO mod_ollama_chat_pending_gives (bot_guid, player_guid, item_guid, cod) VALUES ({}, {}, {}, {})", ...)` using **low GUIDs** (`GetCounter()`) → `RegisterGearGive(...)` → append the "open trade with you" (COD or gift) sentence; `LOG_INFO(... "parked ... for in-person trade ...")`; `gaveSomething = true`.
- **Roll failed / no offer**: nothing is appended — "no empty promises."

**f. Throttle + close (l.2144–2151).**
```cpp
if (!gaveSomething && !GearCommentaryAllowed(bot, player))
    return "";
ctx += ")";
return ctx;
```
Because of `&&` short-circuit, when `gaveSomething` is true
`GearCommentaryAllowed` is **not called** — a real give bypasses the
commentary throttle *and* does not stamp the commentary cooldown. Plain
upgrade commentary (no give) is throttled here exactly like the recognition
tiers. The give sentence is only ever appended inside the successful
mail/park branches, which is what enforces the **prompt-never-lies invariant**.

### `GenerateBotPrompt` — l.2154
```cpp
std::string GenerateBotPrompt(Player* bot, std::string playerMessage, Player* player,
                              std::string const& extraContext, bool suppressGearContext)
```
Builds the full prompt from `g_ChatPromptTemplate` via `SafeFormat`. The only
gear-relevant line is `fmt::arg("gear_context", suppressGearContext ?
std::string() : GenerateGearContext(bot, player))` (l.2267). When
`suppressGearContext` is set — the AskWhoMe disambiguation case, `isWhoMe`
from the caller — `GenerateGearContext` is **not even called**, so no give
happens and no cooldown is consumed on a "who, me?" turn.

### Completion side — `PendingGiveItemGuid` / `ClearPendingGive` / `HandlePendingGearGive`
(mod-playerbots `TradeStatusAction.cpp`, l.35 / l.57 / l.430)
```cpp
static ObjectGuid::LowType PendingGiveItemGuid(Player* bot, Player* trader, uint32* codOut)
bool TradeStatusAction::HandlePendingGearGive(Event& event, Player* trader,
                                              ObjectGuid::LowType itemGuidLow, uint32 cod)
```
`TradeStatusAction::Execute` (l.63) checks, **only when the trader is a real
player** (`!traderBotAI`), whether there is a fresh parked give
(`created_at > NOW() - INTERVAL 10 MINUTE`) and, if so, diverts to
`HandlePendingGearGive` before the normal trade logic. That handler is a small
state machine on the trade `status`:
- `TRADE_STATUS_BEGIN_TRADE`: reacquire the item by GUID (`bot->GetItemByGuid(ObjectGuid::Create<HighGuid::Item>(itemGuidLow))`). **Item gone** (equipped/sold/mailed since parking) → `ClearPendingGive` + weak-symbol LLM apology whisper + `HandleCancelTradeOpcode`. Else face trader + `HandleBeginTradeOpcode`.
- `TRADE_STATUS_OPEN_WINDOW`: `myTrade->SetItem(TradeSlots(0), item)`; if COD, whisper the price; pre-`HandleAcceptTradeOpcode`.
- `TRADE_STATUS_TRADE_ACCEPT` / `BACK_TO_TRADE`: **payment hold** — if `cod` and `trader->GetTradeData()->GetMoney() < cod`, return without accepting; the core re-fires the action as trade contents change, so the bot re-accepts once the money covers the COD. Gifts accept immediately.
- `TRADE_STATUS_TRADE_COMPLETE`: `ClearPendingGive` (row deleted) + LLM whisper + `LOG_INFO("playerbots", "[GearGive] ... handed item over ...")`.

---

## 4. Data structures & DB

**File-local globals (all in the handler):**
| Symbol | Type | Purpose |
|---|---|---|
| `g_gearGiveMutex` | `std::mutex` | guards the two give-cooldown maps |
| `g_lastGiveByBot` | `std::unordered_map<uint64_t,uint32>` | last give time per bot (raw GUID → ms) |
| `g_lastGiveByPair` | `std::map<std::pair<uint64_t,uint64_t>,uint32>` | last give time per bot/player pair |
| `g_gearCtxMutex` | `std::mutex` | guards the commentary-throttle map |
| `g_lastGearCtxByPair` | `std::map<std::pair<uint64_t,uint64_t>,uint32>` | last commentary time per pair |

**Cross-module config globals** (`mod-ollama-chat_config.{h,cpp}`):
- `g_GearGiveBotCooldownMin` (init 30), `g_GearGivePairCooldownMin` (init 1440).
- `g_PersonalityTemplates` : `unordered_map<string, BotPersonalityTemplate>`; the relevant field is `float gearGiveChance = 2.0f` ("% chance per gear-context that the bot actually mails the item"). Populated in `LoadPersonalityTemplates` (config.cpp l.672+).

**DB tables:**
- `mod_ollama_chat_personality_templates` — **read** at config load; the `gear_give_chance` column feeds `gearGiveChance`. Loaded conditionally after an `information_schema.COLUMNS` probe (see Section 7).
- `mod_ollama_chat_pending_gives` — **written** here (`REPLACE INTO`, columns `bot_guid, player_guid, item_guid, cod`; `created_at` defaults to `CURRENT_TIMESTAMP`; PK `(bot_guid, player_guid)`). **Read/deleted** by mod-playerbots. Created by `data/sql/characters/base/2026_07_03_personality_gear_give.sql` (same file that adds the `gear_give_chance` column). GUIDs stored as low counters (`GetCounter()`).
- Core mail/item tables — `item_instance`, `character_inventory`, `mail`, `mail_items` — written indirectly by `MailBagItemTo` via `MoveItemFromInventory` / `SaveToDB` / `MailDraft::SendMailTo`.

The `pending_gives` PK on `(bot_guid, player_guid)` plus `REPLACE INTO` means a
pair has at most one parked give and a new roll overwrites a stale one (in
practice rare, given the 24 h pair cooldown).

---

## 5. Concurrency & threading

- **Primary path (real player chats):** `OnPlayerCanUseChat` → `ProcessChat` →
  `GenerateBotPrompt` → `GenerateGearContext` all run on the **world thread**,
  synchronously, *before* the `std::thread(...).detach()` at l.1472 that does
  the Ollama call. So the inventory reads, `MoveItemFromInventory`, the mail
  transaction, and the `pending_gives` write happen on the world thread —
  the same tick that received the chat packet. This is the case the l.1440
  comment asserts ("Runs here on the world thread … like GenerateGearContext's
  inventory ops below").
- **Bot-to-bot fan-out:** the detached response thread, after a bot posts its
  reply, calls `ProcessBotChatMessage` (l.1580) which re-enters `ProcessChat`
  **on the worker thread**. That path can reach `GenerateGearContext` again
  (responding bot ↔ sender bot), so its map access — and, in principle, its
  inventory/mail ops — can execute off the world thread. This is why the two
  mutexes exist and are load-bearing rather than decorative:
  `g_gearGiveMutex` and `g_gearCtxMutex` serialize all reads/writes of the
  cooldown maps across the world thread and any worker threads.
- The four cooldown maps are **file-local statics** touched only inside
  `CanGiveGearNow` / `RegisterGearGive` / `GearCommentaryAllowed`, always under
  their mutex — no map is exposed elsewhere.
- `CharacterDatabase.Execute(...)` (the park write) is an **async** enqueue to
  the DB worker; `MailBagItemTo`'s transaction is committed via
  `CommitTransaction`. Neither blocks on network I/O in the hook beyond the DB
  queue.
- On the completion side, `TradeStatusAction` runs inside the playerbot AI
  update (world thread); `OllamaChat_SpeakSituation` immediately detaches its
  own query thread and reacquires the bot by GUID before speaking.

---

## 6. Config keys

| Key (`sConfigMgr->GetOption`) | Default | Read at | Meaning |
|---|---|---|---|
| `OllamaChat.GearContextCooldownMin` | `10` | `GearCommentaryAllowed`, l.1952 (inline, every call) | minutes between gear *commentary* lines per bot/player pair |
| `OllamaChat.GearGiveBotCooldownMin` | `30` | config load, l.414 → `g_GearGiveBotCooldownMin` | minutes between any give by one bot |
| `OllamaChat.GearGivePairCooldownMin` | `1440` | config load, l.415 → `g_GearGivePairCooldownMin` | minutes between gives to the same player by one bot |

Per-personality generosity is **not** a config key — it is the
`gear_give_chance` DB column (FLOAT, DB default `2.0`; code fallback `2.0f`
when the personality/key is unknown). Tuned per personality in
`2026_07_03_personality_gear_give.sql` (e.g. `TRADER`/`GOBLIN_MERCHANT` = 12,
`MENTOR` = 8, `GRUMPY_VETERAN`/`EDGE_LORD` = 1).

---

## 7. Failure modes & gotchas

- **Missing migration columns.** Config load probes
  `information_schema.COLUMNS` for `weight` and separately for
  `gear_give_chance` before selecting them, so a DB without
  `2026_07_02_personality_behavior_columns.sql` or
  `2026_07_03_personality_gear_give.sql` still loads with defaults
  (`gearGiveChance` stays `2.0f`) and only logs a warning — it does not crash.
- **Missing `pending_gives` table on the consumer side.** `PendingGiveItemGuid`
  caches an `information_schema.TABLES` probe in a `static int hasTable` and
  returns 0 (no parked give) if the table is absent — the trade proceeds
  normally.
- **Weak symbol for chat lines.** `OllamaChat_SpeakSituation` is declared
  `[[gnu::weak]]` in `TradeStatusAction.cpp`; if mod-ollama-chat is not linked
  the whispers are silently skipped, but the item still changes hands.
- **Null / reacquire-by-GUID.** The parked item is stored as a GUID and
  reacquired with `bot->GetItemByGuid(...)` at trade time; if the bot no longer
  has it (equipped/sold/mailed in the interim) the handler apologizes, clears
  the row, and cancels — it never trades a phantom item.
- **Empty armor slot vs recognition.** An empty armor slot sets `worstIlvl = 0`,
  which fails the `worstIlvl > 0` recognition gate — a well-geared player with
  one bare slot (e.g. no cloak) is treated as an upgrade candidate, not
  recognized. Intentional but easy to trip over.
- **The 2-ilvl upgrade floor.** `consider` requires `ItemLevel > worstIlvl + 1`,
  so a sidegrade (equal ilvl) or a +1 is never offered.
- **Prompt-vs-reality invariant is one-directional.** The code guarantees the
  prompt only *mentions* an item that was actually sent/parked. The inverse is
  not guaranteed: the LLM may fail to relay a give sentence, leaving the player
  a mailbox/parked surprise with no chat line. Only a code path bug (appending
  the sentence without a successful transfer) would break the real invariant —
  keep the append strictly inside the `MailBagItemTo == true` / post-`REPLACE`
  branches.
- **`MailBagItemTo` never returns false.** Callers treat `true` as "sent"; if a
  real failure mode is ever added, the `gaveSomething`/append logic must be
  revisited so the prompt stays honest.
- **GUID key mismatch is deliberate.** Cooldown maps key on
  `GetRawValue()` (full ObjectGuid), while `pending_gives` rows and the
  playerbots lookup use `GetCounter()` (low GUID). Don't "unify" them.
- **`getMSTime()` wraparound.** Cooldown timestamps are uint32 ms and wrap
  ~every 49.7 days; the unsigned `now - stored` delta survives a single wrap,
  but a stamp older than one wrap can transiently read as "recent." Negligible
  in practice given server restarts.

---

## 8. Cross-references

- [`../BOT-BEHAVIOR.md`](../BOT-BEHAVIOR.md) Section 5 — behaviour-level description of the gear-inspect context and recognition tiers.
- [`../BOT-ECONOMY.md`](../BOT-ECONOMY.md) Section 1 (real gear gives / mail path), Section 2 (in-person trade hand-over state machine).
- Personality system + `g_PersonalityTemplates` loading (`mod-ollama-chat_personality.cpp`, `mod-ollama-chat_config.cpp`) — source of `gearGiveChance`, `GetBotPersonality`, and `GetPersonalityQueryOptions`.
- Completion path: `modules/mod-playerbots/src/Ai/Base/Actions/TradeStatusAction.cpp` (`HandlePendingGearGive`).
- Personality-scaled *random* mail gifting reuses `gearGiveChance` in `mod-ollama-chat_events_mail.cpp` (a sibling of this subsystem, out of scope here).
