# Patches to the nested repos

The AzerothCore fork and its modules are separate git clones (excluded from this
repo). Our changes to them live here as patch files so they can be re-applied if
a `git pull`/`checkout` in the nested repos clobbers the working tree.

## Apply

```bash
cd azerothcore-wotlk && git apply /home/admin/git/wow/patches/azerothcore-mariadb-compat.patch
cd modules/mod-ollama-chat && git apply /home/admin/git/wow/patches/mod-ollama-chat-enhancements.patch
cd ../mod-playerbots && git apply /home/admin/git/wow/patches/mod-playerbots-playstyles.patch
```

(`git apply --check` first to test; conflicts mean upstream moved — re-resolve by hand.)

## Regenerate after changing the nested repos

```bash
cd azerothcore-wotlk && git diff -- src/server/database > ../patches/azerothcore-mariadb-compat.patch
cd modules/mod-ollama-chat && git add -N data/sql/characters/base/*.sql && git diff > ../../../patches/mod-ollama-chat-enhancements.patch
cd ../mod-playerbots && git diff > ../../../patches/mod-playerbots-playstyles.patch
```

## Contents

**azerothcore-mariadb-compat.patch** — makes the Playerbot fork build/run on MariaDB:
- `MySQLConnection.cpp`: `!defined(MARIADB_VERSION_ID)` guards (SSL options, `mysql_stmt_bind_named_param`)
- `DatabaseWorkerPool.cpp`: accept MariaDB Connector/C 3.x client, parse `"11.8.6-MariaDB"` server versions

**mod-ollama-chat-enhancements.patch** — module improvements:
- MariaDB fix: chat-history cleanup rewritten from `WITH … DELETE` to `DELETE … JOIN`
- Per-personality behavior: DB columns `weight`, `reply_chance_multiplier`,
  `num_predict_override`, `temperature_override` (+ migration SQL, weighted random
  assignment, per-call Ollama options plumbing)
- `OllamaChat.RequestTimeout` conf key (was hardcoded 120 s)
- Sentiment event nudges: `NudgeSentimentPair` + duel/guild-join/group-add hooks
  (`SentimentOnGroup` GroupScript), conf keys `SentimentEventGroup/Guild/DuelAdjustment`
- Bugfix: `ChatOnGuildMemberChange` was never registered in `main.cpp` — guild
  event hooks never fired upstream; now registered
- Playstyle column migration (`2026_07_03_personality_playstyle.sql`): adds
  `playstyle` to the personality templates + maps the upstream 33 personalities
  (the 40 custom ones are mapped in `personalities.sql`)

**mod-playerbots-playstyles.patch** — per-personality gameplay:
- Six RPG weight profiles (grinder/quester/socializer/explorer/pvper/idler) in
  `PlayerbotAIConfig`, tunable via `AiPlayerbot.RpgStatusProbWeight.<Profile>.<Status>`
- `NewRpgBaseAction::RandomChangeStatus` resolves the bot's playstyle from its
  mod-ollama-chat personality (shared `acore_characters` DB, cached per guid,
  5-min retry on miss) and rolls activities from that profile's weights
- Degrades gracefully: no playstyle column / no personality / chat module
  disabled → global weights, zero queries after the first probe
