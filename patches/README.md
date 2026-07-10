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
- Gear-inspect context (2026-07-03): `{gear_context}` prompt placeholder —
  the bot "inspects" the player it talks to: weakest armor slot + class stat
  priority + a real tradeable upgrade from the bot's own bags if it has one;
  well-geared players get recognition context instead (epic raid set /
  PvP resilience / solid-for-level) so bots don't nag geared players. How the
  bot uses it (gift, sales pitch, mockery, respect) is personality-driven and
  baked into the wow-chat fine-tune (see finetune/)
- Sentiment bridges for mod-playerbots (2026-07-10): plain-extern
  `OllamaChat_GetSentiment` / `OllamaChat_NudgeSentiment` in `handler.cpp` expose
  the LIVE (bot→player) sentiment value and a direct nudge to the same binary, so
  the dungeon-companion system can judge a run and reward/penalise it. Bound with
  `[[gnu::weak]]` at the playerbots call sites (no-op when this module is absent /
  sentiment tracking is off), same idiom as `OllamaChat_SpeakSituation`

**mod-playerbots-playstyles.patch** — per-personality gameplay + arena coordination:

*Arena team coordination (2026-07-03):*
- `arena kill target` value: deterministic team-wide focus target (healers
  first, then lowest health, guid-ordered ties + bucketed health so all
  members agree without messaging); a real player on the team overrides it by
  just attacking — bots assist the human's target
- `arena focus` trigger + `attack arena kill target` action wired into
  `ArenaStrategy`: the whole team stays on the called target
- Synchronized burst: `boost` (burst-cooldown strategy) is no longer always-on
  in arena; `arena burst window` trigger + `arena burst sync` action hold
  everyone's cooldowns until a teammate (bot **or human**) pops a burst aura
  (Recklessness/Avenging Wrath/Icy Veins/... 18 iconic 3.3.5 auras) or the
  kill target drops to execute range (≤50%), then the team unloads together
- Rated arena participation itself is upstream machinery: enable via
  `AiPlayerbot.RandomBotAutoJoinBGRatedArena{2v2,3v3}Count` (set in our live
  conf); teams auto-create once level-70+ bot captains exist

*Quest-help invites (2026-07-03):*
- `QuestHelpOfferTrigger` + `OfferQuestHelpAction`: ungrouped bots in good
  sentiment standing with a nearby real player (reads mod-ollama-chat's
  sentiment table, cached) occasionally offer to group: ~2%/check when the bot
  confirms a shared quest, 0.5% questing-nearby, 0.1% generic help offer —
  say-line + real group invite. Conf: `AiPlayerbot.QuestHelp*`

*Playstyles:*
- Six RPG weight profiles (grinder/quester/socializer/explorer/pvper/idler) in
  `PlayerbotAIConfig`, tunable via `AiPlayerbot.RpgStatusProbWeight.<Profile>.<Status>`
- `NewRpgBaseAction::RandomChangeStatus` resolves the bot's playstyle from its
  mod-ollama-chat personality (shared `acore_characters` DB, cached per guid,
  5-min retry on miss) and rolls activities from that profile's weights
- Degrades gracefully: no playstyle column / no personality / chat module
  disabled → global weights, zero queries after the first probe

*Dungeon autofill + companion persistence (2026-07-10):*
- New self-contained subsystem `src/Bot/Factory/DungeonCompanions.{cpp,h}`
  (world-thread singleton + one `GroupScript` + one `PlayerScript`, wired in
  `Script/Playerbots.cpp` exactly like `PartyGuildFormation`)
- **Autofill**: a world tick keeps every real player's dungeon-finder queue fed
  with role/level-appropriate free random bots (a tank, a healer, then DPS),
  queued for the player's selected dungeons via the same `CMSG_LFG_JOIN` packet
  `LfgJoinAction` uses; the core LFG matcher then forms the group. Self-healing,
  per-player cooldown. Conf: `AiPlayerbot.DungeonAutofill.*`
- **Persistence**: every dungeon *run* (a group with the real player + ≥1 random
  bot that actually zones into an instance) is tracked; at run end each bot is
  judged by its live sentiment (via the ollama bridges above) after a run-outcome
  nudge — good run → **friend**, very bad → **troll**, neutral → not saved.
  Saved companions land in `acore_characters.mod_playerbots_companions`
  (migration `data/sql/characters/base/2026_07_10_dungeon_companions.sql`) and are
  excluded from the re-randomise churn in `RandomPlayerbotMgr::ProcessBot` (one
  `IsCompanion` guard before `Randomize`), so they keep their identity and can be
  met again. Conf: `AiPlayerbot.DungeonCompanion.*`
- Degrades gracefully: no companions table → persistence idles (logged, retried
  on next boot); chat module / sentiment off → no companions saved, autofill
  unaffected
