# 12 — Personality guilds, recruitment & LLM naming

Developer internals for the "fresh-realm guild experience": how bot guilds are
formed by personality, how their leaders recruit real newbies, and how each
guild gets an LLM-generated name in its leader's voice. Behavior-level framing
lives in [../BOT-BEHAVIOR.md](../BOT-BEHAVIOR.md) Section 12; this doc is the
function-by-function reference for anyone modifying or debugging the code.

Three source files, one subsystem:

| file | module | role |
|---|---|---|
| `mod-playerbots/src/Bot/Factory/PersonalityGuildFactory.cpp` | playerbots | forms the guilds |
| `mod-playerbots/src/Bot/Factory/GuildRecruitmentEvent.cpp` | playerbots | sends leaders out to recruit |
| `mod-ollama-chat/src/mod-ollama-chat_guildnames.cpp` | ollama-chat | async LLM rename |

Supporting code it reuses: `PlayerbotGuildMgr` (`src/Mgr/Guild/PlayerbotGuildMgr.cpp`).

---

## 1. Purpose

On a **fresh realm**, once the entire random-bot population is logged in, this
subsystem forms themed guilds — raid, pvp, casual — whose leaders and elite
members are selected from bots' mod-ollama-chat *personalities* rather than at
random. Each freshly-minted leader then teleports to its race's starting area
and spends ~15 minutes pitching its guild in-voice and popping guild invites on
brand-new real players, before returning to normal bot life. In parallel, each
guild's placeholder themed name is replaced asynchronously by an LLM-generated,
in-character name. The whole thing is a one-shot realm-start event: it runs at
most once per process and degrades to nothing on a long-running server.

---

## 2. Entry points & call graph

There are **three independent entry points**, all reached from the world thread.

### Entry A — guild formation (bot-login hook, once)

`RandomPlayerbotMgr::OnBotLoginInternal` (`src/Bot/RandomPlayerbotMgr.cpp:2534`)
fires as each random bot logs in. When the last bot arrives
(`playerBots.size() == GetMaxAllowedBotCount()`), it flips `_isBotLogging=false`
and calls the factory:

```
RandomPlayerbotMgr::OnBotLoginInternal          (last bot online)
└─ PersonalityGuildFactory::FormPersonalityGuilds()      [once-per-process guard]
   ├─ LoadPersonalities(...)                    → CharacterDatabase: mod_ollama_chat_personality
   ├─ LoadOnlineGuildlessBots(pool, ...)        → PlayerbotsDatabase: playerbots_random_bots
   ├─ formEliteGuild(Raid,  RAID_LEADERS, RAID_FIT)  × RaidCount
   │  ├─ findLeader(RAID_LEADERS, ...)
   │  ├─ MakeThemedName(Raid)                   → sGuildMgr->GetGuildByName / CreateRandomGuildName
   │  ├─ PlayerbotGuildMgr::instance().CreateGuild(leader, name)
   │  ├─ guild->AddMember(...) loop  (fitSet + same team, no randomness)
   │  ├─ PlayerbotGuildMgr::instance().OnGuildUpdate(guild)
   │  ├─ SpeakGuildName(...)   → [weak] OllamaChat_RenameGuildInVoice   (Entry C)
   │  └─ recruitLeaders.emplace_back(leaderGuid, "raid")
   ├─ formEliteGuild(Pvp, PVP_LEADERS, PVP_FIT)      × PvpCount
   ├─ formCasualGuild()                              × CasualCount
   │  └─ (as above, but members admitted via roll_chance_f(CasualJoinChance))
   └─ GuildRecruitmentEvent::instance().Begin(recruitLeaders)           (Entry B setup)
```

### Entry B — recruitment driver (world tick)

`PlayerbotsWorldScript::OnUpdate` (`src/Script/Playerbots.cpp:384`) calls the
singleton every world tick:

```
PlayerbotsWorldScript::OnUpdate(diff)
└─ GuildRecruitmentEvent::instance().Update(diff)   // world thread only
   └─ (every kTickIntervalMs = 5s) Tick(elapsed)
      ├─ per Recruiter: reacquire leader by GUID; drop if offline
      ├─ if window elapsed → Finish(recruiter, true)  (teleport home)
      ├─ SpeakSituation(leader, nullptr, "...pitch...", false)   [weak, say]
      └─ FindNearbyRecruit → SendGuildInvite → SpeakSituation(..., true) [weak, whisper]
```

`GuildRecruitmentEvent::Begin` is *not* a hook — it is called synchronously at
the tail of `FormPersonalityGuilds`; it teleports each leader to a start zone
and seeds `_recruiters`.

### Entry C — LLM rename (worker thread → world tick)

```
OllamaChat_RenameGuildInVoice(leader, guildId, archetype)   [world thread]
├─ build prompt from leader personality/class/level
└─ std::thread([...]{ ... }).detach()                        [worker thread]
   ├─ SubmitQuery(prompt, opts).get()      (blocking Ollama call, off world thread)
   ├─ SanitizeGuildName(...)
   ├─ reject reserved/profanity/taken names
   └─ push PendingGuildRename onto g_GuildNameQueue  (under g_GuildNameQueueMutex)

OllamaChatGuildNameWorldScript::OnUpdate(diff)               [world thread, every tick]
└─ drain g_GuildNameQueue → sGuildMgr->GetGuildById → Guild::SetName(name)
```

---

## 3. Function-by-function

### 3.1 `PersonalityGuildFactory.cpp`

#### `void PersonalityGuildFactory::FormPersonalityGuilds()`

The whole formation pass. Static, called from the bot-login hook.

1. **Once-per-process guard**: `static bool formed`; returns immediately if
   already `true`, else sets it. This is *the* idempotency mechanism — the hook
   can fire it repeatedly and only the first call does work. (Declared in
   `PersonalityGuildFactory.h`; the header comment states "Runs at most once per
   server process (guarded internally)".)
2. Reads six config values (see Section 6): `raidCount`, `pvpCount`, `casualCount`,
   `leaderMinLevel`, `casualJoinChance`, and `maxMembers =
   std::max<uint32>(2, sPlayerbotAIConfig.randomBotGuildSizeMax)` (hard floor of
   2 so a guild is always leader + ≥1 slot).
3. Early-out if `raidCount + pvpCount + casualCount == 0`.
4. `LoadPersonalities(personalities)` then `LoadOnlineGuildlessBots(pool, ...)`.
   If `pool` is empty, logs `[PersonalityGuild] No online guildless bots
   available...` and returns.
5. Declares two accumulators used across the lambdas:
   - `std::unordered_set<uint32> committed` — low-GUIDs already placed (as leader
     or member) **or** found stale/guilded and thus never to be reconsidered.
   - `std::vector<std::pair<ObjectGuid, std::string>> recruitLeaders` — leader
     GUID + archetype name for each formed guild, handed to Entry B at the end.
6. Runs `formEliteGuild(Raid,…)` up to `raidCount` times (breaking on the first
   failure), then `formEliteGuild(Pvp,…)` up to `pvpCount`, then
   `formCasualGuild()` up to `casualCount`. Each loop **stops early** the moment
   a form call returns `false` (no eligible leader left).
7. Logs `[PersonalityGuild] Formation complete: {} raid, {} pvp, {} casual`.
8. Calls `GuildRecruitmentEvent::instance().Begin(recruitLeaders)`.

**Side effects**: creates guilds in the DB (via `CreateGuild`), adds members,
mutates `PlayerbotGuildMgr` cache, fires async LLM renames, seeds the recruitment
event. **Output**: none (void).

#### `void LoadPersonalities(std::unordered_map<uint32, std::string>& out)` (anon ns)

Bulk-loads guid→personality in one query:
`CharacterDatabase.Query("SELECT guid, personality FROM
mod_ollama_chat_personality")`. Empty result → returns silently (map stays
empty; every candidate's `personality` becomes `""` and matches no fit/leader
set → no guilds form). Note this is **not** an information_schema-guarded probe —
if the chat module's table is absent the query fails and returns null, handled
as "no personalities".

#### `void LoadOnlineGuildlessBots(std::vector<Candidate>& out, std::unordered_map<uint32, std::string> const& personalities)` (anon ns)

Builds the working candidate pool. Runs `PLAYERBOTS_SEL_RANDOM_BOTS_BOT`
(`SELECT` bot `FROM playerbots_random_bots WHERE event = ?`) with data `"add"` —
the same random-bot roster query used by
`RandomPlayerbotFactory::CreateRandomArenaTeams`. For each low-GUID row:

- `ObjectGuid::Create<HighGuid::Player>(lowGuid)`;
- `ObjectAccessor::FindConnectedPlayer(guid)` — **skips** if not connected or
  already `GetGuildId()` (guilded);
- fills a `Candidate` with `level = bot->GetLevel()`, `team =
  bot->GetTeamId()`, and `personality` from the map (or `""`).

So the pool is *online, guildless, random* bots only, snapshotted at call time.

#### `std::string MakeThemedName(GuildArchetype archetype)` (anon ns)

Placeholder-name generator (the LLM rename replaces this later). Picks a
`prefix`/`suffix` word-list pair by archetype (static vectors: `raidPrefix`
"Wrath/Nightfall/…" × `raidSuffix` "Legion/Covenant/…", etc.). Up to **20
attempts**: `Acore::StringFormat("{} {}", prefix, suffix)`, skip if `> 24`
chars (WoW guild-name limit) or `sGuildMgr->GetGuildByName(name)` says it's
taken; return the first unique one. If all 20 fail, falls back to
`RandomPlayerbotFactory::CreateRandomGuildName()` (the module's
`playerbots_guild_names` pool).

#### `auto findLeader = (PersonalitySet const& leaderSet, Candidate& outLeader) -> Player*`

Shared leader picker (a lambda capturing `pool`, `committed`, `leaderMinLevel`).
Scans `pool` in order; skips a candidate if it is `committed`, below
`leaderMinLevel`, or its `personality` is not in `leaderSet`. For a surviving
candidate it **reacquires** the live `Player*` via
`ObjectAccessor::FindConnectedPlayer(candidate.guid)`; if that returns null or
the player is now guilded, it inserts the low-GUID into `committed` ("stale/
guilded: never reconsider") and continues. Otherwise copies the candidate into
`outLeader` and returns the `Player*`. Returns `nullptr` when the archetype has
no remaining eligible leader — this is what stops the formation loops early.

#### `auto formEliteGuild = (GuildArchetype archetype, PersonalitySet const& leaderSet, PersonalitySet const& fitSet) -> bool`

Forms one raid or pvp guild. **Fit is required; there is no admission
randomness.**

1. `findLeader(leaderSet, leader)` → `leaderPlayer`, else return `false`.
2. `MakeThemedName(archetype)`; empty → `false`.
3. `PlayerbotGuildMgr::instance().CreateGuild(leaderPlayer, name)`; on failure
   logs `[PersonalityGuild] Failed to create {} guild...` and returns `false`.
4. Insert leader into `committed` (the leader always fits its own guild because
   leader personalities are unioned into the fit sets — see Section 4).
5. Reacquire the `Guild*` via `sGuildMgr->GetGuildByName(name)` and walk `pool`:
   for each candidate, stop at `memberCount >= maxMembers`; skip if `committed`,
   different `team` than the leader, or `personality` not in `fitSet`; reacquire
   the member `Player*`, mark stale/guilded ones `committed` and skip; then
   `guild->AddMember(candidate.guid, uint8(urand(GR_OFFICER, GR_INITIATE)))` —
   on success mark `committed` and `++memberCount`.
6. `PlayerbotGuildMgr::instance().OnGuildUpdate(guild)` to refresh the cache.
7. Log the formed guild; `SpeakGuildName(leaderPlayer, guild->GetId(),
   ArchetypeName(archetype))` (Entry C); `recruitLeaders.emplace_back(
   leaderPlayer->GetGUID(), ArchetypeName(archetype))`. Return `true`.

#### `auto formCasualGuild = () -> bool`

Identical shape to `formEliteGuild` **except**: leader comes from
`CASUAL_LEADERS`, there is **no fit set**, and each same-faction remaining
candidate is admitted only if `roll_chance_f(casualJoinChance)` passes (default
40%). This is where "everyone else may roll in" — the casual guilds mop up the
bots the elite guilds didn't claim. Same `AddMember` rank spread, same
`OnGuildUpdate`, `SpeakGuildName`, and `recruitLeaders` push.

#### `static void SpeakGuildName(Player* leader, uint32 guildId, std::string const& archetype)`

Thin wrapper over the weak symbol:

```cpp
[[gnu::weak]] void OllamaChat_RenameGuildInVoice(Player* leader, uint32 guildId, std::string const& archetype);
static void SpeakGuildName(Player* leader, uint32 guildId, std::string const& archetype)
{
    if (OllamaChat_RenameGuildInVoice)
        OllamaChat_RenameGuildInVoice(leader, guildId, archetype);
}
```

If mod-ollama-chat is compiled out, the weak reference resolves to null and this
is a silent no-op (guilds keep their themed names). See Section 7.

### 3.2 `PlayerbotGuildMgr.cpp` (reused, not owned by this subsystem)

#### `bool PlayerbotGuildMgr::CreateGuild(Player* player, std::string guildName)`

Wraps core `Guild::Create`. Allocates a `new Guild()`, calls
`guild->Create(player, guildName)` (makes `player` the GM); on failure logs and
`delete`s. On success: `sGuildMgr->AddGuild(guild)`, `SetGuildEmblem(guildId)`
(random tabard via a raw `UPDATE guild SET Emblem...`), and inserts a
`GuildCache` entry `{name, memberCount=1, status=1, maxMembers=
randomBotGuildSizeMax, faction=player->GetTeamId()}`. **Note** `hasRealPlayer`
defaults to `false`, so a personality guild is registered as a *bot* guild
(`IsRealGuild` returns false for it), exactly like the upstream random-bot guild
path in `PlayerbotFactory::InitGuild`.

#### `void PlayerbotGuildMgr::OnGuildUpdate(Guild* guild)`

Refreshes the cache entry after membership changes: sets `memberCount =
guild->GetMemberCount()`, `status = 1` (partial) if below `maxMembers` else `2`
(full), and flips the guild's name to "used" in `_guildNames`. No-op if the
guild isn't cached.

### 3.3 `GuildRecruitmentEvent.cpp`

#### `GuildRecruitmentEvent& GuildRecruitmentEvent::instance()`

Meyers singleton (`static GuildRecruitmentEvent instance`). All state
(`_recruiters`, `_tickAccumMs`) lives here.

#### `void GuildRecruitmentEvent::Begin(std::vector<std::pair<ObjectGuid, std::string>> const& leaders)`

Sets up the recruiting window. **Gates** (any one aborts the whole event):

- `AiPlayerbot.GuildRecruitEnabled` false → return;
- `leaders.empty()` → return (no guild formed this process);
- `GameTime::GetUptime().count() >= kRealmStartUptimeSec` (30 min) → log
  `[GuildRecruit] Skipping recruitment - server uptime exceeds 30 min` and
  return. **This is the realm-start gate** — it prevents recruitment on a
  long-running server that merely happened to form guilds late.
- `LoadStartPositions()` empty → log `[GuildRecruit] No playercreateinfo start
  positions found; recruitment disabled` and return.

For each leader it reacquires the `Player*` via
`ObjectAccessor::FindPlayer(guid)` (skip if null), resolves a start via
`ResolveStart` (skip if null), and builds a `Recruiter`:

- `leaderGuid`, `archetype`;
- `guildId = leader->GetGuildId()`, `guildName = leader->GetGuild()->GetName()`
  — **captured now** (see the stale-name gotcha in Section 7);
- return position (`returnMapId/X/Y/Z/O`) = leader's *current* location, so it
  can go home;
- `remainingMs = minutes * MINUTE * IN_MILLISECONDS` (`GuildRecruitMinutes`, 15);
- `nextPitchMs = urand(10, 20) * IN_MILLISECONDS` (first pitch shortly after
  arrival).

Then `leader->TeleportTo(start->mapId, x, y, z, o)` and push onto `_recruiters`.
Finally `_tickAccumMs = 0`.

#### `void GuildRecruitmentEvent::Update(uint32 diff)`

The tick driver. Returns immediately if `_recruiters.empty()` (cheap when idle —
this is called every world tick). Accumulates `diff` into `_tickAccumMs`; once it
reaches `kTickIntervalMs` (5s), snapshots the elapsed value, resets the
accumulator, and calls `Tick(elapsed)`. So heavy work (grid scans, pitches) runs
at ~5s cadence regardless of tick rate.

#### `void GuildRecruitmentEvent::Tick(uint32 elapsed)`

Iterates `_recruiters` by index (erase-in-place safe). Per recruiter:

1. Reacquire `leader = ObjectAccessor::FindPlayer(leaderGuid)`. Null → log
   `[GuildRecruit] Leader ... went offline; ending recruitment`, `erase`, don't
   teleport back (nothing to move), continue.
2. If `remainingMs <= elapsed` → `Finish(recruiter, true)` (teleport home),
   `erase`, continue. Else `remainingMs -= elapsed`.
3. If `leader->IsBeingTeleported() || !leader->IsInWorld()` → `++i` and skip:
   mid cross-map teleport the leader has no valid grid neighbours, so don't pitch
   or scan yet.
4. **Pitch**: if `nextPitchMs <= elapsed`, reset `nextPitchMs = urand(60, 90) *
   IN_MILLISECONDS` and `SpeakSituation(leader, nullptr, "...you're recruiting
   new members for your {archetype} guild '{guildName}'...", false)` (a `say`),
   then debug-log. Else `nextPitchMs -= elapsed`.
5. **Invite**: if `invitesSent < kMaxInvitesPerLeader` (10),
   `FindNearbyRecruit(leader, range, recruiter.invited)`; on a hit,
   `SendGuildInvite(leader, target)`, record `invited.insert(GUID)`,
   `++invitesSent`, info-log `[GuildRecruit] {} invited {} to '{}' (n/10)`, then
   `SpeakSituation(leader, target, "you just sent {} a guild invite - tell them
   what your guild is about, briefly", true)` (a whisper). One invite per tick,
   so invites spread out as newcomers wander in.

#### `void GuildRecruitmentEvent::Finish(Recruiter const& recruiter, bool teleportBack)`

Reacquires the leader; if present and `teleportBack`, `TeleportTo` the stored
return position. Logs `[GuildRecruit] {} finished recruiting for '{}' ({} invites
sent)` (using `recruiter.leaderGuid.ToString()` if the leader is gone).

#### `std::unordered_map<uint8, StartPos> LoadStartPositions()` (anon ns)

One query: `SELECT race, map, position_x, position_y, position_z, orientation
FROM playercreateinfo` (**WorldDatabase**). Keeps the **first row per race**
(classes of a race share a spawn), stamping `team =
Player::TeamIdForRace(race)`.

#### `StartPos const* ResolveStart(std::unordered_map<uint8, StartPos> const& starts, Player* leader)` (anon ns)

Leader's own race first (`starts.find(leader->getRace())`); else the first
same-faction race's start (defensive fallback); else `nullptr`.

#### `void SendGuildInvite(Player* leader, Player* target)` (anon ns)

Queues a real `CMSG_GUILD_INVITE` on the leader's session — payload is just the
invitee name — exactly as the client would send, mirroring the module's own
`GuildInviteAction`:

```cpp
WorldPacket* data = new WorldPacket(CMSG_GUILD_INVITE);
*data << target->GetName();
session->QueuePacket(data);   // takes ownership
```

The session dispatches it to `HandleGuildInviteOpcode` on its next update, so the
full core path runs (faction / already-guilded / already-invited / rank checks)
and the `SMSG_GUILD_INVITE` dialog pops on the target. No-op if `GetSession()`
is null.

#### `Player* FindNearbyRecruit(Player* leader, float range, std::unordered_set<ObjectGuid> const& alreadyInvited)` (anon ns)

Grid scan for the first qualifying real recruit:
`Acore::AnyPlayerInObjectRangeCheck` + `Acore::PlayerListSearcher` +
`Cell::VisitObjects(leader, searcher, range)`. Filters each candidate:

- not the leader, not null;
- **not a bot** — `sPlayerbotsMgr.GetPlayerbotAI(target)` must be null (invite
  REAL players only);
- same `GetTeamId()` as the leader (guild invites are same-faction);
- `GetLevel() < 10` (brand-new players only);
- not `GetGuildId()` and not `GetGuildIdInvited()` (already guilded / already has
  a pending invite);
- not in `alreadyInvited` (one invite per player per leader).

Returns the first match or `nullptr`.

#### `SpeakSituation` wrapper + weak `OllamaChat_SpeakSituation`

Same weak-symbol pattern as `SpeakGuildName`. `OllamaChat_SpeakSituation(Player*
bot, Player* target, std::string const& situation, bool whisper)` is defined in
mod-ollama-chat (`mod-ollama-chat_handler.cpp`); here it's re-declared
`[[gnu::weak]]` and wrapped so the call is a no-op when the chat module is
compiled out. Full behavior of that function is in
[../BOT-ECONOMY.md](../BOT-ECONOMY.md) Section 4.

### 3.4 `mod-ollama-chat_guildnames.cpp`

#### `void OllamaChat_RenameGuildInVoice(Player* leader, uint32 guildId, std::string const& archetype)`

The strong definition of the weak hook (non-static so the extern in
`PersonalityGuildFactory.cpp` binds). **Runs on the world thread**, returns fast:

1. Guards: `!g_Enable` (chat master switch) or null `leader`/`guildId` → return;
   `OllamaChat.EnableGuildNameGen` false → return; no live `PlayerbotAI` for the
   leader → return.
2. Gathers, *on the world thread while the pointer is valid*: `personality =
   GetBotPersonality(leader)`, `personalityPrompt =
   GetPersonalityPromptAddition(personality)`, `botClass` from
   `botAI->GetChatHelper()->FormatClass(leader->getClass())` (fallback
   `"adventurer"`), `leaderName`, and a `fmt::format` prompt asking for a
   "memorable 2008-era WoW guild name, 2 to 4 words, plain text, no quotes".
3. Temperature nudge: `opts = GetPersonalityQueryOptions(leader)`, then
   `opts.temperatureOverride = std::min(baseTemp + 0.15f, 1.5f)` (where
   `baseTemp` is the personality override if ≥0 else `g_OllamaTemperature`) — a
   touch more variety than ordinary chat.
4. Spawns a **detached worker thread** capturing *values only* (`guildId`,
   `prompt`, `opts`, `leaderName` — **no `Player*`**): `SubmitQuery(prompt,
   opts)` → `fut.get()` → `SanitizeGuildName`. Rejects the result if `< 3`
   chars, `sObjectMgr->IsReservedName`, `sObjectMgr->IsProfanityName`, or
   `sGuildMgr->GetGuildByName(name)` (taken → keep the themed name). Otherwise
   locks `g_GuildNameQueueMutex` and pushes `PendingGuildRename{guildId, name,
   leaderName}`. All exceptions swallowed (`catch (...)`).

#### `std::string SanitizeGuildName(std::string s)` (anon ns)

Turns a raw model reply into a legal guild name: keep only the first line (cut at
`\r`/`\n`); strip leading/trailing "edge junk" (whitespace and
`"'.,!?:;`*()[]-_`); collapse internal whitespace runs to single spaces; hard-cap
at **24 chars**, trimming a dangling trailing space after the cut. Title-casing
is intentionally *not* applied.

#### `class OllamaChatGuildNameWorldScript : public WorldScript`

```cpp
void OnUpdate(uint32 /*diff*/) override
```

The world-thread applier, registered as a `WorldScript` so it ticks every world
update. Under `g_GuildNameQueueMutex` it early-returns on an empty queue, else
`swap`s the queue into a local `pending` vector (holding the lock only for the
swap). Then, lock-free, for each entry: `sGuildMgr->GetGuildById(guildId)` (skip
if gone); **re-check** `sGuildMgr->GetGuildByName(name)` (another guild may have
taken it since the worker's check); `guild->SetName(name)` (validates + persists
to the characters DB; skip if it returns false); log `[Ollama Chat] [GuildName]
renamed guild {} -> '{}' (leader {})` under category `server.loading`.

#### `void AddSC_mod_ollama_chat_guildnames()`

Registration entry point (`new OllamaChatGuildNameWorldScript();`), called from
`AddSC_mod_ollama_chat_...` in `mod-ollama-chat_main.cpp`.

---

## 4. Data structures & DB

### In-memory (anon-namespace / header structs)

| type | fields | where |
|---|---|---|
| `enum class GuildArchetype : uint8` | `Raid=0, Pvp, Casual` | PersonalityGuildFactory.cpp |
| `using PersonalitySet = std::unordered_set<std::string>` | — | same |
| `struct Candidate` | `ObjectGuid guid; uint32 lowGuid; uint32 level; uint8 team; std::string personality` | same |
| `struct StartPos` | `uint32 mapId; float x,y,z,o; TeamId team` | GuildRecruitmentEvent.cpp |
| `struct GuildRecruitmentEvent::Recruiter` | `leaderGuid, archetype, guildName, guildId, returnMapId/X/Y/Z/O, remainingMs, nextPitchMs, invitesSent, unordered_set<ObjectGuid> invited` | GuildRecruitmentEvent.h |
| `struct PendingGuildRename` | `uint32 guildId; std::string name; std::string leaderName` | mod-ollama-chat_guildnames.cpp |
| `struct PlayerbotGuildMgr::GuildCache` | `name, status, maxMembers, memberCount, faction, hasRealPlayer` | PlayerbotGuildMgr.h |

**Personality fit-sets** (const `PersonalitySet`, anon ns — these *are* the
selection policy, change them here):

- `RAID_LEADERS` = {HARDCORE_RAIDLEAD, THEORYCRAFTER, MIN_MAXER, LORE_NERD}
- `PVP_LEADERS` = {ELITE_ARENA_PVPER, PVP_TRASHTALKER, DUELIST, RAGER}
- `CASUAL_LEADERS` = {WOW_MOM, MENTOR, GUILD_RECRUITER, CHILL_DAD, JOLLY_BEER_LOVER, HEROIC_LEADER}
- `RAID_FIT` = {RAIDER, THEORYCRAFTER, MIN_MAXER, HARDCORE_RAIDLEAD, HEALER_MAIN, SPEEDRUNNER, ACHIEVEMENT_HUNTER, LORE_NERD}
- `PVP_FIT` = {PVP_HARDCORE, PVP_TRASHTALKER, DUELIST, ELITE_ARENA_PVPER, RAGER, EDGE_LORD}

Note the raid/pvp **leader** personalities are all also present in the
corresponding **fit** set (THEORYCRAFTER/MIN_MAXER/HARDCORE_RAIDLEAD/LORE_NERD in
RAID_FIT; PVP_TRASHTALKER/DUELIST/ELITE_ARENA_PVPER/RAGER in PVP_FIT) — this is
the "leader always fits its own guild" property the code comment relies on.
Casual guilds have no fit set, so casual leaders need no such union.

### Module globals (mod-ollama-chat_guildnames.cpp)

- `std::mutex g_GuildNameQueueMutex`
- `std::vector<PendingGuildRename> g_GuildNameQueue`

### Databases touched

| DB | table / columns | access | by |
|---|---|---|---|
| `acore_characters` | `mod_ollama_chat_personality` (`guid`, `personality`) | read | `LoadPersonalities` |
| `acore_playerbots` | `playerbots_random_bots` (`bot`, `event`) via `PLAYERBOTS_SEL_RANDOM_BOTS_BOT`, param `"add"` | read | `LoadOnlineGuildlessBots` |
| `acore_world` | `playercreateinfo` (`race, map, position_x, position_y, position_z, orientation`) | read | `LoadStartPositions` |
| `acore_characters` | `guild` (created via `Guild::Create`; tabard `UPDATE guild SET Emblem*`) | write | `CreateGuild` / `SetGuildEmblem` |
| `acore_characters` | `guild` name column via `Guild::SetName` | write | `OllamaChatGuildNameWorldScript::OnUpdate` |
| `acore_characters` | `playerbots_guild_names` (`name_id`, `name`) | read | `CreateRandomGuildName` fallback / `PlayerbotGuildMgr::LoadGuildNames` |
| `acore_characters` | guild-member rows via `Guild::AddMember` | write | elite/casual member loops |

All `*_guid`/`guid` columns are guid **counters** (low GUIDs), not raw 64-bit
GUIDs — same convention as the sentiment tables.

---

## 5. Concurrency & threading

**World thread (single-threaded core):**

- `FormPersonalityGuilds` and everything it calls synchronously (DB loads,
  `CreateGuild`, `AddMember`, `OnGuildUpdate`, `Begin`). It runs inside the
  bot-login processing path, so all `Player*`/`Guild*` touches are on the world
  thread — safe.
- `GuildRecruitmentEvent::Update/Tick/Finish` — driven by
  `PlayerbotsWorldScript::OnUpdate` (explicit comment: `// World thread only`).
  Teleports, grid scans, packet queueing and `Player*` reacquisition all happen
  here. The event stores **`ObjectGuid`s, never `Player*`s**, and reacquires via
  `ObjectAccessor::FindPlayer` every tick — so a leader logging out mid-event
  can't dangle.
- `OllamaChatGuildNameWorldScript::OnUpdate` — applies the rename via
  `Guild::SetName` (core guild state + DB write) on the world thread.

**Detached worker thread (per rename):**

- The `std::thread(...).detach()` inside `OllamaChat_RenameGuildInVoice` runs the
  blocking `SubmitQuery(...).get()` off the world thread so the Ollama round-trip
  never stalls the tick. It captures **values only** (guildId, strings, opts) —
  no `Player*`, no `Guild*` — so there's nothing tick-owned to dangle. It reaches
  into `sGuildMgr`/`sObjectMgr` only for **read-only** name checks
  (`GetGuildByName`, `IsReservedName`, `IsProfanityName`) and then hands the
  result to the world thread. **The actual mutation** (`Guild::SetName`) is
  deliberately deferred to `OnUpdate` on the world thread.

**Synchronization:** the single `g_GuildNameQueueMutex` guards
`g_GuildNameQueue`. The worker holds it only to `push_back`; the world script
holds it only to `swap` the whole vector into a local, then processes the local
lock-free. Because the worker's uniqueness check and the world-thread apply are
separated in time, `OnUpdate` **re-checks** `GetGuildByName` before `SetName` to
close the race where two guilds were assigned the same generated name.

---

## 6. Config keys

Read with `sConfigMgr->GetOption<T>(key, default)`. Config loads at startup →
**restart worldserver after changes**. None of these ship in the `.conf.dist`
files; add the line yourself to change a default.

| key | default | read in | meaning |
|---|---|---|---|
| `AiPlayerbot.PersonalityGuild.RaidCount` | `4` | `FormPersonalityGuilds` | raid guilds to attempt |
| `AiPlayerbot.PersonalityGuild.PvpCount` | `3` | same | pvp guilds to attempt |
| `AiPlayerbot.PersonalityGuild.CasualCount` | `8` | same | casual guilds to attempt |
| `AiPlayerbot.PersonalityGuild.LeaderMinLevel` | `10` | same | min level for any guild leader |
| `AiPlayerbot.PersonalityGuild.CasualJoinChance` | `40.0` (float) | same | % chance each eligible bot joins a casual guild |
| `AiPlayerbot.RandomBotGuildSizeMax` | `15` | `PlayerbotAIConfig.cpp` → `randomBotGuildSizeMax` | per-guild member cap (floored to 2 as `maxMembers`) |
| `AiPlayerbot.RandomBotGuildCount` | `20` | `PlayerbotAIConfig.cpp` → `randomBotGuildCount` | **must be 0** for personality guilds — see Section 7 |
| `AiPlayerbot.GuildRecruitEnabled` | `true` | `GuildRecruitmentEvent::Begin` | master switch for recruitment |
| `AiPlayerbot.GuildRecruitMinutes` | `15` | `Begin` | recruiting window length per leader |
| `AiPlayerbot.GuildRecruitInviteRange` | `20.0` (float) | `Tick` | yard radius of the recruit grid scan |
| `OllamaChat.EnableGuildNameGen` | `true` | `OllamaChat_RenameGuildInVoice` | enable LLM renames |
| `OllamaChat.Enable` (→ `g_Enable`) | — | same | chat module master switch; gates rename |

Compile-time constants (anon ns in `GuildRecruitmentEvent.cpp`, not config):
`kTickIntervalMs = 5s`, `kRealmStartUptimeSec = 30 min`,
`kMaxInvitesPerLeader = 10`.

---

## 7. Failure modes & gotchas

- **`RandomBotGuildCount` must be 0 (ordering requirement).** In
  `OnBotLoginInternal`, *after* the population-complete check, upstream still
  runs `if (randomBotGuildCount > 0) { PlayerbotFactory(...).InitGuild(); }` on
  **every** bot login. `InitGuild` → `PlayerbotGuildMgr::AssignToGuild` assigns
  bots into random-name guilds as they log in — *before* the last bot arrives and
  `FormPersonalityGuilds` runs. Those bots become guilded and are filtered out of
  the personality pool (`LoadOnlineGuildlessBots` skips `GetGuildId()`, and
  `findLeader` skips guilded candidates). With `RandomBotGuildCount = 0` the
  upstream assignment is skipped entirely, leaving the whole population guildless
  for the personality factory to claim. This is why the realm ships with it set
  to 0.
- **Once-per-process guard.** `static bool formed` means a second worldserver
  session (restart) is required to re-run formation; there is no in-session
  reset. It also means the login hook can safely fire it many times.
- **Realm-start gate.** `GuildRecruitmentEvent::Begin` aborts if uptime ≥ 30 min
  or `leaders` is empty — recruitment is strictly a fresh-boot experience. On a
  long-running server that restarts bots, formation is already blocked by
  `formed`; even if it weren't, recruitment self-disables.
- **Weak-symbol degradation.** `OllamaChat_RenameGuildInVoice` and
  `OllamaChat_SpeakSituation` are `[[gnu::weak]]`. Built with
  `-DDISABLED_AC_MODULES="mod-ollama-chat"` they resolve to null and the wrappers
  (`SpeakGuildName`, `SpeakSituation`) no-op: guilds keep their `MakeThemedName`
  names and leaders recruit silently (invites still pop). ELF/GCC-Clang behavior
  only; not portable to MSVC.
- **Reacquire-by-GUID everywhere.** Every `Player*`/`Guild*` is re-fetched at use
  time (`FindConnectedPlayer`, `FindPlayer`, `GetGuildByName`, `GetGuildById`).
  Stale/logged-out leaders and members are handled: `findLeader` and the member
  loops mark them `committed`; `Tick` drops offline leaders; the rename worker
  captures no pointers at all.
- **Stale guild name in pitches.** `Recruiter.guildName` is captured in `Begin`
  from `leader->GetGuild()->GetName()` — the *themed* name — because the LLM
  rename (Entry C) is async and almost always lands **after** `Begin` runs
  synchronously. So recruitment pitches ("recruiting for '{guildName}'") can
  reference the placeholder name even after the guild has been renamed. If you
  want pitches to use the live name, re-read `leader->GetGuild()->GetName()` in
  `Tick` instead of the cached field.
- **Name-collision race (renames).** Two workers can independently pass their
  `GetGuildByName` check for the same name; the world-thread `OnUpdate`
  re-checks before `SetName`, so at most one wins and the other silently keeps
  its themed name.
- **Rename rejections are silent.** A too-short (`< 3`), reserved, profane, or
  taken generated name simply drops the pending rename — the guild keeps the
  themed name, no error logged. `Guild::SetName` returning false also silently
  skips.
- **No information_schema probe here.** Unlike the gear-give / pending-give paths,
  `LoadPersonalities` queries `mod_ollama_chat_personality` directly; a missing
  table yields a null result treated as "no personalities" → no guilds form (not
  a crash, but also no graceful log beyond the empty-pool message).
- **`MakeThemedName` exhaustion.** After 20 failed unique-name attempts it falls
  back to `RandomPlayerbotFactory::CreateRandomGuildName()`; if that pool is also
  exhausted the name may be empty and the form call returns `false` (that guild
  is skipped, the loop stops).
- **Member cap floor.** `maxMembers = max(2, randomBotGuildSizeMax)` — setting
  `RandomBotGuildSizeMax` to 0/1 still yields 2, so a guild always has room for
  at least one member beyond the leader.
- **Invite path is fully core-validated.** `SendGuildInvite` only *queues*
  `CMSG_GUILD_INVITE`; the real `HandleGuildInviteOpcode` still applies faction /
  guilded / rank checks, so a `FindNearbyRecruit` false-positive can't produce an
  illegal invite — it just gets rejected by the core.
- **Log channels to watch:** `[PersonalityGuild]` and `[GuildRecruit]`
  (Playerbots.log, `playerbots` category); `[Ollama Chat] [GuildName]`
  (Server.log, `server.loading` category).

---

## 8. Cross-references

- [../BOT-BEHAVIOR.md](../BOT-BEHAVIOR.md) — Section 2 personalities (the fit-set keys),
  Section 3 playstyles, Section 12 the behavior-level summary of this subsystem.
- [../BOT-ECONOMY.md](../BOT-ECONOMY.md) — Section 4 the full
  `OllamaChat_SpeakSituation` weak-hook mechanism reused by recruitment; Section 5 the
  one-binary static-link model that makes the weak symbols bind.
- Sibling internals docs in this directory cover the related subsystems this one
  leans on: the personality/template tables and assignment, the sentiment
  system, the Ollama query pipeline (`SubmitQuery` / `OllamaQueryOptions`), and
  `PlayerbotGuildMgr`'s random-bot guild lifecycle (`Init`, `ValidateGuildCache`,
  `DeleteBotGuilds`, the hourly `BotGuildCacheWorldScript`).
