# Internals 13 — Guild-scoped bot memory

Source of truth:
`modules/mod-ollama-chat/src/mod-ollama-chat_memory.cpp`,
`modules/mod-ollama-chat/src/mod-ollama-chat_memory.h`,
recorder call-sites in `modules/mod-ollama-chat/src/mod-ollama-chat_events.cpp`,
recall consumer in `modules/mod-ollama-chat/src/mod-ollama-chat_handler.cpp`,
schema in `data/sql/characters/base/2026_07_03_event_memories.sql`.

---

## 1. Purpose

A random bot keeps a small, bounded set of **structured** memories about a
**real, currently-guilded** player — the meaningful shared moments (duel, group
up, become guildmates, share an arena/battleground) — and later references them
in chat through the `{memory_info}` prompt placeholder. There are **no
record-time LLM calls**: memories are plain rows (`event_type`, `detail`,
`zone`), and the recall text is assembled cheaply by string-joining the newest
rows. Storage stays bounded two ways — a per-`(bot, player)` row cap and a full
purge of a player's rows when they leave a guild — so no row compression is
needed; the bounding *is* the performance strategy (see the header comment in
`mod-ollama-chat_memory.h`).

This is the deeper, function-level companion to the behavior-level framing in
[BOT-BEHAVIOR.md](../BOT-BEHAVIOR.md) Section 4 (sentiment) and Section 7 (arena teams); the
driving "recall the arena" example lives there.

---

## 2. Entry points & call graph

The subsystem has **three write triggers** and **one read trigger**, all
entered from AzerothCore script hooks on the world thread. Nothing here spawns
its own thread.

**Write path A — arena/battleground end** (`mod-ollama-chat_memory.cpp`):

```
BGScript::OnBattlegroundEnd(bg, winnerTeamId)          [OllamaChatMemoryBGScript]
  ├─ split bg->GetPlayers() by GetBgTeamId() → realPlayers[team], bots[team]
  └─ for each team, for each bot, for each real teammate:
       RecordMemory(bot, player, "arena"|"battleground", detail)
         ├─ ResolveBotZoneName(bot) → PlayerbotAI::GetCurrentZone / GetLocalizedAreaName
         ├─ CharacterDatabase.EscapeString(type/detail/zone)
         ├─ CharacterDatabase.Execute(INSERT …)
         └─ CharacterDatabase.Execute(DELETE … prune to g_EventMemoryMaxPerPair)
```

**Write path B — duel/group/guild, piggybacked next to sentiment**
(`mod-ollama-chat_events.cpp`):

```
ChatOnDuel::OnPlayerDuelEnd(winner, loser, type)       [PlayerScript]
  ├─ NudgeSentimentPair(winner, loser, -g_SentimentEventDuelAdjustment)
  └─ RecordMemoryForPair(winner, loser, "duel", "dueled them")   (UNGATED by sentiment)

SentimentOnGroup::OnAddMember(group, guid)             [GroupScript]
  └─ if (g_EnableSentimentTracking && g_SentimentEventGroupAdjustment != 0.0f):
       for each existing member: NudgeSentimentPair(...) ; RecordMemoryForPair(newMember, member, "group", "grouped up with them")

ChatOnGuildMemberChange::OnGuildMemberJoin(player, guild)   [PlayerScript]
  └─ if (g_EnableSentimentTracking && g_SentimentEventGuildAdjustment != 0.0f):
       for each online guildmate: NudgeSentimentPair(...) ; RecordMemoryForPair(player, member, "guild", "became guildmates with them")

RecordMemoryForPair(a, b, type, detail)
  ├─ detect which side is a bot (GetPlayerbotAI)
  └─ RecordMemory(bot, realPlayer, type, detail)   (re-validates random-bot + guilded)
```

**Purge path — guild leave** (`mod-ollama-chat_memory.cpp`):

```
GuildScript::OnRemoveMember(guild, player, isDisbanding, isKicked)  [OllamaChatMemoryGuildScript]
  └─ PurgePlayerMemories(player GUID) → DELETE … WHERE player_guid = ?
```

**Read path — prompt assembly** (`mod-ollama-chat_handler.cpp`, world thread,
inside `GenerateBotPrompt` before the async LLM query thread is spawned):

```
GenerateBotPrompt(...)
  └─ BuildMemoryInfo(bot, player)   [handler.cpp:2211]
       └─ CharacterDatabase.Query(SELECT detail, zone … ORDER BY id DESC LIMIT 4)
  → fmt::arg("memory_info", memoryInfo)   [handler.cpp:2266] into g_ChatPromptTemplate
```

Registration: `AddSC_mod_ollama_chat_memory()` (bottom of `memory.cpp`) news up
both scripts and is called from `Addmod_ollama_chatScripts()` in
`mod-ollama-chat_main.cpp`.

---

## 3. Function-by-function

### `static std::string ResolveBotZoneName(Player* bot)`
`mod-ollama-chat_memory.cpp:32`

```cpp
static std::string ResolveBotZoneName(Player* bot)
```

- Fetches `PlayerbotsMgr::instance().GetPlayerbotAI(bot)`; if present, calls
  `ai->GetCurrentZone()` and returns `PlayerbotAI::GetLocalizedAreaName(zone)`.
- Returns `""` if the bot has no `PlayerbotAI` or no resolvable zone.
- Only ever called on a random bot (the rememberer), where the AI is available.
- Output feeds the `zone` column; an empty result is valid and handled
  downstream (recall omits the "in {zone}" clause).

### `void RecordMemory(Player* bot, Player* player, std::string const& eventType, std::string const& detail)`
`mod-ollama-chat_memory.cpp:42`

```cpp
void RecordMemory(Player* bot, Player* player, std::string const& eventType, std::string const& detail)
```

The single write primitive. Step by step:

1. **Master gate**: no-op unless `g_Enable && g_EnableEventMemory`, and both
   `bot` and `player` are non-null and distinct (`bot == player` bails).
2. **Guilded-players-only gate** — the storage boundary: `if
   (player->GetGuildId() == 0) return;`. Memories are never kept about a player
   who isn't in a guild.
3. **Rememberer / remembered gates**:
   - the rememberer must be a random bot:
     `!PlayerbotsMgr::instance().GetPlayerbotAI(bot) ||
     !sRandomPlayerbotMgr.IsRandomBot(bot)` → return. (A bot without AI, or a
     non-random bot, cannot remember.)
   - the remembered side must be real:
     `if (PlayerbotsMgr::instance().GetPlayerbotAI(player)) return;`. Bots do
     not keep memories about other bots.
4. Reads the raw 64-bit GUIDs once: `botGuid = bot->GetGUID().GetRawValue()`,
   `playerGuid = player->GetGUID().GetRawValue()`. No `Player*` is retained past
   this call.
5. **Escapes** the three free-text fields with
   `CharacterDatabase.EscapeString(...)`: `safeType` (=`eventType`),
   `safeDetail` (=`detail`), and `safeZone` (=`ResolveBotZoneName(bot)`).
6. **INSERT** via `CharacterDatabase.Execute(SafeFormat(...))` into
   `mod_ollama_chat_event_memories (bot_guid, player_guid, event_type, detail,
   zone)`. Numeric GUIDs go in as `{}`; escaped strings are quoted `'{}'`.
7. **Prune** — only when `g_EventMemoryMaxPerPair > 0`: a `DELETE` that keeps
   the newest `g_EventMemoryMaxPerPair` rows for this exact `(bot_guid,
   player_guid)` pair and deletes the rest, ordering by `id DESC` (see
   Section 4/Section 7 for the nested-derived-table form). `id` is `AUTO_INCREMENT`, so it
   orders by insert time.
8. If `g_DebugEnabled`, logs `"[OllamaChat] Recorded memory: bot {} remembers {}
   - {} ({}) in {}"`.

Inputs: rememberer bot, remembered player, a short structured `eventType`
(e.g. `"arena"`), a short human `detail` phrase. Output: none. Side effects: one
async INSERT + one async prune on `CharacterDatabase`.

### `void RecordMemoryForPair(Player* a, Player* b, std::string const& eventType, std::string const& detail)`
`mod-ollama-chat_memory.cpp:94`

```cpp
void RecordMemoryForPair(Player* a, Player* b, std::string const& eventType, std::string const& detail)
```

Symmetric convenience wrapper meant to be dropped in beside a
`NudgeSentimentPair()` call where the two participants are an unordered pair.

- No-op unless `g_EnableEventMemory`, both non-null, `a != b`. (Note: it checks
  `g_EnableEventMemory` but **not** `g_Enable`; the master `g_Enable` gate is
  re-enforced inside `RecordMemory`.)
- Computes `aIsBot`/`bIsBot` via `GetPlayerbotAI(...) != nullptr`.
- Records **at most one direction**:
  - `aIsBot && !bIsBot` → `RecordMemory(a, b, ...)`
  - `bIsBot && !aIsBot` → `RecordMemory(b, a, ...)`
  - both bots, or neither a bot → no call at all.
- `RecordMemory` then re-validates *random*-bot + guilded + non-bot-player, so a
  non-random bot detected here is still filtered out downstream.

### `std::string BuildMemoryInfo(Player* bot, Player* player)`
`mod-ollama-chat_memory.cpp:110`

```cpp
std::string BuildMemoryInfo(Player* bot, Player* player)
```

The read/recall primitive that produces the `{memory_info}` fragment.

1. Returns `""` unless `g_Enable && g_EnableEventMemory` and both pointers are
   non-null. (It does **not** re-check guilded/random-bot — recall trusts what
   was recorded.)
2. Reads both raw GUIDs.
3. **Synchronous** `CharacterDatabase.Query`: `SELECT detail, zone FROM
   mod_ollama_chat_event_memories WHERE bot_guid = {} AND player_guid = {} ORDER
   BY id DESC LIMIT 4`. Note the recall cap of **4** is independent of (and
   smaller than) the storage cap `g_EventMemoryMaxPerPair` (default 8).
4. If `!result`, return `""`.
5. Iterates rows (`Fetch()` → `fields[0]` detail, `fields[1]` zone). Skips any
   row whose `detail` is empty. Builds each part as `"{det} in {zn}"` when the
   zone is non-empty, else just `det`.
6. If no parts survived, return `""`.
7. Joins parts with `"; "` and returns:
   `"You recently: {joined}. Reference this shared history with {player_name}
   naturally if it fits."`

Output is consumed at `handler.cpp:2211` and injected as
`fmt::arg("memory_info", memoryInfo)` into `g_ChatPromptTemplate`
(`handler.cpp:2266`). If a template omits `{memory_info}`, the fragment is
simply unused.

### `void PurgePlayerMemories(uint64_t playerGuid)`
`mod-ollama-chat_memory.cpp:158`

```cpp
void PurgePlayerMemories(uint64_t playerGuid)
```

- Unconditionally executes `DELETE FROM mod_ollama_chat_event_memories WHERE
  player_guid = {}`. **No config gate inside** — the caller gates it.
- Deletes rows for that player across **every** bot (keyed on `player_guid`
  only), so when a real player leaves a guild, all bots forget them at once.
- The bot side is never a purge key because bots are never the remembered side.

### `class OllamaChatMemoryGuildScript : public GuildScript`
`mod-ollama-chat_memory.cpp:168`

```cpp
void OnRemoveMember(Guild* /*guild*/, Player* player, bool /*isDisbanding*/, bool /*isKicked*/) override
```

- Fires on any guild-member removal (voluntary leave, kick, or disband).
- Guards `if (!g_EnableEventMemory || !player) return;`, then calls
  `PurgePlayerMemories(player->GetGUID().GetRawValue())`.
- This is what keeps the table bounded to *currently*-guilded players.

### `class OllamaChatMemoryBGScript : public BGScript`
`mod-ollama-chat_memory.cpp:188`

```cpp
void OnBattlegroundEnd(Battleground* bg, TeamId /*winnerTeamId*/) override
```

The "recall the arena" recorder. On battleground/arena end:

1. Guards `if (!g_Enable || !g_EnableEventMemory || !bg) return;`.
2. `bool isArena = bg->isArena();` chooses `eventType` = `"arena"` /
   `"battleground"` and `detail` = `"were on an arena team with them"` /
   `"fought alongside them in a battleground"`.
3. Splits the roster into two `std::array<std::vector<Player*>,
   PVP_TEAMS_COUNT>` — `realPlayers` and `bots` — indexed by team. For each
   `bg->GetPlayers()` entry: skip null or `!IsInWorld()`; read `p->GetBgTeamId()`
   and skip anything not `TEAM_ALLIANCE`/`TEAM_HORDE`; if it has **no**
   `PlayerbotAI` it goes in `realPlayers[team]`; if it **has** a `PlayerbotAI` it
   goes in `bots[team]` only when `sRandomPlayerbotMgr.IsRandomBot(p)` — an
   AI-bearing but non-random bot lands in **neither** bucket (silently dropped),
   not in `realPlayers`.
4. For each team, nested loop over `bots[team] × realPlayers[team]` calling
   `RecordMemory(bot, player, eventType, detail)`.

Only **same-team** pairings are remembered (opponents are not); the
`RecordMemory` guilded/random-bot re-checks still apply per pair.

### `void AddSC_mod_ollama_chat_memory()`
`mod-ollama-chat_memory.cpp:240`

```cpp
void AddSC_mod_ollama_chat_memory()
```

News up `OllamaChatMemoryGuildScript` and `OllamaChatMemoryBGScript` (standard
AzerothCore self-registering `ScriptObject` pattern). Called from
`Addmod_ollama_chatScripts()` in `mod-ollama-chat_main.cpp` (forward-declared
line 33, invoked line 34).

### Recorder call-sites in `mod-ollama-chat_events.cpp`

- **Duel** — `ChatOnDuel::OnPlayerDuelEnd` (line 612): after
  `NudgeSentimentPair(winner, loser, -g_SentimentEventDuelAdjustment)` it calls
  `RecordMemoryForPair(winner, loser, "duel", "dueled them")`. This call is
  **not** wrapped in a sentiment-enabled check — duel memory records whenever
  memory itself is enabled.
- **Group** — `SentimentOnGroup::OnAddMember` (line 743): the whole block is
  guarded by `if (!group || !g_EnableSentimentTracking ||
  g_SentimentEventGroupAdjustment == 0.0f) return;`, so
  `RecordMemoryForPair(newMember, member, "group", "grouped up with them")` only
  runs when sentiment tracking is on and the group adjustment is non-zero.
- **Guild** — `ChatOnGuildMemberChange::OnGuildMemberJoin` (line 674): the
  memory call `RecordMemoryForPair(player, member, "guild", "became guildmates
  with them")` sits inside `if (g_EnableSentimentTracking &&
  g_SentimentEventGuildAdjustment != 0.0f)`, iterating online members with the
  same `GetGuildId()`.

So **group and guild memory recording are coupled to sentiment being enabled**
(and its adjustment being non-zero); duel and arena/BG recording are not.

---

## 4. Data structures & DB

**Table** `mod_ollama_chat_event_memories` (schema
`data/sql/characters/base/2026_07_03_event_memories.sql`, DB
`acore_characters`):

| column | type | notes |
|---|---|---|
| `id` | `BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY` | insert-order surrogate; used as the recency key for recall and prune |
| `bot_guid` | `BIGINT UNSIGNED NOT NULL` | raw GUID of the rememberer (random bot) |
| `player_guid` | `BIGINT UNSIGNED NOT NULL` | raw GUID of the remembered real player |
| `event_type` | `VARCHAR(24) NOT NULL` | structured tag: `duel`, `group`, `guild`, `arena`, `battleground` (schema comment also lists `trade`, see Section 7) |
| `detail` | `VARCHAR(128) NOT NULL DEFAULT ''` | short human phrase used verbatim in recall |
| `zone` | `VARCHAR(48) NOT NULL DEFAULT ''` | bot's zone at record time; may be empty |
| `created_at` | `TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP` | populated by the DB default; **never read or written by code** |
| index | `KEY idx_bot_player (bot_guid, player_guid)` | primary lookup path for both recall and prune |

Charset `utf8mb4 / utf8mb4_unicode_ci`, `ENGINE=InnoDB`.

**Columns read by code**: `detail`, `zone` (recall `SELECT`); `id` (prune/recall
ordering). **Columns written**: `bot_guid`, `player_guid`, `event_type`,
`detail`, `zone`. `created_at` and `id` come from column defaults /
auto-increment.

**In-memory structures**: none persistent in this subsystem — no caches, no
maps, no globals beyond the two config values. The only transient containers are
`BuildMemoryInfo`'s `std::vector<std::string> parts` and the BG script's two
`std::array<std::vector<Player*>, PVP_TEAMS_COUNT>` roster buckets, both stack
locals.

**Prune query** (exact form) — the nested derived table `keep` is the MySQL
workaround for "can't use `LIMIT` directly inside `IN (SELECT …)`":

```sql
DELETE FROM mod_ollama_chat_event_memories
WHERE bot_guid = {} AND player_guid = {} AND id NOT IN
  (SELECT id FROM (SELECT id FROM mod_ollama_chat_event_memories
   WHERE bot_guid = {} AND player_guid = {} ORDER BY id DESC LIMIT {}) AS keep)
```

---

## 5. Concurrency & threading

Everything in this subsystem runs on the **world thread**:

- `OnBattlegroundEnd`, `OnRemoveMember`, `OnPlayerDuelEnd`, `OnAddMember`,
  `OnGuildMemberJoin` are all synchronous script hooks invoked by the core on
  the world update thread.
- `BuildMemoryInfo` is called from `GenerateBotPrompt` **before** the detached
  LLM query thread is spawned (the async worker described in
  [BOT-ECONOMY.md](../BOT-ECONOMY.md) Section 4 handles only the HTTP call, not memory
  reads), so its `CharacterDatabase.Query` runs on the world thread.

Because there are no shared mutable structures here, there are **no mutexes**.
Thread-safety comes entirely from the database layer:

- Writes (`RecordMemory` INSERT + prune, `PurgePlayerMemories`) use
  `CharacterDatabase.Execute`, which **enqueues** the statement on the async
  DB worker pool and returns immediately (fire-and-forget). The INSERT and its
  following prune are enqueued back-to-back on the same connection queue, so the
  prune is guaranteed to observe the just-inserted row.
- The recall read uses `CharacterDatabase.Query`, a **synchronous** blocking
  query. It is a single indexed lookup capped at 4 rows, so the world-thread
  stall is negligible.

Note the read/write ordering is eventually-consistent, not transactional: a
`RecordMemory` enqueued this tick may not yet be visible to a `BuildMemoryInfo`
that runs a moment later, since the write is async. This is acceptable — recall
is best-effort flavor text.

Unlike the event-chatter path in the same events file (`QueueEvent` spawns a
`std::thread(...).detach()` and **reacquires** the bot by GUID via
`ObjectAccessor::FindPlayer` after every await/`sleep_for`), the memory
functions never cross a thread boundary and never retain a `Player*`, so they
have no reacquire-by-GUID logic of their own — they snapshot the raw GUIDs up
front and touch nothing live afterward.

---

## 6. Config keys

Read in `mod-ollama-chat_config.cpp` (`LoadOllamaChatConfig`), stored in the
`g_*` globals in `mod-ollama-chat_config.h`:

| Key | Global | Default | Effect |
|---|---|---|---|
| `OllamaChat.EnableEventMemory` | `g_EnableEventMemory` | `true` | Master toggle for the whole memory subsystem (record + recall + purge gate). |
| `OllamaChat.EventMemoryMaxPerPair` | `g_EventMemoryMaxPerPair` | `8` | Per-`(bot, player)` row cap enforced on each insert. **`0` disables pruning** (unbounded per pair). |

Also gating this subsystem (owned elsewhere, referenced here):

| Key | Global | Default | Relevance |
|---|---|---|---|
| `OllamaChat.Enable` | `g_Enable` | `true` | Module master switch; re-checked in `RecordMemory`, `BuildMemoryInfo`, and the BG script. |
| `OllamaChat.DebugEnabled` | `g_DebugEnabled` | `false` | Enables the `"Recorded memory: …"` info log. |

Sentiment keys that **couple** group/guild recording (see Section 3): the record calls
only fire when `g_EnableSentimentTracking` is true and, respectively,
`g_SentimentEventGroupAdjustment` / `g_SentimentEventGuildAdjustment` are
non-zero. `g_SentimentEventDuelAdjustment` sizes the duel sentiment drop but
does **not** gate the duel memory.

---

## 7. Failure modes & gotchas

- **`g_EventMemoryMaxPerPair == 0` means no cap**, not "keep zero". The prune is
  wrapped in `if (g_EventMemoryMaxPerPair > 0)`, so a value of 0 leaves the pair
  unbounded (guild-leave purge is then the only bound). This is easy to misread.
- **Recall cap (4) < storage cap (8)**. `BuildMemoryInfo` hard-codes `LIMIT 4`
  while the table keeps up to `g_EventMemoryMaxPerPair` rows. The 4 oldest of a
  full pair are stored but never surfaced until newer rows are pruned away.
- **Group/guild memory silently depends on sentiment.** If an operator disables
  `OllamaChat.EnableSentimentTracking` (or zeroes the group/guild adjustment)
  but leaves `EnableEventMemory` on, group-up and guildmate memories stop being
  recorded even though the subsystem "is enabled." Duel and arena/BG keep
  working. This is the most likely "why aren't memories appearing?" trap.
- **`event_type = 'trade'` is documented but unreached.** The schema comment and
  the header list `trade` as a tag, but neither `memory.cpp` nor `events.cpp`
  records one — no trade hook calls `RecordMemory`/`RecordMemoryForPair`. It is
  a reserved tag, not a live code path.
- **`created_at` is decorative.** Recency everywhere is derived from
  `AUTO_INCREMENT id`, never `created_at`. Do not assume the timestamp is used
  for ordering or expiry — there is no time-based expiry at all.
- **Raw-query construction, not `PreparedStatement`.** Against the repo-wide
  convention (CLAUDE.md), this module builds SQL via `SafeFormat`
  (`fmt::vformat` with a try/catch that returns `"[Format Error]"` on a format
  exception). Injection safety instead comes from `CharacterDatabase.EscapeString`
  on `event_type`/`detail`/`zone`, layered with fmt passing user text as `{}`
  *arguments* (so literal braces in a detail/zone are data, not format tokens).
  A developer adding a new recorder must keep escaping every free-text field.
- **Graceful no-ops via null/identity checks.** Missing `PlayerbotAI`, a
  non-random bot, a bot-vs-bot pair, a non-guilded player, or `a == b` all
  short-circuit to a silent no-op rather than erroring. `ResolveBotZoneName`
  degrading to `""` is normal and handled by omitting the "in {zone}" clause.
- **Purge is player-wide.** `PurgePlayerMemories` deletes by `player_guid`
  across all bots, so guild-hopping wipes a player's entire memory footprint,
  not just memories tied to guildmates. Re-joining a guild starts the player's
  memory history from scratch.
- **BG recorder cost scales with roster.** `OnBattlegroundEnd` does an
  INSERT+prune per `(bot, real teammate)` pair per team; a large battleground
  full of random bots can enqueue many async writes at once. They are async, so
  the world thread only pays the enqueue cost, but it is worth knowing when
  profiling match-end spikes.
- **No `information_schema` probe / weak-symbol shim on this side.** The
  graceful-degradation-if-tables-absent machinery described in
  [BOT-BEHAVIOR.md](../BOT-BEHAVIOR.md) Section 1 lives on the *mod-playerbots* reader
  side. Within mod-ollama-chat, the `mod_ollama_chat_event_memories` table is
  assumed present (created by its own base migration); if the migration hasn't
  run, `RecordMemory`/`BuildMemoryInfo` will log DB errors rather than degrade.

---

## 8. Cross-references

- [BOT-BEHAVIOR.md](../BOT-BEHAVIOR.md) — Section 4 sentiment (the system whose
  `NudgeSentimentPair` calls these recorders piggyback on), Section 7 arena teams and
  the "recall the arena" example, Section 1 cross-module degradation model.
- [BOT-ECONOMY.md](../BOT-ECONOMY.md) — Section 4 `OllamaChat_SpeakSituation` and the
  world-thread-vs-detached-worker prompt/LLM threading model referenced in Section 5.
- Sibling internals docs in this directory, by subsystem source file:
  - sentiment — `mod-ollama-chat_sentiment.cpp` (`NudgeSentimentPair`,
    `GetSentimentPromptAddition`).
  - event chatter — `mod-ollama-chat_events.cpp`
    (`OllamaBotEventChatter::DispatchGameEvent` / `QueueEvent` / `BuildPrompt`),
    which shares the file with the duel/group/guild recorder call-sites.
  - prompt assembly — `mod-ollama-chat_handler.cpp` `GenerateBotPrompt`, the
    consumer that injects `{memory_info}` into `g_ChatPromptTemplate`.
- Schema: `data/sql/characters/base/2026_07_03_event_memories.sql`.
