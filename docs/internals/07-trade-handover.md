# 07 — In-person trade hand-over (internals)

Developer-level reference for how a *parked* gear give
(`mod_ollama_chat_pending_gives`) is completed on a real trade window. This is
the deep companion to [BOT-ECONOMY Section 2](../BOT-ECONOMY.md#2-in-person-trade-hand-over);
that section frames the behavior, this one walks the code function by function
for someone about to modify or debug it.

All code discussed lives in one translation unit:
`modules/mod-playerbots/src/Ai/Base/Actions/TradeStatusAction.cpp`
(header: `TradeStatusAction.h`). The parking side (the `REPLACE INTO` that
creates the row) lives in mod-ollama-chat and is out of scope here — see
[BOT-ECONOMY Section 1](../BOT-ECONOMY.md#1-real-gear-gives-mail-path).

---

## 1. Purpose

When a bot promises a stranger a piece of gear in chat, it does not mail it
(mail is reserved for guildmates/group members); instead mod-ollama-chat parks
the promise as a row in `mod_ollama_chat_pending_gives` and tells the player to
open trade. This subsystem is the mod-playerbots half that watches every trade
the bot enters, notices when the trader is the player who was promised an item,
and drives the real trade window to completion — including holding acceptance
until a COD (cash-on-delivery) price is paid, and apologizing in-voice if the
item is gone. It is a small `SMSG_TRADE_STATUS`-driven state machine bolted on
*in front of* the module's normal trade logic.

---

## 2. Entry points & call graph

The bot's server session emits `SMSG_TRADE_STATUS` on every trade-state change.
mod-playerbots intercepts that outgoing packet and routes it to the `"accept
trade"` action:

```
SMSG_TRADE_STATUS (server → bot session)
  └─ PlayerbotAI: botOutgoingPacketHandlers.AddHandler(SMSG_TRADE_STATUS, "trade status")
        (src/Bot/PlayerbotAI.cpp:187)
     └─ WorldPacketTrigger("trade status")::ExternalEvent → Check() → Event("trade status", packet, owner)
        (src/Ai/Base/Trigger/WorldPacketTrigger.cpp)
        └─ TriggerNode "trade status" → NextAction("accept trade", relevance)
           (src/Ai/Base/Strategy/WorldPacketHandlerStrategy.cpp:35)
           └─ TradeStatusAction::Execute(Event event)          ◄── entry point
              │   Player* trader = bot->GetTrader()
              │   PlayerbotAI* traderBotAI = GET_PLAYERBOT_AI(trader)
              │
              ├─ if (!traderBotAI)                              // real player only
              │     PendingGiveItemGuid(bot, trader, &pendingCod)   // one DB SELECT
              │        └─ (first call) information_schema probe → static hasTable
              │     if row found → HandlePendingGearGive(event, trader, pendingItem, pendingCod)
              │        │   Item* item = bot->GetItemByGuid(Create<HighGuid::Item>(itemGuidLow))
              │        ├─ TRADE_STATUS_BEGIN_TRADE   → face + HandleBeginTradeOpcode
              │        │                               (or ClearPendingGive + SpeakSituation apology + cancel)
              │        ├─ TRADE_STATUS_OPEN_WINDOW    → SetItem(slot 0) + [SpeakSituation price] + pre-accept
              │        ├─ TRADE_STATUS_TRADE_ACCEPT   ┐ payment hold, then HandleAcceptTradeOpcode
              │        │  TRADE_STATUS_BACK_TO_TRADE  ┘
              │        └─ TRADE_STATUS_TRADE_COMPLETE → ClearPendingGive + SpeakSituation + LOG_INFO
              │
              └─ (no pending give) ... normal trade path:
                    BeginTrade() / CheckTrade() / CalculateCost() / HandleAcceptTradeOpcode
```

The pending-give interception is the **first thing** `Execute()` does after
acquiring the trader (before master/group gating, before the
`enableRandomBotTrading` lockout, before the security-cancel), so a stranger can
complete this one specific trade even on a realm where random-bot trading is
otherwise disabled.

---

## 3. Function-by-function

### `SpeakSituation` (file-scope static wrapper over a weak symbol)

```cpp
[[gnu::weak]] void OllamaChat_SpeakSituation(Player* bot, Player* target, std::string const& situation, bool whisper);
static void SpeakSituation(Player* bot, Player* target, std::string const& situation, bool whisper)
{
    if (OllamaChat_SpeakSituation)
        OllamaChat_SpeakSituation(bot, target, situation, whisper);
}
```

- **What**: the cross-module escape hatch that makes a bot say/whisper one
  short, in-character, LLM-generated line about a concrete `situation` string.
- The strong definition lives in mod-ollama-chat
  (`mod-ollama-chat_handler.cpp`); it is re-declared here `[[gnu::weak]]` and not
  shared via any header. Both modules static-link into the one worldserver
  binary, so the linker binds this weak reference to the strong definition. If
  the chat module is compiled out, `OllamaChat_SpeakSituation` resolves to null
  and the `if` makes every call a silent no-op — this file still compiles,
  links, and runs.
- **Side effects**: the real implementation is fire-and-forget — it captures raw
  GUIDs into a detached `std::thread`, runs the Ollama query off-thread, and
  reacquires the players via `ObjectAccessor::FindPlayer` before speaking. It
  never blocks the caller (the world tick). See
  [BOT-ECONOMY Section 4](../BOT-ECONOMY.md#4-llm-situational-dialogue-ollamachat_speaksituation)
  for its internals.

### `PendingGiveItemGuid` (file-scope static)

```cpp
static ObjectGuid::LowType PendingGiveItemGuid(Player* bot, Player* trader, uint32* codOut)
```

- **What**: looks up whether `bot` has a live parked give for `trader`; returns
  the parked item's guid counter (`0` = none), and writes the COD into `*codOut`
  when non-null.
- **Step by step**:
  1. **One-time table probe** — a function-local `static int hasTable = -1;`
     tri-state (unknown / absent / present). On the first call only, it queries
     `information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME =
     'mod_ollama_chat_pending_gives'` and caches `1`/`0`. This is the
     graceful-degradation gate: if the chat module's migration
     (`2026_07_03_personality_gear_give.sql`) never ran, the table is absent,
     `hasTable` sticks at `0`, and every subsequent call returns `0` with no
     further queries — the bot falls straight through to the normal trade path.
  2. If `!hasTable`, return `0`.
  3. Otherwise run the real lookup:
     ```sql
     SELECT item_guid, cod FROM mod_ollama_chat_pending_gives
     WHERE bot_guid = {} AND player_guid = {}
       AND created_at > NOW() - INTERVAL 10 MINUTE
     ```
     bound with `bot->GetGUID().GetCounter()` and
     `trader->GetGUID().GetCounter()` (guid **counters**, not full 64-bit GUIDs).
  4. No row (never parked, or the row is older than 10 minutes) → return `0`.
  5. Row found → `*codOut = (*result)[1].Get<uint32>()`;
     return `(*result)[0].Get<ObjectGuid::LowType>()`.
- **Inputs/outputs**: `bot`, `trader` (the resolved `bot->GetTrader()`), and an
  out-param `codOut`. Returns the item guid counter.
- **Non-obvious**: the **10-minute expiry is enforced here, at read time only**
  (the `created_at > NOW() - INTERVAL 10 MINUTE` predicate). Expired rows are
  never deleted by this subsystem — they simply become invisible and are later
  overwritten by the chat module's next `REPLACE INTO` for that pair.

### `ClearPendingGive` (file-scope static)

```cpp
static void ClearPendingGive(Player* bot, Player* trader)
```

- **What**: deletes the pending-give row for the pair.
  `DELETE FROM mod_ollama_chat_pending_gives WHERE bot_guid = {} AND player_guid
  = {}`, bound with the two guid counters.
- **Non-obvious**: the DELETE has **no `created_at` filter** (unlike the SELECT)
  — it removes the row for the pair regardless of age. Called on successful
  completion, and also on the "item vanished" apology so the dead promise cannot
  re-trigger on a retry.
- It does **not** probe `hasTable`; it is only ever reached after
  `PendingGiveItemGuid` already returned a row, so the table is known to exist.

### `TradeStatusAction::Execute` (the interception)

```cpp
bool TradeStatusAction::Execute(Event event) override;
```

Only the pending-give interception at the top is in scope; the remainder is the
module's stock trade logic (Section 3, "normal-trade helpers"). The intercept:

```cpp
Player* trader = bot->GetTrader();
Player* master = GetMaster();
if (!trader)
    return false;

PlayerbotAI* traderBotAI = GET_PLAYERBOT_AI(trader);

// In-person gear give promised in chat: hand the parked item over
uint32 pendingCod = 0;
if (!traderBotAI)
    if (ObjectGuid::LowType pendingItem = PendingGiveItemGuid(bot, trader, &pendingCod))
        return HandlePendingGearGive(event, trader, pendingItem, pendingCod);
```

- Bails immediately if there is no trader.
- `traderBotAI` distinguishes a real player from another bot. The pending-give
  path is **real-players-only** (`if (!traderBotAI)`) — bots never redeem each
  other's parked gives.
- `PendingGiveItemGuid` is called **once**, capturing both the item guid and
  (via `&pendingCod`) the COD. Both are then **passed through** into
  `HandlePendingGearGive` as the `itemGuidLow` and `cod` parameters. This is the
  recent dedupe: the state-machine callee does not re-query the DB for its own
  item/cod, so there is exactly **one DB SELECT per `SMSG_TRADE_STATUS` packet**,
  not two.
- If a row exists, `Execute` **returns whatever `HandlePendingGearGive` returns**
  and never touches the normal trade path. This is deliberate placement: it runs
  ahead of the master/group check (`trader != master && ...`), the
  `sPlayerbotAIConfig.enableRandomBotTrading == 0` lockout, and the
  `HandleCancelTradeOpcode` security gate further down `Execute`.

### `TradeStatusAction::HandlePendingGearGive` (the state machine)

```cpp
bool TradeStatusAction::HandlePendingGearGive(Event& event, Player* trader,
                                              ObjectGuid::LowType itemGuidLow, uint32 cod);
```

Preamble, run on every call regardless of state:

```cpp
WorldPacket p(event.getPacket());
p.rpos(0);
uint32 status;
p >> status;

Item* item = bot->GetItemByGuid(ObjectGuid::Create<HighGuid::Item>(itemGuidLow));
```

- Re-reads the trade `status` from the packet each time (the packet is re-parsed
  from position 0).
- **Re-resolves the item from the bot's live inventory every call** via
  `GetItemByGuid`. This is intentional and separate from the DB dedupe: the DB
  gives a stable guid counter, but the actual `Item*` must be looked up fresh
  because the bot could have equipped, sold, mailed, or auctioned the parked item
  between two trade packets. `item` may legitimately be `nullptr`.

Then a `switch (status)`:

| `status` | Behavior |
|---|---|
| `TRADE_STATUS_BEGIN_TRADE` | **Item gone** (`!item`): `ClearPendingGive(bot, trader)`, whisper an LLM apology `SpeakSituation(bot, trader, "you promised them a piece of gear but no longer have it", true)`, then `HandleCancelTradeOpcode` with a zero-status packet, `return false`. **Item present**: face the trader if not already (`HasInArc(CAST_ANGLE_IN_FRONT, ...)` → `SetFacingToObject`), then `HandleBeginTradeOpcode` to accept into the trade session, `return true`. |
| `TRADE_STATUS_OPEN_WINDOW` | `TradeData* myTrade = bot->GetTradeData();` guard `if (!myTrade || !item) return false;`. Place the item: `myTrade->SetItem(TradeSlots(0), item);` (trade slot 0). If `cod`, whisper the price via `SpeakSituation(bot, trader, Acore::StringFormat("you put your {} in the trade window; it costs them {} silver, ask them to add the money", item->GetTemplate()->Name1, cod / 100), true)`. Then **pre-accept** with `HandleAcceptTradeOpcode`, `return true`. |
| `TRADE_STATUS_TRADE_ACCEPT` / `TRADE_STATUS_BACK_TO_TRADE` | **The COD payment hold**: `if (cod && trader->GetTradeData() && trader->GetTradeData()->GetMoney() < cod) return true;` — return *without* accepting, leaving the trade open until the player adds gold. Otherwise (gift, or COD now covered) `HandleAcceptTradeOpcode`, `return true`. |
| `TRADE_STATUS_TRADE_COMPLETE` | `ClearPendingGive(bot, trader)` (row deleted), whisper `SpeakSituation(bot, trader, cod ? "you just sold them a piece of gear in a trade, they paid up" : "you just gave them a piece of gear for free in a trade", true)`, and `LOG_INFO("playerbots", "[GearGive] {} handed item over to {} in trade (cod {} copper)", bot->GetName(), trader->GetName(), cod)`. `return true`. |
| default | `return false`. |

- **Inputs**: the trade-status `Event`, the resolved real-player `trader`, the
  parked item's guid counter, and the COD copper amount.
- **Side effects**: writes trade packets through the bot's session
  (`bot->GetSession()->Handle*TradeOpcode`), mutates the bot's `TradeData`
  (`SetItem`), deletes the DB row on terminal states, spawns the off-thread
  LLM whisper, and emits the info log.
- **Non-obvious — how the payment hold makes progress**: the bot pre-accepts at
  `OPEN_WINDOW`. When the player then changes trade contents (adds their gold),
  the core's `TradeData`/`TradeHandler` clears both parties' accept flags and
  re-emits a status (`TRADE_STATUS_BACK_TO_TRADE`,
  `src/server/game/Entities/Player/TradeData.cpp:130`). That re-fires this whole
  action, which re-parses `status`, re-checks `GetMoney() < cod`, and either
  holds again or re-accepts. There is no timer and no stored state between
  packets other than the DB row — every packet reconstructs everything from
  `itemGuidLow`, `cod`, and live game state.

### Normal-trade helpers (not the pending-give path, listed for completeness)

`Execute()` falls through to these when there is no pending give. They are the
stock module trade logic and are documented here only so the call graph is
complete:

- `void TradeStatusAction::BeginTrade()` — `HandleBeginTradeOpcode` + tells the
  master the bot's inventory (used on `TRADE_STATUS_BEGIN_TRADE` in the normal
  path).
- `bool TradeStatusAction::CheckTrade()` — the value/fairness gate for ordinary
  buy/sell trades (discounts, "I don't need this", "I want N for this").
- `int32 TradeStatusAction::CalculateCost(Player* player, bool sell)` — sums
  `SellPrice`/`BuyPrice × count × multiplier` over the trade slots.

The pending-give path deliberately **bypasses all three** — a promised give is
neither value-checked nor discount-adjusted.

---

## 4. Data structures & DB

**Table read/written** (all in `acore_characters`):

`mod_ollama_chat_pending_gives` — created by the chat module's migration
`2026_07_03_personality_gear_give.sql`; PK `(bot_guid, player_guid)`.

| column | type/meaning | used here |
|---|---|---|
| `bot_guid` | bot guid **counter** | SELECT/DELETE key |
| `player_guid` | player guid **counter** | SELECT/DELETE key |
| `item_guid` | parked item's guid counter (still in the bot's bags) | returned by `PendingGiveItemGuid`, fed to `GetItemByGuid` |
| `cod` | copper the player must put in the window (`0` = gift) | out-param `codOut`, drives the payment hold |
| `created_at` | auto timestamp | 10-minute read-time expiry predicate |

Writes from this file: only `ClearPendingGive`'s `DELETE`. The `REPLACE INTO`
that creates rows is on the chat-module side.

**`information_schema.TABLES`** — read once (probe) to decide whether the table
exists at all.

**In-file state / globals**:

- `static int hasTable` (function-local in `PendingGiveItemGuid`) — the cached
  tri-state table-existence flag. Process-lifetime.
- No maps, no caches beyond that. Per-packet state (`status`, `item`,
  `pendingCod`) is stack-local and reconstructed each call.

**Core types touched**: `TradeData` (`bot->GetTradeData()`,
`trader->GetTradeData()`, `SetItem`, `GetMoney`); `TradeSlots` /
`TRADE_SLOT_TRADED_COUNT` (= 6, `src/server/game/Entities/Player/TradeData.h`);
`ObjectGuid::Create<HighGuid::Item>(itemGuidLow)`; `ItemTemplate::Name1`.

---

## 5. Concurrency & threading

- **`Execute` / `HandlePendingGearGive` / `PendingGiveItemGuid` /
  `ClearPendingGive` all run on the world thread.** They are invoked
  synchronously from the bot's AI update while it processes the intercepted
  `SMSG_TRADE_STATUS` packet. All `CharacterDatabase.Query`/`.Execute` calls
  here are **synchronous, blocking** queries on that thread (not the async
  `_queryProcessor` path). They are cheap — a primary-key lookup or delete on a
  tiny table, guarded by the `hasTable` short-circuit — and only fire while a
  real trade window is actually open, so blocking the tick briefly is
  acceptable.
- **`static int hasTable`** has no mutex. It is safe because every bot's AI runs
  on the single world thread, so the read-modify-write of the flag is never
  concurrent. (C++ also guarantees thread-safe *initialization* of function-local
  statics, but the guard here relies on the single-threaded world loop, not on
  that guarantee.)
- **The only off-world-thread work is inside `SpeakSituation`** →
  `OllamaChat_SpeakSituation`, which detaches a `std::thread` for the Ollama HTTP
  call and reacquires `Player*`s by GUID before speaking. That call returns
  immediately; the trade completes on the world thread without waiting on the
  LLM. This is why the design invariant holds — the item moves synchronously,
  the words arrive later and asynchronously.
- No shared mutable state crosses threads: the DB row is the only durable state,
  and the detached speak-thread captures GUIDs by value, never the `Player*` or
  `Item*` pointers.

---

## 6. Config keys

**This file reads no `sConfigMgr` option specific to the pending-give feature.**
The 10-minute expiry window is a hardcoded SQL literal (`INTERVAL 10 MINUTE`),
not a config key. The `sPlayerbotAIConfig` members it *does* touch belong to the
normal-trade path and the interception's ordering:

| member (in this file) | conf key | default | relevance to hand-over |
|---|---|---|---|
| `sPlayerbotAIConfig.sightDistance` | `AiPlayerbot.SightDistance` | `100.0f` | arc/facing check before `SetFacingToObject` on `BEGIN_TRADE` |
| `sPlayerbotAIConfig.enableRandomBotTrading` | `AiPlayerbot.EnableRandomBotTrading` | `1` | the lockout the pending-give interception jumps *ahead of* — a parked give completes even when this is `0` |

The keys that actually gate whether a give ever gets **parked** live in
mod-ollama-chat (read in `LoadOllamaChatConfig()`), not here:

| key | default | meaning |
|---|---|---|
| `OllamaChat.GearGiveBotCooldownMin` | `30` | minutes between any two gives by one bot |
| `OllamaChat.GearGivePairCooldownMin` | `1440` | minutes before the same bot gives the same player again |

(The COD amount stored in the row is likewise computed on the parking side:
`cod = max(SellPrice * 4, 500)` copper for seller personalities, `0` otherwise —
see [BOT-ECONOMY Section 1](../BOT-ECONOMY.md#1-real-gear-gives-mail-path).)

---

## 7. Failure modes & gotchas

- **Table absent (older DB, chat migration not applied)** — the
  `information_schema` probe caches `hasTable = 0` on first call;
  `PendingGiveItemGuid` returns `0` forever after; `Execute` never enters the
  pending path and the bot behaves as stock playerbots. Graceful degrade, no
  errors.
- **Chat module compiled out** — `OllamaChat_SpeakSituation` weak-resolves to
  null; `SpeakSituation` becomes a no-op. The item still transfers correctly
  (all opcode work is local to playerbots); only the in-voice lines go silent.
  Note this is GCC/Clang ELF behavior and not portable to MSVC — fine for this
  Linux-only deployment.
- **Item vanished between park and trade** — the bot may have equipped,
  vendored, mailed, or auctioned the parked item (the AH filter does *not*
  exclude pending-give items). `GetItemByGuid` returns null; on `BEGIN_TRADE`
  the bot clears the row, whispers an apology, and cancels the trade. If the item
  vanishes *after* `BEGIN_TRADE` but before `OPEN_WINDOW`, the `if (!myTrade ||
  !item) return false;` guard bails without placing anything and the trade simply
  can't complete.
- **Reacquire-by-GUID everywhere** — the DB stores a guid counter, never a
  pointer; the `Item*` and (in the speak thread) the `Player*`s are always
  re-resolved at use time. Nothing holds a stale pointer across packets or across
  the thread boundary.
- **Stale rows are not garbage-collected** — expiry is read-time only. A row
  whose player never showed up lingers past 10 minutes (invisible to the SELECT)
  until the chat module's next `REPLACE INTO` for that pair overwrites it — at
  least the 24h pair cooldown away. The item is never lost; the promise just
  lapses and the bot keeps the gear.
- **COD race is benign** — the payment hold re-checks `GetMoney() < cod` on every
  re-fired status packet. If the player removes gold after adding it, the core
  re-emits `BACK_TO_TRADE` (accept cleared) and the bot re-holds. The bot never
  completes a COD trade with insufficient money in the window.
- **One SELECT per packet** — because `Execute` re-runs on every
  `SMSG_TRADE_STATUS`, `PendingGiveItemGuid` does hit the DB once per trade-state
  transition for the life of the window. The `hasTable` short-circuit keeps that
  to a single indexed lookup; the recent dedupe (passing `pendingItem`/`cod`
  into `HandlePendingGearGive` rather than re-querying inside it) keeps it to
  exactly one, not two.

---

## 8. Cross-references

- [BOT-ECONOMY.md Section 2 — In-person trade hand-over](../BOT-ECONOMY.md#2-in-person-trade-hand-over)
  — the behavior-level framing this doc deepens (state-machine table, expiry,
  design invariant).
- [BOT-ECONOMY.md Section 1 — Real gear gives (mail path)](../BOT-ECONOMY.md#1-real-gear-gives-mail-path)
  — where rows get **parked**: the `gear_give_chance` roll, `mailEligible`
  guilds/group split, COD computation, and the `REPLACE INTO
  mod_ollama_chat_pending_gives` this subsystem consumes.
- [BOT-ECONOMY.md Section 4 — OllamaChat_SpeakSituation](../BOT-ECONOMY.md#4-llm-situational-dialogue-ollamachat_speaksituation)
  — internals of the weak-symbol LLM whisper used for the three voiced lines
  here.
- [BOT-ECONOMY.md Section 3 — Organic bot auction house](../BOT-ECONOMY.md#3-organic-bot-auction-house)
  — the AH path that can consume a parked item out from under a pending give
  (feeds the "item vanished" apology).
- [BOT-BEHAVIOR.md Section 5 — Gear-inspect chat context](../BOT-BEHAVIOR.md#5-gear-inspect-chat-context)
  — how a bot decides it has an upgrade worth promising in the first place.
