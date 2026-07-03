# Bot Economy Systems — Deep Dive

How bots put real items into real players' hands: gear gifts and COD sales
over mail, in-person trade hand-overs, and organic auction house listings —
with every scripted interaction voiced by the LLM instead of canned strings.
This is the economy layer on top of the social systems in
[BOT-BEHAVIOR](BOT-BEHAVIOR.md) (personalities Section 2, playstyles Section 3, sentiment
Section 4, the gear-inspect context Section 5 — none of that is repeated here). The
[README](../README.md) is the quick operating reference; [BUILD-NOTES](BUILD-NOTES.md)
is the chronological journal.

The design invariant across all of it: **a bot never *says* it's handing over
an item unless the transfer has already really happened (or is really
parked)**. The item moves first; the words are generated after, from a prompt
that states what was done.

Contents:
1. [Real gear gives (mail path)](#1-real-gear-gives-mail-path)
2. [In-person trade hand-over](#2-in-person-trade-hand-over)
3. [Organic bot auction house](#3-organic-bot-auction-house)
4. [LLM situational dialogue: OllamaChat_SpeakSituation](#4-llm-situational-dialogue-ollamachat_speaksituation)
5. [How it compiles](#5-how-it-compiles)
6. [Config keys](#6-config-keys)
7. [Verification & log lines](#7-verification--log-lines)

---

## 1. Real gear gives (mail path)

**What**: the gear-inspect context (BOT-BEHAVIOR Section 5) stops being
informational. When a bot notices a weak slot *and* carries a genuine
upgrade, its personality rolls `gear_give_chance` — and on success the item
is **actually transferred** before the bot ever opens its mouth. Generous
personalities gift it; seller personalities send it COD.

**Code path** (all in
`mod-ollama-chat/src/mod-ollama-chat_handler.cpp` unless noted), from a
player chatting near a bot:

1. `PlayerBotChatHandler::OnPlayerCanUseChat` → `ProcessChat()` picks the
   responding bots, then — synchronously, on the world thread, *before* the
   async LLM query thread is spawned — calls `GenerateBotPrompt()`, which
   fills the `{gear_context}` placeholder via `GenerateGearContext(bot, player)`.
2. `GenerateGearContext()` scans the player's 9 armor slots for the weakest
   piece and the bot's own bags for a tradeable upgrade (`offer`) — the scan
   and recognition tiers are documented in BOT-BEHAVIOR Section 5; the well-geared
   tiers return early, so **recognized players are never give candidates**.
3. If an `offer` exists, the bot's personality template supplies
   `gearGiveChance` (`g_PersonalityTemplates`, default `2.0` if the key is
   missing). The gate is one expression:
   `giveChance > 0.0f && CanGiveGearNow(bot, player) && roll_chance_f(giveChance)`.
4. **Cooldowns** — `CanGiveGearNow()` / `RegisterGearGive()` keep two
   mutex-guarded in-memory maps keyed on raw GUIDs:
   - `g_lastGiveByBot`: one give per bot per `g_GearGiveBotCooldownMin`
     (default 30 min) — a bot can't strip its bags into the mailbox.
   - `g_lastGiveByPair`: one give per bot→player pair per
     `g_GearGivePairCooldownMin` (default 1440 min = 24 h) — the same player
     can't farm the same bot.

   Timestamps are `getMSTime()` (server-uptime ms), so **cooldowns reset on
   worldserver restart** — accepted, since 30 min/24 h windows across a
   restart are not worth a table.
5. **COD or gift** — `IsGearSeller(personality)` is a hardcoded set:
   `TRADER`, `GOBLIN_MERCHANT`, `GOLD_FARMER`, `LOOTGOBLIN`, `BANK_ALT`.
   Sellers charge `cod = max(SellPrice * 4, 500)` copper (never below
   5 silver); everyone else sends `cod = 0`.
6. **Relationship gate** — mail is only used when there's a relationship:
   `mailEligible = same guild || currently grouped`
   (`bot->GetGuildId() == player->GetGuildId()` or
   `bot->GetGroup()->IsMember(player->GetGUID())`). A stranger promising you
   mail would be weird in 2008; strangers get the item **in person** instead —
   a *pending give* parked in `mod_ollama_chat_pending_gives`, completed via
   the trade window (Section 2).
7. **Mail branch** — `MailBagItemTo(bot, player, offer, cod)` mirrors the
   core's `HandleSendMail` item transfer inside one
   `CharacterDatabaseTransaction`:
   - `item->SetNotRefundable(bot)`; `bot->MoveItemFromInventory(...)`
     (removes it from the bot's live inventory);
   - `item->DeleteFromInventoryDB(trans)`; force `ITEM_CHANGED` state;
     `item->SetOwnerGUID(player->GetGUID())`; `item->SaveToDB(trans)`;
   - `MailDraft draft(itemName, cod ? "Pay the man." : "From one adventurer
     to another.")` + `AddItem` + `AddCOD(cod)` +
     `SendMailTo(..., MAIL_CHECK_MASK_COPIED)`;
   - `bot->SaveInventoryAndGoldToDB(trans)`; commit.
8. **Only then** does the prompt say so. `RegisterGearGive()` stamps the
   cooldowns and the `{gear_context}` string gets one of:
   - *"You just mailed them your `<item>` with a `<cod/100>` silver COD -
     tell them to check their mailbox and pay up."*
   - *"You just mailed them your `<item>` as a gift - tell them to check
     their mailbox."*
   - (pending-give branch) *"You have a `<item>` for them - tell them to open
     trade with you ..."*

   If the roll **fails**, nothing about the item is appended — the comment in
   the code is literal: *"roll failed: say nothing about the item - no empty
   promises."* The model cannot mention an item it isn't handing over,
   because the only way item talk enters the prompt is after the transfer has
   been committed (or parked). The inverse edge exists though: the mail is
   sent even if the subsequent LLM call errors out — the player just gets a
   pleasant mailbox surprise with no chat line.

**Tuning generosity**: the `gear_give_chance` column (percent per
gear-context, i.e. per reply where an upgrade was found). Column default 2.0;
the module migration tunes the upstream 33 and `personalities.sql` tunes ours
— full table in Section 6. Extremes: WOW_MOM 15 (mom *will* mail you a chestpiece),
ELITE_ARENA_PVPER and PVP_TRASHTALKER 0 (get good, get your own).

## 2. In-person trade hand-over

**What**: the stranger path. The bot said "open trade with me"; when the
player actually does, mod-playerbots completes the hand-over through the real
trade window — including holding out for payment on COD gives.

**Table**: `acore_characters.mod_ollama_chat_pending_gives`
(created by `2026_07_03_personality_gear_give.sql`)

| column | meaning |
|---|---|
| `bot_guid` | bot's guid *counter* (PK part) |
| `player_guid` | player's guid *counter* (PK part) |
| `item_guid` | parked item's guid counter — still in the bot's bags |
| `cod` | copper the player must put in the trade window (0 = gift) |
| `created_at` | auto timestamp; rows older than 10 minutes are ignored |

The PK is `(bot_guid, player_guid)`, and the write is a `REPLACE INTO`, so a
pair has at most one pending give and a new one overwrites a stale one. The
item itself never leaves the bot's inventory at park time — parking is just
this row.

**Mechanism** (`mod-playerbots/src/Ai/Base/Actions/TradeStatusAction.cpp`):
`TradeStatusAction` (registered as the `"accept trade"` action) executes on
every `SMSG_TRADE_STATUS` the bot receives. At the top of `Execute()`:

- `PendingGiveItemGuid(bot, trader, &cod)` — after a one-time cached
  `information_schema` probe for the table (graceful degrade when the chat
  module's migration is absent), selects
  `item_guid, cod ... WHERE bot_guid = ... AND player_guid = ... AND
  created_at > NOW() - INTERVAL 10 MINUTE`.
- If the trader is a **real player** (`!traderBotAI`) and a fresh row exists,
  control jumps to `HandlePendingGearGive()` — *before* the usual
  master/group gating and before the `enableRandomBotTrading` check, so a
  stranger can complete this one trade even on a realm where random-bot
  trading is otherwise locked down.

**The state machine** (`HandlePendingGearGive`), driven by the `status` field
of each trade packet; the item is re-resolved from the row every time via
`bot->GetItemByGuid(ObjectGuid::Create<HighGuid::Item>(itemGuidLow))`:

| status | bot's move |
|---|---|
| `TRADE_STATUS_BEGIN_TRADE` | Item still in bags? Face the trader, `HandleBeginTradeOpcode` — accept the trade session. **Item vanished** (equipped, auctioned Section 3, mailed since parking)? `ClearPendingGive` + whispered LLM apology (*"you promised them a piece of gear but no longer have it"*) + `HandleCancelTradeOpcode`. |
| `TRADE_STATUS_OPEN_WINDOW` | `myTrade->SetItem(TradeSlots(0), item)` — the item appears in trade slot 0. If COD, whisper the price via the LLM (*"you put your `<item>` in the trade window; it costs them `<cod/100>` silver, ask them to add the money"*). Then `HandleAcceptTradeOpcode` — the bot pre-accepts. |
| `TRADE_STATUS_TRADE_ACCEPT` / `TRADE_STATUS_BACK_TO_TRADE` | **The payment hold**: if `cod` and the player's trade money `< cod`, return *without* accepting — the trade sits until they add gold. The core clears accept-states whenever trade contents change, which re-fires this action; once the money covers the COD the bot re-accepts. Gifts accept immediately. |
| `TRADE_STATUS_TRADE_COMPLETE` | `ClearPendingGive` (row deleted) + LLM whisper (*"you just sold them a piece of gear in a trade, they paid up"* / *"...gave them a piece of gear for free"*) + `LOG_INFO("playerbots", "[GearGive] {} handed item over to {} in trade (cod {} copper)")`. |

**Expiry**: the 10-minute window is enforced only at read time
(`created_at > NOW() - INTERVAL 10 MINUTE`); expired rows aren't garbage-
collected, they're simply invisible and eventually overwritten by the next
`REPLACE` for that pair (which, given the 24 h pair cooldown, is at least a
day away). If the player never shows up, the bot keeps the item — nothing is
lost, the promise just lapses.

## 3. Organic bot auction house

**What**: bots wander into town (WanderNpc — the socializer playstyle loves
this, BOT-BEHAVIOR Section 3), end up near an auctioneer, and list a few of their
**real spare drops** on their faction's auction house under their own names,
at prices their personality believes in. This is a second, organic supply
stream — **separate from and coexisting with `mod-ah-bot`** (also installed),
which injects synthetic volume from a dedicated seller character. These
listings are genuine `AuctionEntry` rows: players can browse, bid, buy out,
and get outbid mails exactly as with a human seller, and expiry/returns are
handled by the core `AuctionHouseMgr` like any other auction.

**Trigger** (`AhSellSparesTrigger::IsActive()`,
`mod-playerbots/src/Ai/World/Rpg/Trigger/NewRpgTrigger.cpp`; wired into
`NewRpgStrategy` at relevance 12.0, 30 s check interval — so it needs
`AiPlayerbot.EnableNewRpgStrategy = 1`):

- bail if `botAhSellChance <= 0`, bot is grouped, or in combat;
- `AhSellSparesAction::FindNearbyAuctioneer()` must find one: it walks the
  `"nearest npcs"` value and returns the first
  `bot->GetNPCIfCanInteractWith(guid, UNIT_NPC_FLAG_AUCTIONEER)` — real
  interaction-range and hostility checks, no teleporting items to a distant
  AH;
- then a single `roll_chance_f(sPlayerbotAIConfig.botAhSellChance)`
  (default 25% per 30 s check while standing near an auctioneer).

**Action** (`AhSellSparesAction::Execute()`,
`mod-playerbots/src/Ai/World/Rpg/Action/RpgSubActions.cpp`):

1. **Listing cap** — `SELECT COUNT(*) FROM auctionhouse WHERE itemowner =
   <bot counter>`; at or above `botAhSellMaxListings` (default 8), do
   nothing. Bots don't flood the AH.
2. Resolve the house from the *auctioneer's* faction
   (`AuctionHouseMgr::GetAuctionHouseEntryFromFactionTemplate` +
   `sAuctionMgr->GetAuctionsMap`) — Alliance/Horde/neutral AH comes out
   correct for free.
3. **Item filter** — walk backpack + bags collecting at most **3** spares per
   execution (`consider` lambda): not soulbound, `SellPrice > 0`, and either
   - armor/weapon of quality ≥ `ITEM_QUALITY_UNCOMMON` (greens and up — no
     vendor-trash gray swords), or
   - `ITEM_CLASS_TRADE_GOODS` of quality ≥ `ITEM_QUALITY_NORMAL` (ore, herbs,
     leather, cloth — the actual 2008 AH economy).
4. **Pricing** — `base = max(SellPrice, 25) * 3 * count` (3× vendor is the
   classic AH rule of thumb), then
   `bid = max(base * priceFactor * urand(80,120)/100, 100)` (±20% jitter,
   1 silver floor) and `buyout = bid * 13 / 10` (+30%).
5. **Personality pricing** — `AhPriceFactorFor(bot)` reads the bot's
   personality from `mod_ollama_chat_personality` (shared characters DB —
   same loose-coupling pattern as playstyles) and multiplies:

   | overpricers | × | underpricers | × |
   |---|---|---|---|
   | ELITE_ARENA_PVPER | 2.2 | FOOL | 0.4 |
   | MIN_MAXER | 1.8 | SCARED_NEWBIE, ONE_WORD_GRUNTER, STONER | 0.5 |
   | GOBLIN_MERCHANT | 1.7 | TWELVE_YEAR_OLD | 0.55 |
   | TRADER | 1.6 | WOW_MOM, HUMBLE_FARMER, SILENT_TYPE | 0.7 |
   | GOLD_FARMER | 1.5 | CHILL_DAD | 0.75 |
   | THEORYCRAFTER | 1.4 | | |
   | HARDCORE_RAIDLEAD, FAIRWEATHER_FRIEND | 1.3 | everyone else | 1.0 |

   The elitist lists a green BoE at arena-champion prices; the fool
   practically gives mithril away. Sniping underpriced bot auctions is an
   intended gameplay loop.
6. **Construction & persistence** — per item, a real `AuctionEntry`:
   `Id = sObjectMgr->GenerateAuctionID()`, `houseId`, `item_guid`,
   `item_template = item->GetEntry()`, `itemCount`, `owner = bot->GetGUID()`,
   `startbid = bid`, `buyout`, `bidder = ObjectGuid::Empty`, `bid = 0`,
   `expire_time = now + 12h * RATE_AUCTION_TIME`, `deposit = 0` (bots skip
   the deposit gold-sink), `auctionHouseEntry`. Then
   `sAuctionMgr->AddAItem(item)`, `auctionHouse->AddAuction(auction)`,
   `bot->MoveItemFromInventory(...)`, and one transaction:
   `item->DeleteFromInventoryDB` + `item->SaveToDB` + `auction->SaveToDB` +
   `bot->SaveInventoryAndGoldToDB`, committed — survives a restart like any
   player auction.

**Interaction with Section 2**: a parked pending-give item is *not* excluded from
the AH filter — a bot can auction the very item it promised a stranger. The
trade-window "item vanished" apology path is exactly what covers this.

## 4. LLM situational dialogue: OllamaChat_SpeakSituation

**What**: gameplay code in mod-playerbots can make a bot say or whisper one
short, in-character, LLM-generated line about a concrete situation — so
"that'll be 15 silver" comes out as *"15 silver, chop chop, mama didn't raise
a charity"* from a GOBLIN_MERCHANT and *"oh sweetie just take it... okay fine
15 silver"* never happens, because the words are generated from the same
personality prompt as all other chat instead of a hardcoded string table.

**The cross-module mechanism**: the function is *defined* in mod-ollama-chat
(`mod-ollama-chat_handler.cpp`, external linkage, deliberately not declared
in any shared header):

```cpp
void OllamaChat_SpeakSituation(Player* bot, Player* target,
                               std::string const& situation, bool whisper)
```

Consumers in mod-playerbots (`TradeStatusAction.cpp`,
`InviteToGroupAction.cpp`) re-declare it weakly and wrap it:

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

Both modules are static-linked into the one worldserver binary (Section 5), so when
mod-ollama-chat is compiled in, the linker binds the weak reference to the
strong definition and the calls go through. Build with the chat module
disabled (`-DDISABLED_AC_MODULES="mod-ollama-chat"`) and the weak symbol
resolves to null — the `if` makes every call a silent no-op and playerbots
compiles, links, and runs untouched. No headers shared, no link dependency
declared, degrades to vanilla. (This is GCC/Clang ELF behavior; the
`[[gnu::weak]]` attribute is not portable to MSVC — fine for this Linux-only
deployment.)

**Inside the function** (fire-and-forget, never blocks the caller):

1. Guards: `g_Enable` (module master switch) and a live `PlayerbotAI`.
2. Builds a one-shot prompt: bot name/level/class + the full personality
   prompt (*"MAKE SURE YOU RESPOND USING YOUR PERSONALITY..."*) + the
   caller's `situation` string + *"Say one short in-character line about it,
   under 15 words. No narration, no quotes, just the line."*
3. `GetPersonalityQueryOptions(bot)` — per-personality `num_predict` /
   `temperature` overrides apply here too (ONE_WORD_GRUNTER stays terse even
   when selling you a belt).
4. Captures **raw GUIDs, not pointers**, into a detached `std::thread`:
   `SubmitQuery(prompt, opts)` (the module's queue-managed Ollama call) →
   `future.get()` → reacquire `Player*`s via `ObjectAccessor::FindPlayer` →
   `bot->Whisper(response, LANG_UNIVERSAL, target)` when `whisper` and the
   target is still online, else `botAI->Say(response)`. All exceptions
   swallowed. Same async pattern as the module's normal chat replies —
   the world tick never waits on Ollama.

**Current call sites**:

| caller | situation string (gist) | mode |
|---|---|---|
| `HandlePendingGearGive` BEGIN_TRADE | promised gear but no longer have it | whisper |
| `HandlePendingGearGive` OPEN_WINDOW | item's in the window, costs N silver, ask for the money | whisper |
| `HandlePendingGearGive` TRADE_COMPLETE | sold it / gave it for free | whisper |
| `OfferQuestHelpAction::Execute` | both on quest '<title>' — group up? (tier-dependent, see BOT-BEHAVIOR Section 6) | say |

Adding a new one from anywhere in mod-playerbots is the two declarations
above plus one call — no build-system changes.

## 5. How it compiles

**One binary**: the tree builds with `-DMODULES=static -DSCRIPTS=static`
(see BUILD-NOTES), so mod-playerbots and mod-ollama-chat are compiled as
static libraries and linked **into worldserver itself**. That single final
link is what makes the `[[gnu::weak]]` cross-module call in Section 4 work: both the
weak reference and the strong definition are visible to the same linker
invocation, no dlopen/plugin machinery involved. It's also why a cross-module
signature change is dangerous — see the hazard note below.

**SQL migrations auto-apply**: each module ships migrations under
`modules/<mod>/data/sql/<database>/base/`. With `Updates.AutoSetup = 1` in
`worldserver.conf` (I run 1), the DB updater applies anything new at
worldserver startup and records it in the `updates` table of that database,
one-shot per file. So `2026_07_03_personality_gear_give.sql` (in
mod-ollama-chat's `data/sql/characters/base/`) added the `gear_give_chance`
column and the `mod_ollama_chat_pending_gives` table on the first boot after
the rebuild — no manual SQL. (`personalities.sql` at the repo root is *not* a
module migration; it's applied by hand / reset-world.sh, per BOT-BEHAVIOR Section 2.)
Both C++ sides also probe `information_schema` at runtime before trusting the
schema, so an older database still runs a newer binary with defaults.

**Rebuild**:

```bash
cd azerothcore-wotlk/build && make -j8 install   # incremental
# then restart worldserver
```

Incremental is fine for .cpp edits. Note that
`mod-ollama-chat_config.h` is included by nearly every translation unit in
the chat module — the `BotPersonalityTemplate` struct change (adding
`gearGiveChance`) recompiled most of mod-ollama-chat in one go, and any
future field added there will too. Same story for
`PlayerbotAIConfig.h` on the playerbots side (the two `botAhSell*` members):
expect a long make after touching either header.

## 6. Config keys

None of these four keys ship in the `.conf.dist` files yet — they're read
with `sConfigMgr->GetOption` defaults, so to change one, add the line to
`server/etc/modules/mod_ollama_chat.conf` / `playerbots.conf` yourself.
Config loads at startup → **restart worldserver after changes**.

| key | default | read in | meaning |
|---|---|---|---|
| `OllamaChat.GearGiveBotCooldownMin` | 30 | `LoadOllamaChatConfig()`, mod-ollama-chat_config.cpp | minutes between *any* two gives by one bot |
| `OllamaChat.GearGivePairCooldownMin` | 1440 | same | minutes before the same bot gives to the same player again |
| `AiPlayerbot.BotAhSellChance` | 25.0 | `PlayerbotAIConfig.cpp` | % per 30 s check that a bot near an auctioneer lists spares (0 = feature off) |
| `AiPlayerbot.BotAhSellMaxListings` | 8 | same | max concurrent auctions per bot |

**`gear_give_chance` column** (percent per gear-context; column default
**2.0** for every key not listed):

| chance | personalities |
|---|---|
| 15 | WOW_MOM |
| 12 | TRADER, GOBLIN_MERCHANT (COD) · CHILL_DAD, HUMBLE_FARMER |
| 10 | GOLD_FARMER (COD), HEALER_MAIN |
| 8 | MENTOR, JOLLY_BEER_LOVER, HEROIC_LEADER · EGIRL, BANK_ALT (COD), STOIC_PALADIN |
| 6 | LOOTGOBLIN (COD), CASUAL |
| 5 | SCARED_NEWBIE, GUILD_RECRUITER, WANDERING_RP |
| 1 | GRUMPY_VETERAN, EDGE_LORD, WANNABE_VILLAIN, RAGER, PARANOID, TRICKSTER · MIN_MAXER, UNHINGED_TROLL, HARDCORE_RAIDLEAD, FAIRWEATHER_FRIEND, SPEEDRUNNER |
| 0 | ELITE_ARENA_PVPER, PVP_TRASHTALKER |

(Upstream-33 rows tuned by the module migration
`2026_07_03_personality_gear_give.sql`; our 42 by `personalities.sql`. "COD"
marks `IsGearSeller` personalities — they charge for the mail/trade.)
Templates hot-reload with console `ollama reload`; the cooldown keys do not.

## 7. Verification & log lines

**Is it alive?**

```bash
# gear gives: mail + park events (INFO, server.loading → Server.log)
grep "\[GearGive\]" server/bin/Server.log
#   [Ollama Chat] [GearGive] Botname (WOW_MOM) mailed <item> to Player (COD 0 copper)
#   [Ollama Chat] [GearGive] Botname (TRADER) parked <item> for in-person trade with Player (cod 600 copper)

# trade-window completions (INFO, playerbots → Playerbots.log)
grep "\[GearGive\]" server/bin/Playerbots.log
#   [GearGive] Botname handed item over to Player in trade (cod 600 copper)

# AH listings: per-bot summary is INFO, per-item detail is DEBUG
grep "\[BotAH\]" server/bin/Playerbots.log
#   [BotAH] Botname listed 2 item(s) at the auction house          (INFO)
#   [BotAH] Botname listed <item> x1 (bid 450 buyout 585 copper)   (DEBUG)

# situational LLM lines ride the normal query path - watch with
# OllamaChat.DebugEnabled = 1 like any other prompt (BOT-BEHAVIOR Section 9)
```

**Pending gives** (parked in-person hand-overs; rows > 10 min old are dead
but linger until overwritten):

```sql
SELECT pg.bot_guid, pg.player_guid, pg.item_guid, pg.cod, pg.created_at,
       pg.created_at > NOW() - INTERVAL 10 MINUTE AS still_live
  FROM acore_characters.mod_ollama_chat_pending_gives pg;
```

**A bot's active auctions** (they're ordinary `auctionhouse` rows;
`itemowner` is the guid counter):

```sql
SELECT c.name AS bot, it.itemEntry, ah.startbid, ah.buyoutprice,
       FROM_UNIXTIME(ah.time) AS expires
  FROM acore_characters.auctionhouse ah
  JOIN acore_characters.characters c   ON c.guid = ah.itemowner
  JOIN acore_characters.item_instance it ON it.guid = ah.itemguid
 WHERE c.name = 'Botname';

-- who's flooding the AH (cap check: nobody should exceed BotAhSellMaxListings)
SELECT itemowner, COUNT(*) FROM acore_characters.auctionhouse
 GROUP BY itemowner ORDER BY 2 DESC LIMIT 10;
```

**Mailed gives** land in the regular mail tables:

```sql
SELECT id, sender, receiver, subject, cod, has_items
  FROM acore_characters.mail ORDER BY id DESC LIMIT 10;
```

**Gotchas**:
- All `*_guid` columns here are guid *counters*, same convention as the
  sentiment tables (BOT-BEHAVIOR Section 9).
- Gear-give cooldowns are in-memory (`getMSTime`) — a worldserver restart
  clears them; don't be surprised by two gives 5 minutes apart across a
  restart.
- The AH trigger only exists inside `NewRpgStrategy` — with
  `AiPlayerbot.EnableNewRpgStrategy = 0` bots never list anything, whatever
  `BotAhSellChance` says.
- A bot can auction or equip an item it parked for a pending give; the
  player then gets the whispered apology at trade time, not the item.
- `[GearGive]` appears in **two logs**: the mail/park half is the chat module
  (Server.log), the trade half is playerbots (Playerbots.log).
