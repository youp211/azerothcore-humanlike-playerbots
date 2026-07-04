# Internals: Organic bot auction-house economy

Developer reference for the subsystem that makes idle bots list their real
spare drops on the auction house. This is the code-level companion to the
behavior-level framing in [BOT-ECONOMY Section 3](../BOT-ECONOMY.md) — same
terminology (organic supply stream, personality pricing, `[BotAH]` logs), but
here documented function-by-function for anyone modifying or debugging it.

All subsystem code lives in three files in `mod-playerbots`:

- `src/Ai/World/Rpg/Trigger/NewRpgTrigger.cpp` — `AhSellSparesTrigger::IsActive`
- `src/Ai/World/Rpg/Action/RpgSubActions.cpp` — `AhSellSparesAction`, `AhPriceFactorFor`
- `src/Ai/World/Rpg/Action/RpgSubActions.h` — `AhSellSparesAction` declaration

It leans on the core auction subsystem in
`src/server/game/AuctionHouse/AuctionHouseMgr.{h,cpp}` (`AuctionEntry`,
`AuctionHouseObject`, `sAuctionMgr`).

---

## 1. Purpose

When an ungrouped bot wanders into a town (via the New-RPG `WanderNpc`
activity) and ends up in interaction range of an auctioneer NPC, it lists a
few of its genuine spare items (green+ gear, tradeskill goods) on its
faction's auction house under its own name, priced according to its
mod-ollama-chat personality. The listings are real `AuctionEntry` rows —
players browse, bid, buy out, and receive outbid/expiry mail through the core
`AuctionHouseMgr` exactly as with a human seller. This is a second, organic
supply stream that **coexists with** the optional `mod-ah-bot` buy/sell-side
module (which injects synthetic volume from a dedicated seller character);
the two are independent and unaware of each other.

## 2. Entry points & call graph

Execution enters through the mod-playerbots strategy engine, not a core hook.
The trigger/action pair is registered by name in the context registries and
wired into `NewRpgStrategy`, so the feature only runs when
`AiPlayerbot.EnableNewRpgStrategy = 1`.

Registration points (the `creators[...] =` map assignment and the creator
function it points at live at different lines in the same header):
- `src/Ai/Base/TriggerContext.h:243` — `creators["ah sell spares"] = &TriggerContext::ah_sell_spares;`; the creator itself is at `TriggerContext.h:461` — `static Trigger* ah_sell_spares(PlayerbotAI* botAI) { return new AhSellSparesTrigger(botAI); }`
- `src/Ai/Base/ActionContext.h:280` — `creators["ah sell spares"] = &ActionContext::ah_sell_spares;`; the creator itself is at `ActionContext.h:490` — `static Action* ah_sell_spares(PlayerbotAI* ai) { return new AhSellSparesAction(ai); }`
- `src/Ai/World/Rpg/Strategy/NewRpgStrategy.cpp:86` — `TriggerNode("ah sell spares", { NextAction("ah sell spares", 12.0f) })`

The trigger's check interval is `30` (seconds), set in the ctor
`AhSellSparesTrigger(PlayerbotAI* botAI) : Trigger(botAI, "ah sell spares", 30)`.

Per-tick call graph (all on the world thread):

```
NewRpgStrategy tick
  └─ AhSellSparesTrigger::IsActive()                        [NewRpgTrigger.cpp:22]
       ├─ botAhSellChance <= 0 / grouped / in combat  → false
       ├─ AhSellSparesAction::FindNearbyAuctioneer()        [RpgSubActions.cpp:544]
       │     └─ GetValue<GuidVector>("nearest npcs")
       │         → GetNPCIfCanInteractWith(guid, UNIT_NPC_FLAG_AUCTIONEER)
       └─ roll_chance_f(botAhSellChance)              → gates the NextAction
  └─ (on active trigger) AhSellSparesAction::Execute()       [RpgSubActions.cpp:553]
       ├─ FindNearbyAuctioneer()                       (re-run; may fail → false)
       ├─ SELECT COUNT(*) FROM auctionhouse WHERE itemowner = ?   (listing cap)
       ├─ AuctionHouseMgr::GetAuctionHouseEntryFromFactionTemplate(faction)
       ├─ sAuctionMgr->GetAuctionsMap(faction)         → AuctionHouseObject*
       ├─ consider() lambda over backpack + bags       → std::vector<Item*> spares (≤3)
       ├─ AhPriceFactorFor(bot)                         [RpgSubActions.cpp:526]
       │     └─ SELECT personality FROM mod_ollama_chat_personality WHERE guid = ?
       └─ for each spare:
             new AuctionEntry (Id via sObjectMgr->GenerateAuctionID())
             sAuctionMgr->AddAItem(item)
             auctionHouse->AddAuction(auction)
             bot->MoveItemFromInventory(...)
             txn: item->DeleteFromInventoryDB + item->SaveToDB
                  + auction->SaveToDB + bot->SaveInventoryAndGoldToDB
```

## 3. Function-by-function

### `AhSellSparesTrigger::IsActive()` — NewRpgTrigger.cpp:22

```cpp
bool AhSellSparesTrigger::IsActive()
```

Cheap gate evaluated every 30 s per bot in `NewRpgStrategy`. Step by step:

1. Early-out to `false` if any of: `sPlayerbotAIConfig.botAhSellChance <= 0.0f`
   (feature disabled), `bot->GetGroup()` (bot is grouped — grouped bots don't
   list), or `bot->IsInCombat()`.
2. Call `AhSellSparesAction::FindNearbyAuctioneer(bot, botAI)`; return `false`
   if no auctioneer is in interaction range.
3. Otherwise return `roll_chance_f(sPlayerbotAIConfig.botAhSellChance)` — a
   percent roll (default 25.0). Uses the project RNG helper `roll_chance_f`
   from `src/common/Utilities/Random.h`.

Inputs: bot state + config. Output: `bool` (should the `ah sell spares` action
fire this tick). No side effects. Note the auctioneer probe runs **twice** per
successful cycle (once here, once at the top of `Execute`) — deliberately
cheap and idempotent; the state can change between trigger and action.

### `AhSellSparesAction::FindNearbyAuctioneer(Player*, PlayerbotAI*)` — RpgSubActions.cpp:544

```cpp
Creature* AhSellSparesAction::FindNearbyAuctioneer(Player* bot, PlayerbotAI* botAI)
```

`static` helper (declared `static` in the `.h`), so both the trigger and the
action share one implementation and no instance is needed.

1. Pull the cached `GuidVector` named `"nearest npcs"` from the bot's AI object
   context: `botAI->GetAiObjectContext()->GetValue<GuidVector>("nearest npcs")->Get()`.
2. For each `ObjectGuid`, call
   `bot->GetNPCIfCanInteractWith(guid, UNIT_NPC_FLAG_AUCTIONEER)` — this applies
   the core's real interaction gates (range = `INTERACTION_DISTANCE`, alive,
   not hostile, correct NPC flag). Return the first match.
3. Return `nullptr` if none qualify.

Output: a live `Creature*` for the auctioneer, or `nullptr`. Because it uses
`GetNPCIfCanInteractWith`, items are never teleported to a distant AH — the bot
must be physically standing at the auctioneer.

### `AhPriceFactorFor(Player* bot)` — RpgSubActions.cpp:526

```cpp
static float AhPriceFactorFor(Player* bot)
```

File-`static` free function (not a class member). Returns the personality-driven
price multiplier applied to an item's base value.

1. Holds a `static std::unordered_map<std::string, float> const factors` mapping
   personality key → multiplier. Overpricers: `ELITE_ARENA_PVPER` 2.2,
   `MIN_MAXER` 1.8, `GOBLIN_MERCHANT` 1.7, `TRADER` 1.6, `GOLD_FARMER` 1.5,
   `THEORYCRAFTER` 1.4, `HARDCORE_RAIDLEAD` 1.3, `FAIRWEATHER_FRIEND` 1.3.
   Underpricers: `SCARED_NEWBIE` 0.5, `ONE_WORD_GRUNTER` 0.5, `STONER` 0.5,
   `FOOL` 0.4, `TWELVE_YEAR_OLD` 0.55, `WOW_MOM` 0.7, `HUMBLE_FARMER` 0.7,
   `SILENT_TYPE` 0.7, `CHILL_DAD` 0.75.
2. Query `SELECT personality FROM mod_ollama_chat_personality WHERE guid = {}`
   with `bot->GetGUID().GetCounter()` against `CharacterDatabase`. This is a
   **guid-counter** lookup on mod-ollama-chat's table (the same loose-coupling
   pattern as playstyles/sentiment — playerbots reads the chat module's tables
   directly, no shared header).
3. Look up the returned personality string; return its factor, or `1.0f` when
   the personality is unknown, unassigned, or the table/row is missing.

Inputs: `Player* bot`. Output: `float` multiplier. Side effect: one synchronous
`CharacterDatabase.Query`. Non-obvious: this is a **raw blocking query on the
world thread**, acceptable only because it runs at most once per listing pass,
and listing passes are rare (gated behind being at an auctioneer + the chance
roll). The number produced here is the *listing* price only; the bot's spoken
line about the price is generated separately by the voice model
(`OllamaChat_SpeakSituation` is not called here).

### `AhSellSparesAction::Execute(Event)` — RpgSubActions.cpp:553

```cpp
bool AhSellSparesAction::Execute(Event /*event*/)
```

The workhorse. Returns `true` iff ≥1 item was listed.

1. **Re-find auctioneer** — `FindNearbyAuctioneer(bot, botAI)`; return `false`
   if gone (bot may have moved between trigger and action).
2. **Listing cap** — synchronous
   `SELECT COUNT(*) FROM auctionhouse WHERE itemowner = {}`
   (`bot->GetGUID().GetCounter()`). If `active >= sPlayerbotAIConfig.botAhSellMaxListings`
   (default 8), return `false`. This counts the bot's rows directly in the core
   `auctionhouse` table, so it's correct across restarts.
3. **Resolve the house** from the *auctioneer's* faction:
   `AuctionHouseMgr::GetAuctionHouseEntryFromFactionTemplate(auctioneer->GetFaction())`
   → `AuctionHouseEntry const* ahEntry` (return `false` if null), and
   `sAuctionMgr->GetAuctionsMap(auctioneer->GetFaction())`
   → `AuctionHouseObject* auctionHouse` (return `false` if null). Using the
   auctioneer's faction makes Alliance/Horde/neutral routing automatic.
4. **Collect spares** — a lambda `consider(Item* item)` appends to
   `std::vector<Item*> spares` (capped at 3). An item is skipped if
   `!item`, `item->IsSoulBound()`, or `spares.size() >= 3`. It is *sellable* if:
   - `proto->Class == ITEM_CLASS_ARMOR || proto->Class == ITEM_CLASS_WEAPON`
     with `proto->Quality >= ITEM_QUALITY_UNCOMMON` (greens and up — no gray
     vendor-trash), **or**
   - `proto->Class == ITEM_CLASS_TRADE_GOODS` with
     `proto->Quality >= ITEM_QUALITY_NORMAL` (ore/herb/leather/cloth).

   And it must have `proto->SellPrice > 0`. `consider` is invoked over the
   backpack slots (`INVENTORY_SLOT_ITEM_START`..`INVENTORY_SLOT_ITEM_END` via
   `bot->GetItemByPos(INVENTORY_SLOT_BAG_0, i)`) and every equipped bag
   (`INVENTORY_SLOT_BAG_START`..`INVENTORY_SLOT_BAG_END` via
   `bot->GetBagByPos(b)` → `bag->GetItemByPos(s)`). If `spares` is empty, return
   `false`.
5. **Price factor** — `float priceFactor = AhPriceFactorFor(bot)` (one query for
   the whole pass, reused for all up-to-3 items).
6. **Per-item pricing** (all `uint32`, copper):
   - `base = std::max<uint32>(proto->SellPrice, 25) * 3 * item->GetCount()` —
     3× vendor price, at least 25c per unit, scaled by stack count.
   - `bid = std::max<uint32>(uint32(base * priceFactor) * urand(80, 120) / 100, 100)` —
     apply personality multiplier, ±20% jitter, floor of 100c (1 silver).
   - `buyout = bid * 13 / 10` — +30%.
7. **Construct `AuctionEntry`** (heap `new`, ownership passed to the auction
   house) and set every field:
   - `Id = sObjectMgr->GenerateAuctionID()`
   - `houseId = AuctionHouseId(ahEntry->houseId)`
   - `item_guid = item->GetGUID()`
   - `item_template = item->GetEntry()`
   - `itemCount = item->GetCount()`
   - `owner = bot->GetGUID()`
   - `startbid = bid`
   - `bidder = ObjectGuid::Empty`
   - `bid = 0`
   - `buyout = buyout`
   - `expire_time = GameTime::GetGameTime().count() + uint32(12 * HOUR * sWorld->getRate(RATE_AUCTION_TIME))`
   - `deposit = 0` (bots skip the deposit gold-sink)
   - `auctionHouseEntry = ahEntry`
8. **Register + move + persist** per item:
   - `sAuctionMgr->AddAItem(item)` — registers the item pointer in the auction
     manager's live item map (so `LoadFromDB`/lookups find its count & template).
   - `auctionHouse->AddAuction(auction)` — inserts into the in-memory
     `AuctionHouseObject`.
   - `bot->MoveItemFromInventory(item->GetBagSlot(), item->GetSlot(), true)` —
     removes the item from the bot's live inventory.
   - One `CharacterDatabaseTransaction trans`:
     `item->DeleteFromInventoryDB(trans)`, `item->SaveToDB(trans)`,
     `auction->SaveToDB(trans)`, `bot->SaveInventoryAndGoldToDB(trans)`,
     then `CharacterDatabase.CommitTransaction(trans)`. Ordering mirrors the
     core's own auction-create path so the listing survives a restart.
   - `++listed`, then `LOG_DEBUG("playerbots", "[BotAH] {} listed {} x{} (bid {} buyout {} copper)", ...)`.
9. If `listed > 0`, `LOG_INFO("playerbots", "[BotAH] {} listed {} item(s) at the auction house", ...)`.
   Return `listed > 0`.

Inputs: bot + world state. Outputs: `bool`; side effects: DB writes (item +
auction + inventory), in-memory auction-house mutation, item removal from bags,
log lines.

## 4. Data structures & DB

**In-memory / core types used:**
- `AuctionEntry` (`AuctionHouseMgr.h:96`) — the listing row. Fields set by the
  action: `Id, houseId, item_guid, item_template, itemCount, owner, startbid,
  bid, buyout, expire_time, bidder, deposit, auctionHouseEntry`.
- `AuctionHouseEntry const*` — the DBC house descriptor (`ahEntry->houseId`).
- `AuctionHouseObject*` — the in-memory per-faction auction collection
  (`AddAuction`).
- `sAuctionMgr` (`AuctionHouseMgr` singleton) — `GetAuctionsMap`, `AddAItem`.
- `sObjectMgr->GenerateAuctionID()` — unique auction id allocator.
- `factors` — `static std::unordered_map<std::string, float> const` inside
  `AhPriceFactorFor` (personality → multiplier).
- `spares` — `std::vector<Item*>` (≤3) built by the `consider` lambda.

**DB tables/columns:**
- `auctionhouse` (acore_characters) — **read**: `COUNT(*) ... WHERE itemowner = ?`
  (the listing cap); **written** by `AuctionEntry::SaveToDB` via prepared
  statement `CHAR_INS_AUCTION`:
  `INSERT INTO auctionhouse (id, houseid, itemguid, itemowner, buyoutprice, time, buyguid, lastbid, startbid, deposit)`.
  Note: `item_template` and `itemCount` are **not** columns here — on reload the
  core recovers them from `item_instance` (via `AddAItem`'s registered item).
  So those two `AuctionEntry` fields matter only for the in-memory session; the
  persisted row is item-guid-based.
- `mod_ollama_chat_personality` (acore_characters, owned by mod-ollama-chat) —
  **read** only: `SELECT personality ... WHERE guid = ?` (guid *counter*).
- `item_instance` / character inventory tables — **written** indirectly via
  `item->DeleteFromInventoryDB`, `item->SaveToDB`, `bot->SaveInventoryAndGoldToDB`.

## 5. Concurrency & threading

Everything in this subsystem runs on the **world (main) thread**. The
`NewRpgStrategy` tick, `AhSellSparesTrigger::IsActive`, and
`AhSellSparesAction::Execute` are all driven by the per-bot AI update inside the
world loop. There is no detached worker thread here (unlike
`OllamaChat_SpeakSituation`, which offloads the LLM call).

Consequences:
- The two `CharacterDatabase.Query` calls (`AhPriceFactorFor`, the listing-cap
  `COUNT(*)`) are **synchronous/blocking** on the world thread. This violates
  the general "reads go through the async path" guidance, and is tolerated only
  because these queries run at most once per listing pass, which is rare (gated
  by proximity + a ≤25% roll on a 30 s interval).
- Item mutation (`MoveItemFromInventory`) and auction-house insertion
  (`AddAItem`, `AddAuction`) touch shared game state, but being on the world
  thread means no locks are needed — there is no concurrent mutator.
- The multi-write persistence is wrapped in a single
  `CharacterDatabaseTransaction`, so the item removal + auction insert commit
  atomically. The transaction append/commit is thread-safe via the DB layer.
- No mutexes or caches are introduced by this feature. The only cache it *reads*
  is the `"nearest npcs"` AI value (populated by the normal AI value pipeline on
  the same thread). The `factors` map is `static const`, immutable, read-only —
  safe to share.

## 6. Config keys

Both are `AiPlayerbot.*` options read in `PlayerbotAIConfig.cpp` and stored on
`sPlayerbotAIConfig`:

| key | member | default | line | meaning |
|---|---|---|---|---|
| `AiPlayerbot.BotAhSellChance` | `float botAhSellChance` | `25.0f` | `PlayerbotAIConfig.cpp:723` | percent chance per 30 s check that a bot near an auctioneer lists spares; `<= 0` disables the feature entirely |
| `AiPlayerbot.BotAhSellMaxListings` | `uint32 botAhSellMaxListings` | `8` | `PlayerbotAIConfig.cpp:724` | max concurrent `auctionhouse` rows per bot before the action no-ops |

Read with `sConfigMgr->GetOption<float>(...)` / `GetOption<uint32>(...)`. Both
keys ship in `conf/playerbots.conf.dist` — `AiPlayerbot.BotAhSellChance = 25.0`
(line 2392) and `AiPlayerbot.BotAhSellMaxListings = 8` (line 2393) — so an
operator finds them already present; the shipped values equal the code defaults,
so runtime behavior is unchanged if they are left untouched. Config loads at
startup → restart worldserver to change them.

Also relevant (not owned by this subsystem, but required for it to run):
`AiPlayerbot.EnableNewRpgStrategy` must be `1` — the trigger only exists inside
`NewRpgStrategy`. And the core `Rate.Auction.Time` (`RATE_AUCTION_TIME`) scales
the 12 h expiry.

## 7. Failure modes & gotchas

- **Feature silently off** if `EnableNewRpgStrategy = 0` — the trigger node is
  never added, regardless of `BotAhSellChance`. `BotAhSellChance <= 0` also
  disables it at the trigger's first line.
- **Graceful degradation to vanilla**: `AhPriceFactorFor` returns `1.0f` when
  the `mod_ollama_chat_personality` table/row is absent — a database without the
  chat module still lists items, just at neutral prices. There is no
  `information_schema` probe here (unlike the pending-gives path in
  BOT-ECONOMY Section 2); the query simply returns no rows and the `find` misses.
- **Personality lookup is a raw blocking query** on the world thread (see Section 5) —
  fine at current frequency, but a hazard if this action were ever made to fire
  per-tick or in a loop.
- **Grouped/in-combat bots never list** (trigger early-out). Bots also stop at
  the `botAhSellMaxListings` cap, so the AH cannot be flooded by one bot.
- **≤3 items per pass** — the `consider` lambda caps `spares` at 3 even if the
  bags hold more; a bot with a full inventory of greens dribbles them out across
  many passes rather than dumping.
- **Deposit is always 0** — bots skip the auction deposit gold-sink; do not use
  bot listings to reason about the deposit economy.
- **No exclusion of parked pending-give items**: a bot can auction the very item
  it promised a stranger for an in-person trade (BOT-ECONOMY Section 2). That is
  intentional — the trade-window "item vanished" apology path
  (`HandlePendingGearGive` at `TRADE_STATUS_BEGIN_TRADE`) covers it.
- **Auctioneer probed twice** (trigger + action). If the bot walks out of range
  between the two, `Execute` returns `false` cleanly at step 1.
- **Item pointers within the pass**: the loop dereferences each `Item*` and
  moves it out of inventory inside the same iteration before touching the next,
  so there's no stale-pointer window across ticks (all within one synchronous
  `Execute`). Do not refactor this to defer the move past the tick without
  switching to `ObjectGuid` re-resolution.
- **`item_template`/`itemCount` not persisted** to `auctionhouse` (see Section 4) — a
  debugging query joining `auctionhouse` to `item_instance` is the correct way
  to see what a bot actually listed; reading those `AuctionEntry` fields from
  the DB is impossible because the columns don't exist.
- **Coexistence with `mod-ah-bot`**: both can be installed; they write ordinary
  `auctionhouse`/`AuctionEntry` rows and never coordinate. Bot listings are
  distinguishable by `itemowner` being a real bot character guid (join to
  `characters`), whereas mod-ah-bot uses its dedicated seller character.

**Verification (from BOT-ECONOMY Section 7):**
```bash
grep "\[BotAH\]" server/bin/Playerbots.log
#   [BotAH] Botname listed 2 item(s) at the auction house          (INFO)
#   [BotAH] Botname listed <item> x1 (bid 450 buyout 585 copper)   (DEBUG)
```
```sql
-- per-bot active listings (cap check: nobody should exceed BotAhSellMaxListings)
SELECT itemowner, COUNT(*) FROM acore_characters.auctionhouse
 GROUP BY itemowner ORDER BY 2 DESC LIMIT 10;
```

## 8. Cross-references

- [`../BOT-ECONOMY.md`](../BOT-ECONOMY.md) — behavior-level framing (Section 3 organic
  AH; Section 1–2 gear gives & in-person trade; Section 4 `OllamaChat_SpeakSituation`).
- [`../BOT-BEHAVIOR.md`](../BOT-BEHAVIOR.md) — Section 2 personalities (the keys this
  subsystem prices on), Section 3 playstyles (the socializer `WanderNpc` weight that
  parks bots at auctioneers), Section 5 gear-inspect context, Section 9 verification cookbook.
- Core auction subsystem: `src/server/game/AuctionHouse/AuctionHouseMgr.{h,cpp}`
  (`AuctionEntry::SaveToDB`, `AddAItem`, `GetAuctionsMap`,
  `GetAuctionHouseEntryFromFactionTemplate`) and the `CHAR_INS_AUCTION`
  prepared statement in
  `src/server/database/Database/Implementation/CharacterDatabase.cpp`.
