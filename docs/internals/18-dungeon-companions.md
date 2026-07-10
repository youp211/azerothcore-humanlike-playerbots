# Internals: Dungeon companions — autofill & experience-based persistence

*Subsystem of `mod-playerbots`. Source of truth:
`modules/mod-playerbots/src/Bot/Factory/DungeonCompanions.{cpp,h}`, wired from
`src/Script/Playerbots.cpp`, with one guard in `src/Bot/RandomPlayerbotMgr.cpp`
and two sentiment bridges in `mod-ollama-chat`'s
`src/mod-ollama-chat_handler.cpp`. This is the developer-level companion to
[`../BOT-BEHAVIOR.md`](../BOT-BEHAVIOR.md) Section 14 ("Dungeon companions").*

---

## 1. Purpose

Two behaviors that share one file:

1. **Autofill** — a real player who queues the dungeon finder should never wait
   on the vanilla matchmaker. A world-thread tick keeps their queue fed with
   free, role/level-appropriate random bots (a tank, a healer, then DPS) so the
   core LFG matcher forms the group promptly.
2. **Experience-based persistence** — the dungeon bots a player leaves a *strong*
   impression on become permanent world residents; the forgettable ones don't. A
   great run saves the bot as a **friend**, a genuinely bad one as a **troll**;
   a neutral run saves nothing and the bot stays a throwaway random-pool filler.
   "Strong impression" is read straight off the existing sentiment system
   ([05](05-sentiment.md)) after a run-outcome nudge, so this adds no new metric.

The load-bearing design decision (same as [17](17-pvp-respect.md)): **the server
decides and persists; the LLM only narrates.** Classification is pure sentiment
math on the world thread; the one optional spoken line is fire-and-forget.

---

## 2. Entry points & call graph

One world-thread singleton, fed by a `GroupScript` + a `PlayerScript`, driven by
the world-update tick, plus one read-side guard in the random-bot processor.

```
AddPlayerbotsScripts()                                   [Script/Playerbots.cpp]
  └─ AddSC_DungeonCompanions()  → new DungeonCompanionsGroupScript / …PlayerScript

PlayerbotsWorldScript::OnUpdate(diff)                    [Script/Playerbots.cpp]
  └─ DungeonCompanions::instance().Update(diff)           (world thread only)
       ├─ EnsureLoaded()                     — lazy load companion guids from DB
       ├─ latch RunState.entered for live runs (player in a dungeon map)
       ├─ drain _pendingResolve → ResolveRun()
       ├─ every ResolveIntervalSec: ResolveStaleRuns() → ResolveRun()
       └─ every AutofillIntervalSec: AutofillTick()

DungeonCompanionsGroupScript   (GroupScript)
  ├─ OnCreate / OnAddMember / OnRemoveMember → OnPartyChanged(group)
  └─ OnDisband                                → OnPartyDisband(groupGuid)
DungeonCompanionsPlayerScript  (PlayerScript)
  └─ OnPlayerJustDied(player)                 → OnMemberDeath(player)

RandomPlayerbotMgr::ProcessBot(Player*)                 [Bot/RandomPlayerbotMgr.cpp]
  └─ if idle & randomize due:
       if DungeonCompanions::instance().IsCompanion(botId) → skip Randomize(), reschedule
```

Cross-module (same binary, `[[gnu::weak]]`, no-op if `mod-ollama-chat` absent or
sentiment off):

```
DungeonCompanions.cpp                         mod-ollama-chat_handler.cpp
  OllamaChat_GetSentiment(bot, player)   →  live GetBotPlayerSentiment(...)  (−1 sentinel if unavailable)
  OllamaChat_NudgeSentiment(bot,player,d)→  NudgeSentimentPair(...)          (symmetric, clamped)
  OllamaChat_SpeakSituation(...)         →  async in-voice line              (friend greeting)
```

Registration: `AddSC_DungeonCompanions()` (bottom of `DungeonCompanions.cpp`)
`new`s the two scripts; called from `AddPlayerbotsScripts()`. The `.cpp` is a new
file — modules glob at configure time, so a `cmake` reconfigure is needed before
`make` (pattern #2 in [01-architecture](01-architecture.md)); `build.sh` always
reconfigures.

---

## 3. Autofill — `AutofillTick()`

Runs every `AiPlayerbot.DungeonAutofill.IntervalSec` (10s). Walks
`ObjectAccessor::GetPlayers()` and, for each **real** player:

- Acts once per queue via the owner: solo player, or the group **leader**
  (skips non-leaders so a queued party is topped up once).
- Resolves the queue GUID (`group ? group->GetGUID() : player->GetGUID()` —
  LFG state/selection live under the *group* guid when grouped) and requires
  `sLFGMgr->GetState(queueGuid) == LFG_STATE_QUEUED` (actively waiting, not yet
  in a proposal) and `< 5` current members.
- Honors a per-player cooldown (`PlayerCooldownSec`, 20s) via `_lastFill` so the
  queue is topped up gently, not every tick.
- Reads `sLFGMgr->GetSelectedDungeons(queueGuid)`, gathers free candidate random
  bots (`AutofillCandidate`) split by natural role (`BotRoleMask`, a mirror of
  `LfgJoinAction::GetRoles`), and queues up to `5 − have` of them preferring a
  full **tank / heal / DPS** comp, backfilling leftovers as DPS.
- Each chosen bot is queued with `QueueBotForDungeons` — the **identical**
  `CMSG_LFG_JOIN` packet form `LfgJoinAction::JoinLFG` uses (JoinLfg itself isn't
  thread-safe, so a packet is pushed onto the bot's session). The core matcher
  then forms the group.

`AutofillCandidate(bot, player, spread)` gates: online, in-world, not
teleporting, `IsRandomBot`, same team, level ≥ 15, alive, not in combat, no
group, not in a BG/BG-queue, not on an instanceable map, LFG state `NONE`, and
within `LevelSpread` (4) levels of the player so the core accepts the join.
`BotValidDungeons` normalises each selected value (`& 0x00FFFFFF` strips the LFG
type bits) and applies the same level gate `LfgJoinAction` does, so the queued
packet is well-formed.

**Self-healing:** if a tick's queued bots don't happen to form the group, the
next tick re-evaluates and tops up again. Autofill is independent of the chat
module — it works with `mod-ollama-chat` off.

---

## 4. Persistence — run tracking → classification

### 4a. Run registry (`OnPartyChanged` / `OnMemberDeath` / disband)

A **run** = a group holding a real player and ≥ 1 random bot. `OnPartyChanged`
(fired on any membership change) qualifies the group and, on first qualification,
stamps `RunState.startMs` + `playerGuid`; on every call it **accumulates** the
current random-bot members into `RunState.botGuids` (a `std::set`, so a bot that
drops mid-run is still judged at the end). When a group stops qualifying (player
left / all bots gone) the run is moved to `_pendingResolve` and erased — the
heavier resolve happens off-lock on the next tick, never inside the GroupScript
hook. `OnPartyDisband` does the same for a full disband. `OnMemberDeath`
(`OnPlayerJustDied`) increments `RunState.deaths` for the dead member's run.

`RunState.entered` is latched **every world tick** in `Update` (not just on the
30s stale sweep) whenever the owning player is on a dungeon map
(`Map::IsDungeon()`), so a run that ends before a sweep still carries the flag.

### 4b. `ResolveRun(RunState&)` — the judgement

- No-ops unless the run actually **entered** an instance — merely queuing and
  cancelling leaves the bots as throwaway pool bots (no companions).
- Computes the **run-outcome nudge** once: `+CompletionNudge` (0.15) if the run
  lasted ≥ `MinMinutes` (8), minus `DeathNudge` (0.04) per death. This is what
  lets a single memorable run push a bot across a threshold from the 0.5 neutral
  default (0.5 + 0.15 = 0.65 = friend; 0.5 − 0.04·8 = 0.18 ≤ 0.20 = troll).
- For each tracked bot still online, still a random bot, and **not already a
  companion**: apply the nudge (`OllamaChat_NudgeSentiment`), then read the
  resulting live value (`OllamaChat_GetSentiment`). A negative sentinel (chat
  module / sentiment off) → skip (stay ephemeral). Then:
  - `sentiment ≥ GoodThreshold` (0.65) → `SaveCompanion(FRIEND)` + one async
    friendly whisper via `OllamaChat_SpeakSituation`.
  - `sentiment ≤ TrollThreshold` (0.20) → `SaveCompanion(TROLL)` (no line — a
    troll just persists).
  - otherwise **neutral** → nothing saved.

`SaveCompanion` inserts the bot's low guid into the in-RAM `_companions` set
(dedup via `set::insert().second`, so a race is a no-op) and
`INSERT … ON DUPLICATE KEY UPDATE` into
`acore_characters.mod_playerbots_companions` (newest run wins).

### 4c. The persistence effect (`RandomPlayerbotMgr::ProcessBot`)

The random-bot processor re-randomises idle bots (`Randomize` re-rolls level and
gear — it *erases* identity). The single added guard:

```cpp
if (DungeonCompanions::instance().IsCompanion(botId))
{
    ScheduleRandomize(botId, urand(min, max));   // push the timer out, don't re-roll
    return true;
}
Randomize(bot);
```

So a saved companion keeps its name/level/gear and is never re-rolled; it still
logs in/out and wanders like any random bot, so the player runs into the *same*
bot again. Neutral (unsaved) bots hit `Randomize` as normal — instance-only
throwaways. `IsCompanion` is an O(log n) lock-guarded set lookup that lazily
triggers `EnsureLoaded` on first call.

---

## 5. Data structures & DB

### In-memory (`DungeonCompanions.h`, all under `_mutex`)
| Symbol | Type | Meaning |
|---|---|---|
| `_runs` | `std::map<ObjectGuid, RunState>` | group GUID → tracked run |
| `_pendingResolve` | `std::vector<RunState>` | ended runs awaiting off-lock resolve |
| `_companions` | `std::set<ObjectGuid::LowType>` | saved companion low-guids (read by `IsCompanion`) |
| `_lastFill` | `std::map<LowType, uint32>` | player low-guid → last autofill `getMSTime()` |

`RunState` = `{ playerGuid, botGuids(set), startMs, deaths, entered, resolved }`.

### DB table — `acore_characters.mod_playerbots_companions`
Created by `data/sql/characters/base/2026_07_10_dungeon_companions.sql`:

| Column | Type | Written by code? |
|---|---|---|
| `bot_guid` | `INT UNSIGNED PRIMARY KEY` | Yes (low guid) |
| `player_guid` | `INT UNSIGNED` | Yes |
| `disposition` | `TINYINT UNSIGNED` (1 friend, 2 troll) | Yes |
| `sentiment` | `FLOAT` | Yes (`{:.3f}`) |
| `created_at` | `TIMESTAMP DEFAULT CURRENT_TIMESTAMP` | No (MySQL) |

`EnsureLoaded` probes `information_schema` for the table first (idles with a log
line if the migration hasn't applied), then loads all `bot_guid`s into
`_companions`. Same probe-then-read idiom as the cross-module reads in
[09](09-playstyles.md)/[10](10-quest-help.md).

---

## 6. Concurrency & threading

Everything here runs on the **world thread**: the two scripts, `Update`, and the
`RandomPlayerbotMgr` guard all execute on it in this fork (same as the sibling
`PartyGuildFormation` / `SentimentOnGroup` hooks). `_mutex` is defensive and,
critically, is **never held across** `ResolveRun` — the resolve (DB writes, the
weak-symbol nudge/read, the async speak) runs after the lock is released via the
`_pendingResolve` drain and the `toResolve` snapshot in `ResolveStaleRuns`. The
one weak-symbol read/nudge touches `mod-ollama-chat`'s own mutex-guarded map; the
one speak is fire-and-forget on a detached thread that reacquires by GUID.

---

## 7. Config keys

All under `AiPlayerbot.`, read live via `sConfigMgr->GetOption` (same idiom as
`PartyGuildFormation`); documented in `playerbots.conf.dist`.

| Key | Default | Effect |
|---|---|---|
| `DungeonAutofill.Enabled` | 1 | Master switch for autofill |
| `DungeonAutofill.IntervalSec` | 10 | Seconds between autofill passes |
| `DungeonAutofill.PlayerCooldownSec` | 20 | Min seconds before re-topping a player's queue |
| `DungeonAutofill.LevelSpread` | 4 | Max level gap filler-bot ↔ player |
| `DungeonCompanion.Enabled` | 1 | Master switch for persistence |
| `DungeonCompanion.GoodThreshold` | 0.65 | Sentiment ≥ this → friend |
| `DungeonCompanion.TrollThreshold` | 0.20 | Sentiment ≤ this → troll |
| `DungeonCompanion.MinMinutes` | 8 | Run length needed for the completion nudge |
| `DungeonCompanion.CompletionNudge` | 0.15 | Sentiment bump for a clean, long-enough run |
| `DungeonCompanion.DeathNudge` | 0.04 | Sentiment drop per member death |
| `DungeonCompanion.ResolveIntervalSec` | 30 | Seconds between stale-run sweeps |

---

## 8. Gotchas & edge cases

- **`entered` is required to save.** Queue-then-cancel never produces companions;
  the run must have zoned into a dungeon (latched per tick). A run whose whole
  life fits between the last stale sweep and its disband still resolves via the
  `_pendingResolve` path with `entered` already latched.
- **Autofill vs the chat module are independent.** Autofill needs neither
  `mod-ollama-chat` nor sentiment; persistence needs sentiment (a negative
  sentinel → nothing saved).
- **Group-vs-player LFG guid.** State and selected dungeons are queried under the
  *group* guid when grouped — querying the player guid there returns nothing and
  autofill silently does nothing.
- **Companion set is authoritative in RAM.** `IsCompanion` reads `_companions`,
  loaded once at boot and updated on save; a companion added this session is
  effective immediately (no DB round-trip on the hot path).
- **No un-save path yet.** Companions persist until the row is deleted by hand
  (`DELETE FROM mod_playerbots_companions WHERE bot_guid = …`). Disposition is
  recorded but currently only the *presence* of a row gates re-randomisation;
  friend vs troll is informational (hook for future voice/behavior differences).
- **Unbuilt-in-CI caveat.** Like the rest of `patches/`, this was authored
  against source but the realm binary must be rebuilt (`build.sh`) and the
  autofill group-formation verified in-world (it depends on core LFG matching).

---

## 9. Cross-references

- [`../BOT-BEHAVIOR.md`](../BOT-BEHAVIOR.md) Section 14 — behavior-level framing.
- [05-sentiment](05-sentiment.md) — the value this system reads and nudges.
- [14-social-channels](14-social-channels.md) — `OllamaChat_SpeakSituation`.
- [17-pvp-respect](17-pvp-respect.md) — the sibling "server acts, model narrates"
  sentiment consumer.
- [12-guilds](12-guilds.md) / `PartyGuildFormation` — the subsystem this one's
  structure (singleton + GroupScript + PlayerScript + world tick) mirrors.
