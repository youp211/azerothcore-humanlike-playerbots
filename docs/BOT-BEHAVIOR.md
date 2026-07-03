# Bot Behavior Systems — Deep Dive

How the "humanlike bots" actually work: every system, its mechanism, where the
code lives, what's in the database, the config knobs, and how to verify it's
working. The [README](../README.md) is the quick operating reference;
[BUILD-NOTES](BUILD-NOTES.md) is the chronological journal. All custom code is
captured as patch files in `patches/` (see `patches/README.md` for
apply/regenerate).

Contents:
1. [Architecture: who does what](#1-architecture-who-does-what)
2. [Personalities](#2-personalities)
3. [Playstyles: personality-driven gameplay](#3-playstyles-personality-driven-gameplay)
4. [Sentiment: evolving relationships](#4-sentiment-evolving-relationships)
5. [Gear-inspect chat context](#5-gear-inspect-chat-context)
6. [Quest-help invites](#6-quest-help-invites)
7. [Arena team coordination](#7-arena-team-coordination)
8. [The wow-chat voice model](#8-the-wow-chat-voice-model)
9. [Verification & debugging cookbook](#9-verification--debugging-cookbook)

---

## 1. Architecture: who does what

Two modules split the work; they share the `acore_characters` database and
nothing else:

- **mod-playerbots** (C++ decision engine) — everything a bot *does*: leveling,
  questing, grinding, combat rotations, BG/arena play, grouping, gear
  upgrades. Runs a strategy engine per bot: **triggers** (conditions checked on
  intervals) fire **actions** (behavior), both looked up by name from context
  registries (`src/Ai/Base/{TriggerContext,ActionContext,ValueContext}.h`).
  Named **values** are computed/cached facts ("arena kill target").
- **mod-ollama-chat** (C++ → HTTP → Ollama) — everything a bot *says*: builds a
  prompt from world state + the bot's personality, sends it to the local
  Ollama server, says the reply in-game. Owns the personality and sentiment
  tables.

Cross-module integration is deliberately loose: mod-playerbots reads
mod-ollama-chat's tables directly (guid-keyed lookups, cached, with an
`information_schema` probe at startup so everything degrades gracefully to
vanilla behavior if the chat module or its migrations are absent).

```
personality (chat)  ──playstyle column──►  RPG activity weights   (gameplay)
sentiment   (chat)  ──sentiment table──►  quest-help invites      (gameplay)
world state (game)  ──{gear_context}───►  chat prompt             (chat)
```

## 2. Personalities

**What**: 75 chat personas. 33 ship with mod-ollama-chat; 42 are ours
(`personalities.sql`: 40 WotLK-2008 archetypes + EGIRL + ELITE_ARENA_PVPER).

**Table**: `acore_characters.mod_ollama_chat_personality_templates`

| column | meaning |
|---|---|
| `key` | e.g. `LFG_SPAMMER` |
| `prompt` | injected into every chat prompt as the bot's persona |
| `weight` | random-assignment commonness (LFG_SPAMMER 130, UNHINGED_TROLL 30) |
| `reply_chance_multiplier` | talkativeness: scales reply + ambient-chatter rolls (EGIRL 1.6×, SILENT_TYPE 0.3×) |
| `num_predict_override` | per-personality token cap (ONE_WORD_GRUNTER 10) |
| `temperature_override` | sampling chaos (STOIC_PALADIN 0.5, UNHINGED_TROLL 1.3) |
| `playstyle` | gameplay profile, see Section 3 |

**Assignment**: `mod_ollama_chat_personality` (guid → key), rolled
weight-proportionally at bot login (`GetBotPersonality()`,
`mod-ollama-chat_personality.cpp`). **Stable for life**: an assigned
personality is never re-rolled. Templates hot-reload with console
`ollama reload`; *assignments* only load at startup, so re-rolling requires
clearing rows + restart (reset-world.sh now avoids this by loading all 75
templates before the first bot logs in).

**Tuning**: edit `personalities.sql`, apply with
`sudo mariadb acore_characters < personalities.sql`, then `ollama reload` in
the worldserver console. New keys join the pool for future assignments.

## 3. Playstyles: personality-driven gameplay

**What**: a bot's personality decides what it *does* all day, not just how it
talks. Six weight profiles for the New RPG activity roll: **grinder, quester,
socializer, explorer, pvper, idler** (+ `default` = global weights).

**Mechanism**: with `AiPlayerbot.EnableNewRpgStrategy = 1`, an idle bot
periodically rolls its next activity from 8 weighted statuses (DoQuest,
GoGrind, GoCamp, WanderNpc, WanderRandom, TravelFlight, Rest, OutdoorPvP).
Upstream uses one global weight table
(`AiPlayerbot.RpgStatusProbWeight.<Status>`); our patch makes it per-bot:

- `NewRpgBaseAction::RandomChangeStatus`
  (`mod-playerbots/src/Ai/World/Rpg/Action/NewRpgBaseAction.cpp`) resolves the
  bot's playstyle — guid-cached lookup joining
  `mod_ollama_chat_personality → templates.playstyle`, 5-min retry on empty so
  bots pick up late-assigned personalities — and rolls from that profile's
  weights.
- Profiles load in `PlayerbotAIConfig.cpp`; every weight is overridable:
  `AiPlayerbot.RpgStatusProbWeight.<Grinder|Quester|Socializer|Explorer|Pvper|Idler>.<Status>`.
  Built-in defaults are documented in `server/etc/modules/playerbots.conf`.

**Mapping**: upstream 33 mapped in the module migration
(`2026_07_03_personality_playstyle.sql`), our 42 in `personalities.sql`.
Spread across 75: 16 socializer, 12 explorer, 12 idler, 8 grinder, 9 pvper,
7 quester, 11 default.

**Verified live** (2026-07-02, 1,142 logged rolls): each profile's signature
status dominates — grinder 59% grind, quester 72% quest, socializer 49%
wander-npc, idler 40% rest, explorer 30% wander-random. PvP profile can't
express at low level (OutdoorPvP unavailable) — its weight redistributes until
bots level into PvP zones.

**Config change → restart** worldserver (config loads at startup).

## 4. Sentiment: evolving relationships

**What**: per-pair relationship scores, 0.0 (hostile) → 1.0 (friendly),
default 0.5. Personality is the stable core; sentiment is what changes.

**Table**: `acore_characters.mod_ollama_chat_bot_player_sentiments`
(`bot_guid`, `player_guid` — guid *counters* — `sentiment_value`). Bot↔bot
pairs work too.

**Moves via**:
- **Chat tone** — an extra LLM classification call per exchange nudges the pair
  (this is the one Ollama call that keeps global sampling params).
- **World events** (our patch, no LLM): duel ends −0.03 between the two;
  group add +0.02 vs each member; guild join +0.01 vs online guildmates.
  Conf: `OllamaChat.SentimentEvent{Duel,Group,Guild}Adjustment`.

**Used by**: chat prompts (`{sentiment_info}` steers tone), and quest-help
invites (Section 6). Inspect: `.ollama sentiment view <bot> <player>` in-game, or
query the table.

## 5. Gear-inspect chat context

**What**: when a bot chats with a player it "inspects" them, and the prompt
carries what a 2008 player would notice. The *personality* decides what to do
with it: WOW_MOM gifts, GOLD_FARMER sells, MIN_MAXER quotes percentages,
ELITE_ARENA_PVPER tells you to get good, and nobody lectures a raid-geared
player.

**Mechanism** (`GenerateGearContext()`,
`mod-ollama-chat/src/mod-ollama-chat_handler.cpp`, injected as
`{gear_context}` in `OllamaChat.ChatPromptTemplate`):

1. Scan the player's 9 armor slots (weapons/jewelry excluded); find the
   weakest by item level (empty counts as 0). Track epic count, set pieces,
   resilience pieces along the way.
2. **Recognition tiers** — if nothing is weak (worst ilvl ≥ player level, no
   empty slot):
   - 4+ resilience pieces → *"serious PvP resilience gear - not someone to
     lecture about gear"*
   - 5+ epics → *"decked in epic raid gear[, set pieces and all]"*
   - otherwise → *"gear is solid for their level"*
3. **Weak-slot tier** — names the slot + item level, states the class stat
   priority (rogue→agility, mage→intellect/spellpower, …), and scans the
   *bot's own bags* for a real, tradeable (non-soulbound), class-wearable
   (armor-class rules by class+level), level-appropriate upgrade. If found,
   the item name + ilvl goes in the prompt — offers reference real inventory.

The reply behavior is trained into the wow-chat model (Section 8) with
per-personality reaction banks, so it's in-voice, not templated.

**Note**: the context is informational — the bot won't literally hand the item
over; an actual trade is a possible future step.

## 6. Quest-help invites

**What**: bots that *like* you sometimes offer to group — most likely when
they can confirm you're on the same quest. Nobody you've never interacted
with will cold-invite you (strangers sit at neutral 0.5 sentiment, below the
0.6 threshold).

**Mechanism** (`QuestHelpOfferTrigger` → `OfferQuestHelpAction`, wired into
`NewRpgStrategy`; code in `mod-playerbots/src/Ai/World/Rpg/Trigger/NewRpgTrigger.cpp`
and `src/Ai/Base/Actions/InviteToGroupAction.cpp`):

Every 30 s, an eligible bot (alive, ungrouped, not in BG/combat) looks for a
nearby (≤30 yd) real player who is alive, ungrouped, not DND, and whose
sentiment with the bot ≥ threshold. Then one roll:

| tier | condition | default chance/check | say-line flavor |
|---|---|---|---|
| confirmed | player's log has the bot's active quest | 2.0% | "hey you're on *X* too right? group up, goes way faster" |
| nearby | bot is questing, can't confirm the player is | 0.5% | "grinding the same camp it looks like - group?" |
| random | bot isn't questing | 0.1% | "need a hand with anything? im around" |

On success: say-line + a real group invite (reuses the module's
`InviteToGroupAction::Invite` packet path).

**Conf** (`AiPlayerbot.*`): `QuestHelpSentimentThreshold` (0.6),
`QuestHelpConfirmedChance` (2.0), `QuestHelpNearbyChance` (0.5),
`QuestHelpRandomChance` (0.1). Chances are percent per 30-second check — at
2%/check, questing next to a friendly bot produces an invite roughly once per
25 minutes of shared proximity.

Bot↔bot grouping is upstream's separate `RandomBotGroupNearby` feature.

**Log line**: `[QuestHelp] Bot X offered quest help to Y (tier N)` (debug
level, Playerbots.log).

## 7. Arena team coordination

**What**: bot arena teams play like teams — one kill target, cooldowns
held and dumped together. If a real player is on the team, the bots follow
*their* lead automatically.

**Mechanism** (all in the mod-playerbots patch):

- **Kill-target calling** — `ArenaKillTargetValue`
  (`src/Ai/Base/Value/LeastHpTargetValue.cpp`): healers first, then lowest
  health (bucketed so the pick doesn't flap), computed *deterministically*
  from shared world state (guid-ordered player map) so every team member
  independently agrees without any messaging. **Human override**: if a real
  player on the team is attacking an enemy, that victim *is* the kill target —
  you call targets by attacking. `arena focus` trigger + `attack arena kill
  target` action keep everyone on it.
- **Synchronized burst** — the per-class `boost` strategy (offensive
  cooldowns) is removed from the arena default set; bots *hold* cooldowns.
  `ArenaBurstWindowTrigger` (`src/Ai/Base/Trigger/PvpTriggers.cpp`) opens the
  team burst window when **any teammate — bot or human — has one of 18 iconic
  3.3.5 burst auras up** (Recklessness, Avenging Wrath, Icy Veins, Bloodlust,
  Death Wish, Adrenaline Rush, …) **or** the kill target drops to ≤50% health.
  `arena burst sync` toggles `boost` on for everyone; window closes, cooldowns
  hold again. Pop your wings and the team pops with you.

**Participation** (upstream machinery, enabled in our conf): bot arena teams
auto-create once level-70+ random-bot captains exist
(`RandomPlayerbotFactory::CreateRandomArenaTeams`, counts:
`AiPlayerbot.RandomBotArenaTeam{2v2,3v3,5v5}Count` = 10/10/5, ratings
1000–2000). Rated queueing: `AiPlayerbot.RandomBotAutoJoinBGRatedArena2v2Count
= 2`, `3v3Count = 1` (0 = off; 5v5 off). **Dormant on a fresh world** until
bots level to 70+ — everything switches on by itself when they do.

**Log lines**: `[Arena] Bot X enters/leaves burst window` (debug);
`ARENA:2v2 ...` queue stats (info, once brackets have eligible bots).

## 8. The wow-chat voice model

**What**: a QLoRA fine-tune of Qwen3-4B-Instruct that replaces
generic-assistant diction with a terse, lazy-caps, 2008-player voice — now
including gear talk and the two new personalities. Served by local Ollama on
the 7900 XT (~114 tok/s, ~0.2 s per reply).

**Dataset** (`finetune/generate_dataset.py`, deterministic, 4,900 train +
100 eval): every user turn is byte-format identical to what mod-ollama-chat
sends at inference — including `{gear_context}` blocks that mirror the C++
strings exactly. Coverage: 42 personalities × 12 message categories, 25%
ambient chatter, sentiment-conditioned tone, ~35% gear contexts with
per-personality reaction banks (gift / sell / flame / respect) and
recognition examples for well-geared players.

**Pipeline** (details in `finetune/README.md` and `gpu-box/`):
generate → `04-train.py` (QLoRA, ~55 min on the 7900 XT) → merge fp16 →
GGUF → quantize Q8_0 (+Q4_K_M) → **stage as `wow-chat:q8-test`** → coherence
eval → promote tags → `ollama cp wow-chat:q8 wow-chat`. The realm picks up the
new model on the next generation; no server restart.

**Deploy gate — not optional**: one training round produced a corrupted model
(garbled subwords) with a *bit-identical clean loss curve*. Loss does not
prove the export is sane. Always eval samples
(`python3 finetune/eval_models.py <model> wow-chat:v1 --n 8 --show 8`) before
re-aliasing, and keep the previous model as a rollback tag
(`ollama cp wow-chat wow-chat:v1`). Rollback = `ollama cp wow-chat:v1 wow-chat`.

**Serving config** (`server/etc/modules/mod_ollama_chat.conf`):
`Model = wow-chat`, `Url = http://127.0.0.1:11434/api/generate`,
`MaxConcurrentQueries = 8`, `EnableTypingSimulation = 1`,
`OllamaChat.RequestTimeout = 120`. Ollama env: `OLLAMA_KEEP_ALIVE=-1`,
`OLLAMA_NUM_PARALLEL=4` (localhost bind).

## 9. Verification & debugging cookbook

**Is a system alive?**

```bash
# playstyles active (INFO, once, at first roll after boot)
grep "personality playstyle column found" server/bin/Playerbots.log
# per-bot playstyle resolution + roll outcomes (debug level)
grep "\[Playstyle\]" server/bin/Playerbots.log | tail
# quest-help offers
grep "\[QuestHelp\]" server/bin/Playerbots.log
# arena burst windows
grep "\[Arena\]" server/bin/Playerbots.log
# chat prompts incl. gear context (set OllamaChat.DebugEnabled + ShowFullPrompt)
grep "Full prompt sent" server/bin/Server.log | tail -1
# what Ollama is serving
ollama ps    # digest c456b968d394 = v2
```

**Distribution checks** (the playstyle methodology, reusable):

```sql
-- personality spread
SELECT personality, COUNT(*) FROM acore_characters.mod_ollama_chat_personality
  GROUP BY personality ORDER BY 2 DESC LIMIT 10;
-- playstyle spread over templates
SELECT playstyle, COUNT(*) FROM acore_characters.mod_ollama_chat_personality_templates
  GROUP BY playstyle;
```

For behavior claims, count **roll outcomes** (`rolled status N` debug lines),
not downstream events: "select random grind pos" lines fire during *candidate
availability checks* (once per roll, chosen or not) and status durations vary
wildly, so per-bot event counts are confounded. This exact mistake cost an
afternoon — see BUILD-NOTES Section 17.

**Model quality**: `python3 finetune/eval_models.py wow-chat wow-chat:v1 --n 10 --show 6`
— form metrics (avg words ~4–5, 100% ≤13 words, ~100% lazy-caps, 0
assistant-isms) *plus read the samples*; corruption shows up as garbled
subwords with clean metrics.

**Common gotchas** (hard-won, see BUILD-NOTES Section 14–18 for the stories):
- `.ollama reload` hot-reloads personality *templates*, never *assignments*.
- Console commands are dotless (`ollama reload`); `.command` is in-game syntax.
- First boot after a wipe: authserver can lose the DB-populate race and die —
  reset-world.sh now relaunches it automatically.
- Sentiment guids are *counters*, not raw 64-bit GUIDs.
- Killing a ROCm training run mid-step: verify the next run's *outputs*, not
  its loss curve.

---

## 10. Group-join requests & addressee disambiguation (2026-07-03)

Ask a bot to help/party in **proximity chat** (say/yell/whisper, within say
range — channel messages deliberately can't trigger invites from bots you
can't see) and it acts: `EvaluateGroupRequest`
(`mod-ollama-chat_groupjoin.cpp`) detects group-request intent (keyword
heuristic), rolls a personality base chance (LFG_SPAMMER 95 … ELITE_ARENA_PVPER
5, playstyle defaults otherwise) + `GroupJoinSentimentBonus` (+20 at sentiment
≥ 0.6), sends a **real group invite** on success, and tells the model what it
just did so the reply matches the action. Declines are phrased in-voice.

**Ambiguity**: 2+ candidate bots and none named → each asks "who, me?" in its
own voice with a randomized 0.8–5 s stagger; naming a bot (or a bare
affirmative like "yeah you") within 60 s resolves the pending ask to that bot.
Conf: `OllamaChat.EnableGroupJoin`, `GroupJoinSentimentBonus`. Logs:
`[GroupJoin]`.

## 11. Channel conversation system (2026-07-03)

Public channels (General/Trade) are conversation spaces, not random-banter
firehoses (`mod-ollama-chat_channels.cpp`):

- **Session memory**: last 30 lines per channel kept in RAM; a bot replying in
  a channel gets the recent stream as prompt context, so channel talk is
  conversation-aware.
- **Personality gating**: most personalities barely use General (8% scale —
  they're the quest-with-you-in-person types); the channel-native set
  (LFG_SPAMMER 90, GUILD_RECRUITER 85, traders 60-70, DRAMA_QUEEN 50 …) stays
  active **when the message is relevant** (group/dungeon/trade/guild keywords
  or being named). Irrelevant chatter gets near-zero bot response.
- **Throttles**: one channel line per bot per `ChannelReplyCooldownSec` (90).
- **Relevant announcements**: a periodic announcer lets channel-native bots
  post level-appropriate dungeon LFG, real-item WTS lines, or recruitment for
  their actual guild — a few per hour, only when real players share the
  faction+zone. Conf: `OllamaChat.EnableChannelOverhaul`,
  `ChannelReplyCooldownSec`, `ChannelAnnounceIntervalSec`,
  `ChannelAnnounceChance`. Logs: `[Channel]`.

## 12. Realm-start guild recruitment (2026-07-03)

After personality guilds form on a fresh world, each leader teleports to **its
own race's starting area** (from `playercreateinfo`), pitches its guild
in-voice every 60–90 s for `AiPlayerbot.GuildRecruitMinutes` (15), and sends
guild invites to nearby unguilded real players under level 10 (once per
player, ≤10 per leader), then teleports back to its old life
(`mod-playerbots/src/Bot/Factory/GuildRecruitmentEvent.cpp`). Accept one and
you level inside that leader's guild from day one. Conf:
`AiPlayerbot.GuildRecruit{Enabled,Minutes,InviteRange}`. Logs: `[GuildRecruit]`.
