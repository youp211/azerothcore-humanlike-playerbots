# Internals: PvP respect — appreciation, rescues & gank disapproval

*Subsystem of `mod-ollama-chat`. Source of truth:
`modules/mod-ollama-chat/src/mod-ollama-chat_pvp.{cpp,h}`. This document is the
developer-level companion to [`../BOT-BEHAVIOR.md`](../BOT-BEHAVIOR.md) Section 13
("PvP respect"); it explains how the code actually works, function by function.*

---

## 1. Purpose

Turn *how a player fights* into a relationship signal. On every PvP kill of an
enemy-faction foe, the friendly random bots that were fighting nearby react:
they gain respect for an honorable kill, gain a lot for a **rescue** (you kill
the enemy that was about to finish off a low-HP bot), and **lose** respect if
the kill was a dishonorable **gank** of a much-lower-level foe. It piggybacks
entirely on existing machinery — sentiment ([05](05-sentiment.md)), memory
([13](13-memory.md)), and `OllamaChat_SpeakSituation`
([14](14-social-channels.md)) — and adds no new tables.

The load-bearing design decision: **respect + memory always update; speaking is
optional.** The sentiment nudge and the memory row are cheap and happen every
time. The spoken line is a separate roll, scaled by the reacting bot's
personality talkativeness, and only ever fires when a real player is close
enough to hear it — so bot-vs-bot skirmishing at 600-bot scale never wakes the
LLM. This is the standard "personality base chance + sentiment gate" and
"server acts, then the model narrates" pattern (see the internals
[README](README.md) pattern list), specialized to combat.

---

## 2. Entry point & call graph

One hook, one detached-thread fan-out for any spoken lines.

```
PlayerScript::OnPlayerPVPKill(killer, killed)          [PlayerScript.h; dispatched from Unit::Kill()]
  └─ OllamaChatPvpScript::OnPlayerPVPKill               [pvp.cpp]   (world thread)
       ├─ (guards: chat+sentiment enabled, EnablePvpAppreciation, enemy-faction kill)
       ├─ RESCUE detect  → NudgeSentimentPair + RecordMemoryForPair   (sentiment.cpp / memory.cpp)
       ├─ one bounded Map::GetPlayers() walk → {witnesses, realPlayerPresent, highLevelEnemyNearby}
       ├─ classify GANK vs APPRECIATION
       │    ├─ GANK        → per-witness NudgeSentimentPair(−) + RecordMemoryForPair + maybe 1 ChimesIn line
       │    └─ APPRECIATION→ per-witness NudgeSentimentPair(+) + RecordMemoryForPair + maybe 1 ChimesIn line
       ├─ deferred rescue payoff → maybe ChimesIn thank-you (+ MaybeBattleBuddy for a real killer)
       └─ LOG_INFO("[PvpFriend] …")
                 │
   every ChimesIn/MaybeBattleBuddy line ↓
   OllamaChat_SpeakSituation(bot, target, situation, whisper)          [handler.cpp]
     └─ std::thread(detached): SubmitQuery → reacquire Player* by GUID → Whisper()/Say()
```

Registration: `AddSC_mod_ollama_chat_pvp()` (bottom of `pvp.cpp`) instantiates
`new OllamaChatPvpScript()`, called from `Addmod_ollama_chatScripts()` in
`mod-ollama-chat_main.cpp`. The `PlayerScript` is constructed with only a name,
so AzerothCore enables *all* `PlayerScript` hooks for it (an empty hook list is
filled with every id) — same as the sibling `ChatOnKill`. The `.cpp` was a new
file, so the build needed a `cmake` reconfigure before `make` (pattern #2 in
[01-architecture](01-architecture.md)); it is now globbed.

---

## 3. `OnPlayerPVPKill(Player* killer, Player* killed)` — the whole flow

Runs on the **world thread** (synchronous inside `Unit::Kill`), so all `Player*`
are valid for the duration; only the deferred LLM lines cross a thread boundary,
and those capture GUIDs and reacquire (handled inside `OllamaChat_SpeakSituation`).

**Guards** (early-return):
- `g_Enable && g_EnableSentimentTracking` and `OllamaChat.EnablePvpAppreciation`.
- `killed->GetTeamId() != killer->GetTeamId()` — must be a real cross-faction
  kill. (No same-faction friendly-fire, no PvE.)
- **No killer-is-bot guard** — a bot killer is processed too. `killerIsBot`
  (`GetPlayerbotAI(killer) != nullptr`) is remembered for two later decisions:
  whether to whisper vs say, and whether to pitch a guild.

**Config read once** (all `sConfigMgr->GetOption`, defaults in `pvp.cpp` and
documented in `mod_ollama_chat.conf.dist`): `range` (40yd), the three sentiment
deltas, `rescueHealthPct`, `buddyChance`, and the three base **voice** chances.

**`realPlayerPresent`** starts `= !killerIsBot` (a real killer is inherently in
earshot) and is flipped true by the map walk if any other real player is in
range. It gates *every* spoken line — never the sentiment/memory work.

### 3a. Rescue detection (first, so it claims the cooldown)

```cpp
if (Unit* victim = killed->GetVictim())               // whoever the dying enemy was attacking
  if (victim->IsPlayer())
    Player* saved = victim->ToPlayer();
    if (saved alive && IsFriendlyRandomBot(saved, killer)
        && saved->GetHealthPct() <= rescueHealthPct   // 35%
        && MarkNudged(saved, killer))                 // claims the 2-min pair cooldown
      NudgeSentimentPair(saved, killer, rescueSentiment);   // +0.12
      RecordMemoryForPair(saved, killer, "pvp_rescue", "you saved me from a <race> <class>");
      rescuedBot = saved;  rescued = 1;
```

`killed->GetVictim()` is `m_attacking` — the unit the dying enemy was beating
on. In the normal death path this is still intact at this hook (it is only
cleared in the priest Spirit-of-Redemption branch), so "enemy was about to kill
a low-HP bot, player intervened" is detected correctly. The grateful **voice**
is *deferred* to Section 3d (I don't yet know `realPlayerPresent`); only the sentiment
and memory happen here, and `MarkNudged` claims the pair's cooldown so this bot
gets the big rescue bump and is skipped by the smaller appreciation bump below.

### 3b. The single bounded map walk

One pass over `killer->GetMap()->GetPlayers()`, each candidate range-gated once
(`IsWithinDist(killer, range, false)`), doing three things at once:
- **`realPlayerPresent`**: set true if the candidate is not a playerbot.
- **witnesses**: same-team, in-combat, random bots, collected into a
  `std::vector<Player*>` capped at `PVP_MAX_APPRECIATE_BOTS` (10) — "everyone in
  the fight" while staying bounded.
- **`highLevelEnemyNearby`**: an enemy-team player whose level ≥
  `killer level − PvpGankLevelGap` — evidence this is a real skirmish (gank
  exclusion B(ii)).

Doing all three in one walk is why there is never a second map iteration.

### 3c. Classify — gank vs appreciation

`isGank` is true iff **all**: `!rescued`, `EnablePvpGankDisapproval`, and
`killer level − killed level >= PvpGankLevelGap` (10) — **and none** of the
exclusions:
- **A — shared fight**: `killed->GetVictim()` is a same-team random bot (any
  HP). A bot was already on the target → fair game. (Rescue is the low-HP
  subcase, already handled and mutually exclusive via `!rescued`.)
- **B(i) — big fight**: `killer->GetVictim()` is a hostile unit (≠ the just-
  killed target) of level ≥ `killer level − PvpGankLevelGap`.
- **B(ii) — high-level enemy nearby**: the `highLevelEnemyNearby` flag from the
  walk.

All level math uses `int32` casts, so a low-level killer never underflows.

**GANK branch**: for each witness with **line of sight** (`IsWithinLOSInMap` —
a bot behind a wall didn't see it) and off the per-pair cooldown
(`MarkNudged`): `NudgeSentimentPair(bot, killer, gankSentiment)` (−0.05) +
`RecordMemoryForPair(... "pvp_gank" ...)`. Then **at most one** witness, if
`realPlayerPresent && ChimesIn(bot, gankVoiceChance)`, says a disapproving line
(public `say`, not whisper).

**APPRECIATION branch** (`else`): for each witness off cooldown,
`NudgeSentimentPair(bot, killer, killSentiment)` (+0.03) +
`RecordMemoryForPair(... "pvp_kill" ...)`. Then **at most one** witness, if
`realPlayerPresent && ChimesIn(bot, killVoiceChance)`, says a "good work" line.
Because this branch also runs after a rescue (`!isGank`), the rest of the group
gets the small bump; the rescued bot is skipped by its cooldown stamp.

### 3d. Deferred rescue payoff

After the walk (so `realPlayerPresent` is known):
```cpp
if (rescuedBot && realPlayerPresent && ChimesIn(rescuedBot, rescueVoiceChance))
    OllamaChat_SpeakSituation(rescuedBot, killer, "…thank them…", /*whisper=*/!killerIsBot);
if (rescuedBot && !killerIsBot)
    MaybeBattleBuddy(rescuedBot, killer, buddyChance);
```
Whisper when a real player saved it; **say aloud** when a bot did (whispering to
a bot is pointless). `MaybeBattleBuddy` (the on-the-spot guild pitch) fires only
for a real-player rescuer; a bot rescuer's raised sentiment simply feeds the
ongoing recruiter ([12](12-guilds.md)) — no duplicate invite path.

Finally two `LOG_INFO("server.loading", "[PvpFriend] …")` lines summarize
appreciated/rescued counts and any gank disapproval.

---

## 4. Helpers (file-local, anonymous namespace)

- **`ChimesIn(bot, baseChance)`** — the personality gate. Returns
  `roll_chance_f(min(100, baseChance * GetPersonalityReplyChanceMultiplier(
  GetBotPersonality(bot))))`. The base chance is the moment's inherent
  noteworthiness (rescue 50 > kill 25 ≈ gank 20); the multiplier is the bot's
  talkativeness — a high multiplier (e-girl, arena elitist) pipes up often, a
  low one (quiet types) almost never. This is the *only* thing deciding whether
  a bot speaks; respect never depends on it.
- **`MarkNudged(bot, player)`** — the 2-minute per-pair cooldown. A
  `std::mutex`-guarded `std::map<pair<u64,u64>, uint32(getMSTime())>`; returns
  true and stamps if the pair is off cooldown. Stops a kill farm from spamming
  standing, and enforces rescue-beats-appreciation for the saved bot. In-RAM,
  cleared on restart by design (pattern in the [README](README.md)).
- **`IsFriendlyRandomBot(candidate, ally)`** — same team, has a `PlayerbotAI`,
  and `sRandomPlayerbotMgr.IsRandomBot` (an autonomous bot, not a real player's
  alt/controlled bot).
- **`DescribeEnemy(bot, enemy)`** — `"<race> <class>"` via the reacting bot's
  `ChatHelper->FormatRace/FormatClass`, for the memory text.
- **`EnemyFactionName(friendly)`** — "Horde"/"Alliance" relative to the bot.
- **`MaybeBattleBuddy(bot, player, chance)`** — guild-leader-only pitch: bot
  leads a guild with room, player is unguilded/uninvited, sentiment ≥ 0.6, and
  a `roll_chance_f(chance)` succeeds → one in-voice "you'd fit right in" line.
  The **invite itself is intentionally not sent here** — the raised sentiment
  crosses the ongoing recruiter's ≥0.6 bonus threshold, which owns the single
  authoritative invite path (guild cap, cooldown, anti-popup-spam). Avoids
  double popups.

---

## 5. Config keys

All read via `sConfigMgr->GetOption` with in-code defaults; documented in
`mod_ollama_chat.conf.dist` and mirrored into the live conf.

| Key | Default | Effect |
|---|---|---|
| `OllamaChat.EnablePvpAppreciation` | 1 | Master switch for the whole hook |
| `OllamaChat.EnablePvpGankDisapproval` | 1 | Enable the gank (negative) branch |
| `OllamaChat.PvpAppreciationRange` | 40.0 | Witness / high-level-enemy scan radius (yd) |
| `OllamaChat.PvpKillSentiment` | 0.03 | Respect bump per honorable kill |
| `OllamaChat.PvpRescueSentiment` | 0.12 | Respect bump for the saved bot |
| `OllamaChat.PvpGankSentiment` | −0.05 | Respect drop per gank witness |
| `OllamaChat.PvpRescueHealthPct` | 35.0 | Saved bot must be at/under this HP% |
| `OllamaChat.PvpGankLevelGap` | 10 | Victim this many levels below = gank-eligible |
| `OllamaChat.PvpRescueVoiceChance` | 50.0 | Base thank-you chance (× talkativeness) |
| `OllamaChat.PvpKillVoiceChance` | 25.0 | Base "good work" chance (× talkativeness) |
| `OllamaChat.PvpGankVoiceChance` | 20.0 | Base disapproval chance (× talkativeness) |
| `OllamaChat.PvpBuddyGuildChance` | 25.0 | Rescued guild-leader on-the-spot pitch chance |

(Sentiment deltas apply to the `[0,1]` mutual pair value and are clamped inside
`SetBotPlayerSentiment`.)

---

## 6. Gotchas & edge cases

- **`GetVictim()` may be null/stale at kill time.** Rescue relies on the dying
  enemy's `m_attacking` still pointing at the bot; a kill via pet/DoT/AoE with
  no active melee target can leave it null (no rescue credited — acceptable, no
  false positives). B(i) tolerates a null `killer->GetVictim()`; B(ii) still
  covers the skirmish case.
- **Bot↔bot memory is a no-op by design.** `RecordMemoryForPair` only records
  when exactly one side is a bot and the *player* side is guilded
  ([13-memory](13-memory.md)); a two-bot pair records nothing, so bot-vs-bot PvP
  adds zero DB churn while still moving in-RAM sentiment.
- **Voice, never respect, is gated on `realPlayerPresent`.** Two bots duelling
  in the wild with no human around still build/erode mutual respect (the unseen
  history that feeds guild formation, [12](12-guilds.md)); they just don't burn
  an Ollama call to narrate it.
- **`GetTeamId()` in battlegrounds** is used as-is; `GetBgTeamId()` would be
  marginally more precise for BG cross-faction assignment, but world PvP — the
  target scenario — is correct.
- **Cooldown map growth.** `g_lastPvpNudge` is never evicted; bounded by pairs
  that actually fought within a boot (≤ bots² worst case, a few MB), cleared on
  restart. Not a leak worth managing.

---

## 7. Cross-references

- [BOT-BEHAVIOR.md](../BOT-BEHAVIOR.md) — Section 13 (this system, mechanism-first),
  Section 4 sentiment, Section 12 guilds (where raised respect becomes an invite), Section 6
  quest-help (the sibling sentiment gate).
- [05-sentiment](05-sentiment.md) — `NudgeSentimentPair` / `SetBotPlayerSentiment`
  (the symmetric, clamped writer these nudges call).
- [13-memory](13-memory.md) — `RecordMemoryForPair` and the guilded-only bound.
- [14-social-channels](14-social-channels.md) — `OllamaChat_SpeakSituation`, the
  detached-worker narrate-a-situation primitive every line here uses.
- [04-personalities](04-personalities.md) — `GetPersonalityReplyChanceMultiplier`
  (the talkativeness scalar behind `ChimesIn`).
