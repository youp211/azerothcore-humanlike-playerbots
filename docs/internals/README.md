# Internals — Developer Reference

Function-level documentation of every **custom** subsystem in this server: the
actual code paths, call graphs, data structures, threading model, DB tables,
config keys, and failure modes. This is the "how the code works" layer, one
step deeper than the behavior docs.

Each page was written from the source and then **adversarially verified against
that source** (a second agent checked every function name, signature, config
key, and DB table; discrepancies were corrected). Line-number anchors are
best-effort — trust the symbol names and behavior, and grep for the exact line.

## Where each doc sits in the doc set

- [README](../../README.md) — operating reference (how to run it).
- [BOT-BEHAVIOR](../BOT-BEHAVIOR.md) — what each system does, mechanism-first.
- [BOT-ECONOMY](../BOT-ECONOMY.md) — the economy/social layer, mechanism-first.
- [BUILD-NOTES](../BUILD-NOTES.md) — chronological journal, every problem+fix.
- [TESTING](../TESTING.md) — in-game test checklist.
- **internals/** (you are here) — the code itself, function by function.

## Read this first

**[01-architecture](01-architecture.md)** — the load-bearing mental model, and
the three things that trip up every change:

1. **Two modules, one binary.** `mod-playerbots` decides what a bot *does*;
   `mod-ollama-chat` decides what it *says*. They share only the
   `acore_characters` DB and a set of `[[gnu::weak]]` cross-module symbols
   (`OllamaChat_SpeakSituation`, `OllamaChat_RenameGuildInVoice`) that let
   gameplay code call the chat module and degrade to a no-op if it's absent.
2. **New source file → re-run `cmake` before `make`.** Modules glob their
   sources at configure time (`CollectSourceFiles`); a bare `make` link-fails
   with an undefined reference until you reconfigure.
3. **World thread vs detached worker thread.** Anything touching game state
   (inventory, invites, guild rename, DB writes that must be transactional with
   the world) runs on the world thread; every LLM call runs on a detached
   worker thread that reacquires `Player*` by GUID after the HTTP round-trip.
   Get this wrong and you crash on a dangling pointer or corrupt state.

## Index

| # | Doc | Subsystem |
|---|-----|-----------|
| 01 | [architecture](01-architecture.md) | Module split, weak-symbol linkage, build, threading |
| 02 | [chat-pipeline](02-chat-pipeline.md) | Inbound chat → candidate bots → prompt assembly → reply |
| 03 | [llm-transport](03-llm-transport.md) | Query manager, `/api/generate`, HTTP client, per-call options |
| 04 | [personalities](04-personalities.md) | Template load, weighted assignment, query options |
| 05 | [sentiment](05-sentiment.md) | Per-pair relationship scoring + world-event nudges |
| 06 | [gear-context-gives](06-gear-context-gives.md) | Gear inspect context, recognition tiers, real gives |
| 07 | [trade-handover](07-trade-handover.md) | In-person pending-give trade state machine |
| 08 | [organic-ah](08-organic-ah.md) | Bots list spare loot at personality prices |
| 09 | [playstyles](09-playstyles.md) | Personality → RPG activity weight profiles |
| 10 | [quest-help](10-quest-help.md) | Sentiment-gated group-help invites |
| 11 | [arena](11-arena.md) | Deterministic kill-target + synchronized burst |
| 12 | [guilds](12-guilds.md) | Personality guilds, realm-start recruitment, LLM naming |
| 13 | [memory](13-memory.md) | Guilded-only event memories, recalled in chat |
| 14 | [social-channels](14-social-channels.md) | Group-join/who-me, channel system, SpeakSituation |
| 15 | [finetune-pipeline](15-finetune-pipeline.md) | Dataset generator, training, staged deploy gate |
| 16 | [ops](16-ops.md) | reset-world, systemd, self-healing client launcher |
| 17 | [pvp-respect](17-pvp-respect.md) | PvP kills/rescues/ganks move respect; personality-gated chatter |

## The recurring patterns (learn these once)

Every custom subsystem is built from the same handful of moves — recognize them
and any single doc reads fast:

- **Personality base chance + sentiment gate.** Helpful/social behaviors roll a
  per-personality base probability, boosted when the bot's sentiment for *that*
  player is high. See [04](04-personalities.md), [05](05-sentiment.md), used by
  [06](06-gear-context-gives.md) [10](10-quest-help.md) [14](14-social-channels.md).
- **Server acts, then the model narrates.** Gameplay code performs the real
  action (invite, mail, trade, rename) and injects an explicit *situation*
  string into the prompt; the model phrases it in-voice and never claims an
  action that didn't happen. `OllamaChat_SpeakSituation`, [14](14-social-channels.md).
- **Cross-module DB bridge, cached, probed.** `mod-playerbots` reads
  `mod-ollama-chat`'s tables directly (personality, playstyle, sentiment),
  guid-cached, behind an `information_schema` column probe so it degrades to
  vanilla if a migration is missing. [09](09-playstyles.md) [10](10-quest-help.md).
- **In-RAM cooldowns / caches keyed by GUID pair.** `getMSTime()` + a
  mutex-guarded `std::map<pair<low,low>,ts>` throttles gives, gear commentary,
  channel replies, who-me windows. Cleared on restart by design.
- **New feature file self-registers.** A new `mod-ollama-chat_*.cpp` exposes
  `void AddSC_mod_ollama_chat_*()` called from `Addmod_ollama_chatScripts()`;
  a new `mod-playerbots` file is globbed and wired via an existing hook.

## Regenerating / extending

When you add a subsystem, add its internals doc here and cross-link it from
the index and the pattern list above. The behavior summary goes in
BOT-BEHAVIOR/BOT-ECONOMY; the chronological "why" goes in BUILD-NOTES; the
function-level "how" goes here.
