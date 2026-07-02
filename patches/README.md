# Patches to the nested repos

The AzerothCore fork and its modules are separate git clones (excluded from this
repo). Our changes to them live here as patch files so they can be re-applied if
a `git pull`/`checkout` in the nested repos clobbers the working tree.

## Apply

```bash
cd azerothcore-wotlk && git apply /home/admin/git/wow/patches/azerothcore-mariadb-compat.patch
cd modules/mod-ollama-chat && git apply /home/admin/git/wow/patches/mod-ollama-chat-enhancements.patch
```

(`git apply --check` first to test; conflicts mean upstream moved — re-resolve by hand.)

## Regenerate after changing the nested repos

```bash
cd azerothcore-wotlk && git diff -- src/server/database > ../patches/azerothcore-mariadb-compat.patch
cd modules/mod-ollama-chat && git add -N data/sql/characters/base/*.sql && git diff > ../../../patches/mod-ollama-chat-enhancements.patch
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
