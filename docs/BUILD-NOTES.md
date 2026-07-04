# Build Notes — Everything Done, In Detail

Complete technical record of building this server, in the order it happened
(2026-07-02). The [README](../README.md) is the operating reference; this
document is the *why and how*, including every problem hit and its fix.

Contents:
1. [Goal & architecture](#1-goal--architecture)
2. [Base server build](#2-base-server-build)
3. [MariaDB compatibility patches](#3-mariadb-compatibility-patches)
4. [First boot & the post-ready crash](#4-first-boot--the-post-ready-crash)
5. [Fresh-realm & immersion configuration](#5-fresh-realm--immersion-configuration)
6. [GPU offload plan (7900XT)](#6-gpu-offload-plan-7900xt)
7. [mod-ollama-chat source extensions](#7-mod-ollama-chat-source-extensions)
8. [40 new personalities](#8-40-new-personalities)
9. [Fine-tune pipeline (wow-chat model)](#9-fine-tune-pipeline-wow-chat-model)
10. [Scaling to 500 bots](#10-scaling-to-500-bots)
11. [Git repository layout](#11-git-repository-layout)
12. [Client setup under Wine/jwgui](#12-client-setup-under-winejwgui)
13. [Operations: start.sh & systemd](#13-operations-startsh--systemd)
14. [Troubleshooting log](#14-troubleshooting-log)

---

## 1. Goal & architecture

Make a WotLK 3.3.5a private server feel like a *populated MMO*: hundreds of
autonomous bots that quest, travel, fight, group, and talk like 2008 players.

```
┌─ host (Linux, i7-14700K, 32GB, RX 7900 XT) ───────────┐
│ authserver (:3724)  worldserver (:8085, 500 bots)     │
│ MariaDB 11.8 (4 acore_* DBs)                          │
│ mod-playerbots (bot brains, C++ strategy engine)      │
│ mod-ollama-chat (bot chat -> LLM via HTTP)            │
│ Ollama ROCm: Qwen3-4B / fine-tuned "wow-chat"         │
└───────────────────────────────────────────────────────┘
```

Key design facts discovered during research:
- **mod-playerbots requires a custom core fork** (`liyunfan1223/azerothcore-wotlk`,
  branch `Playerbot`); it has never been merged into stock AzerothCore and will
  not compile against it.
- The "world feels alive" behavior is mostly one module feature:
  `AiPlayerbot.EnableNewRpgStrategy` — bots autonomously wander, quest, grind,
  rest at inns, train, and use the AH.
- Bot *gameplay* is the C++ engine; the LLM only does *dialogue*. (This is why
  "re-train a model to play the game" was scoped to chat only — LLM-driven
  gameplay at 500-bot scale is orders of magnitude beyond local inference.)

## 2. Base server build

- **Toolchain**: Debian 13 packages (see `DEPENDENCIES.txt`). Built with
  **clang 19** rather than gcc 14 — the wiki-blessed compiler for the fork; gcc 14
  intermittently breaks on it, and clang uses less RAM.
- **Compile**: `build.sh` — clones core + both modules, then
  `cmake -DCMAKE_INSTALL_PREFIX=server -DTOOLS_BUILD=none -DSCRIPTS=static -DMODULES=static`
  and `make -j6`. `-j6` (not `-j8`) because the precompiled-header-heavy build
  spikes RAM; 8 jobs can OOM the system. `TOOLS_BUILD=none` because map
  extractors aren't needed (next point). Full build ≈ 35 min.
- **Client data**: pre-extracted maps/vmaps/mmaps/dbc from
  `wowgaming/client-data` release **v19** (1.2 GB zip → ~5 GB in `server/data`).
  mmaps are required by playerbots for navigation.
- **Databases**: MariaDB 11.8 with `create_mysql.sql` (makes `acore` user +
  auth/world/characters) plus a manually created `acore_playerbots`. All content
  imports happen automatically on first worldserver boot (`Updates.AutoSetup=1`);
  the playerbots module populates its own DB. InnoDB tuning in
  `/etc/mysql/mariadb.conf.d/60-azerothcore.cnf` (6G buffer pool,
  `innodb_flush_log_at_trx_commit=2`, `skip-log-bin`).
- **First-boot cost**: ~75 RNDBOT accounts × 10 characters are created on the
  first boot (10-40 min of console spam that looks like a hang). This work
  persists; later boots just log bots in.

## 3. MariaDB compatibility patches

The fork (and current upstream AzerothCore) only supports Oracle MySQL 8.
Debian has no MySQL packages, so three patches make MariaDB 11.8 work. All are
captured in `patches/azerothcore-mariadb-compat.patch` (+ the module fix in the
module patch) — **re-apply after any `git pull` in the nested repos**.

1. **Compile failure** — `src/server/database/Database/MySQLConnection.cpp`
   uses `MYSQL_OPT_SSL_MODE` / `mysql_ssl_mode` / `mysql_stmt_bind_named_param`,
   none of which exist in MariaDB's client library. Root cause: Debian's MariaDB
   headers define `MYSQL_VERSION_ID = 110806` (11.8.6), which satisfies the
   code's `#if MYSQL_VERSION_ID >= 80300` MySQL-8.3 checks. Fix: guard those
   paths with `!defined(MARIADB_VERSION_ID)` and fall back to
   `MYSQL_OPT_SSL_ENFORCE` / `mysql_stmt_bind_param`.
2. **Startup abort (wiki error ACE00043/46)** —
   `DatabaseWorkerPool.cpp` fatally requires client version ≥ 80000 *and*
   client == compiled version. MariaDB Connector/C reports its own version
   (3.4.9 → 30409), so both checks fail. Also `DatabaseIncompatibleVersion()`
   parses version strings character-by-character and cannot read the
   two-digit-major `"11.8.6-MariaDB"`. Fix: a `LIBMARIADB` branch accepting
   Connector/C ≥ 3.2.3, and an explicit MariaDB ≥ 10.5 check via `sscanf`.
3. **Crash ~45 s after "ready"** — see next section.

Debugging tip learned here: **`worldserver: segfault at 0 ... error 6` in dmesg
is not memory corruption** — it's AzerothCore's `WPFatal` assert, which crashes
deliberately by writing to address 0. The real error text is in
`server/bin/Errors.log`.

## 4. First boot & the post-ready crash

First boot came up ("World initialized in 19s", bots preparing to log in), then
died ~45 s later. `Errors.log` showed a SQL parse error: mod-ollama-chat's
chat-history pruning uses `WITH ranked_history AS (...) DELETE FROM ...` — a
**CTE-fronted DELETE, which MySQL 8 supports but MariaDB cannot parse** — and a
failed statement is fatal in AzerothCore. Fix (in
`modules/mod-ollama-chat/src/mod-ollama-chat_handler.cpp`): rewrite as
`DELETE h FROM ... JOIN (derived table with ROW_NUMBER()) ranked ON ...`,
validated against MariaDB directly before rebuilding. Same semantics, both
dialects happy.

Useful fact from the wreckage: the bot account/character creation from the
crashed boot **persisted in the DB**, so the rebuilt server booted straight into
bot logins.

## 5. Fresh-realm & immersion configuration

`server/etc/modules/playerbots.conf` deltas from dist:

| Key | Value | Why |
|---|---|---|
| `MinRandomBots` / `MaxRandomBots` | 500 | population (started 250, scaled after GPU plan) |
| `DisableRandomLevels` | 1 | fresh realm: no pre-seeded level spread |
| `RandombotStartingLevel` | 1 | everyone starts at 1 (key really has lowercase `b`) |
| `RandomBotFixedLevel` | 0 | bots keep leveling naturally |
| `EnableNewRpgStrategy` | 1 | the living-world brain |
| `RandomBotSayWithoutMaster` | 1 | ambient chatter without a master |
| BG/LFG/AutoUpgradeEquip/EquipAndSpecPersistence | 1 | default-on immersion features kept |

Death Knight bots start at 55 — class minimum, not a config bug. `worldserver.conf`:
`DataDir`, `MapUpdate.Threads = 4`. Both daemons bind `0.0.0.0` (dist default);
realm row `Gigi @ 127.0.0.1:8085` in `acore_auth.realmlist`.

Result at 250 bots: 1 ms mean update diff. At 500: 3-5 ms. Enormous headroom;
the eventual ceiling is RAM (maps never unload) and single-thread CPU.

## 6. GPU offload plan (7900XT)

The RX 7900 XT (gfx1100, officially supported by ROCm 7.x /
Ollama / Unsloth in 2026 — no `HSA_OVERRIDE_GFX_VERSION` hacks needed) serves
chat inference. Everything for it is in `gpu-box/`
as numbered user-run scripts (see `gpu-box/README.md` for order and
troubleshooting):

- `01-install-ollama.sh`: ROCm Ollama, `OLLAMA_HOST=0.0.0.0:11434`,
  `OLLAMA_KEEP_ALIVE=-1`, `OLLAMA_NUM_PARALLEL=4`, pulls **Qwen3-4B-Instruct**
  (chosen over llama3.2:3b for quality and over 8B for latency/concurrency).
- `apply-gpu-config.sh <ip> [model]` (run here): rewrites `OllamaChat.Url`/`Model`,
  raises `MaxConcurrentQueries` to 8, enables typing simulation, backs up the
  conf, and hot-applies via the worldserver console (`.ollama reload`) — no
  restart. Rollback = restore the `.bak` and reload.
- Until those scripts are run, chat stays on local CPU llama3.2:3b — slow but
  functional.

## 7. mod-ollama-chat source extensions

All changes are in the module patch (`patches/mod-ollama-chat-enhancements.patch`).
Design goal from the user: *personalities are stable at the core, but bots have
evolving relationships with specific players/bots*.

**Per-personality behavior** — migration
`data/sql/characters/base/2026_07_02_personality_behavior_columns.sql` adds four
columns to `mod_ollama_chat_personality_templates` (all defaults reproduce old
behavior exactly; the module's SQL auto-updater applies it at boot):

| Column | Meaning | Example use |
|---|---|---|
| `weight` (INT, 100) | random-assignment commonness | unhinged troll = 30 (rare) |
| `reply_chance_multiplier` (FLOAT, 1.0) | scales reply & ambient-chatter rolls | LFG spammer 2.0, silent type 0.3 |
| `num_predict_override` (INT, NULL) | per-personality token cap | one-word grunter = 10 |
| `temperature_override` (FLOAT, NULL) | per-personality sampling temp | stoic paladin 0.5, troll 1.3 |

Code path: `LoadPersonalityTemplatesFromDB()` (config.cpp) reads the columns
into a new `g_PersonalityTemplates` map (probing `information_schema` first so
un-migrated DBs still load with defaults); `GetBotPersonality()`
(personality.cpp) does a weighted roll instead of uniform; a new
`OllamaQueryOptions` struct (querymanager.h) carries num_predict/temperature
overrides per call through `SubmitQuery → QueryManager → QueryOllamaAPI` into
the request JSON (api.cpp); reply-chance scaling multiplies the per-bot rolls
in handler.cpp (:~1340) and random.cpp (:~153), clamped to 100%. The sentiment
classifier's own LLM call deliberately keeps global sampling.

**Relationships (sentiment)** — the module already had per-pair sentiment
scores (0.0 hostile → 1.0 friendly, nudged by an extra LLM call classifying
each message, injected into prompts as `{sentiment_info}`); it was just
disabled in the dist conf and only moved via chat. Enabled it and added
**direct world-event nudges** (no LLM call): `NudgeSentimentPair(a, b, delta)`
in sentiment.cpp, wired to duel end (−0.03 between the two), guild join
(+0.01 vs online guildmates), and group add (+0.02 vs each member, via a new
`SentimentOnGroup : GroupScript`). Deltas are conf keys
(`OllamaChat.SentimentEventGroup/Guild/DuelAdjustment`). Bot↔bot pairs work
out of the box since keys are raw GUID pairs. Personality assignment itself is
never re-rolled — verified in code: the cached/persisted assignment always wins.

**Upstream bug found & fixed**: `ChatOnGuildMemberChange` (all four guild
hooks) existed but was **never registered** in `mod-ollama-chat_main.cpp` —
guild event chatter had never fired for anyone. Registered it alongside the
new GroupScript.

**Also**: `OllamaChat.RequestTimeout` conf key (HTTP timeout was hardcoded
120 s in the vendored cpp-httplib wrapper; `SetTimeout()` existed but nothing
called it); `m_timeout` made atomic since worker threads set it per call.

Hot-reload caveat: `.ollama reload` re-reads personality *templates* but
per-bot *assignments* only load at startup (`LoadBotPersonalityList` is
startup-only) — that's why re-rolling assignments required a restart.

## 8. 40 new personalities

`personalities.sql` (idempotent DELETE+INSERT, run against `acore_characters`,
then `.ollama reload`) adds 40 WotLK-2008 archetypes to the upstream 33 → **73
total, combined random weight 6460**. The behavior columns are used
deliberately: chatty commons (LFG_SPAMMER w130/×2.0, TRADE_COMEDIAN ×1.8),
standard commons (QUEST_WHINER, LORE_NERD, WOW_MOM, HEALER_MAIN...), quiet
types (SILENT_TYPE ×0.3, ONE_WORD_GRUNTER 10-token cap), and spicy rares
(UNHINGED_TROLL w30/temp1.3, FAIRWEATHER_FRIEND w40). Since the initial 500
bots had been assigned from the old 33-only pool minutes earlier, all
assignments were cleared once so every bot re-rolled from the weighted 73 pool
(208 re-assigned across 68 distinct types within minutes of the restart).

## 9. Fine-tune pipeline (wow-chat model)

Goal: replace generic-assistant diction with an authentic 2008-player voice.

- **Dataset** (`finetune/generate_dataset.py`, deterministic): 4,900 train +
  100 eval examples. The critical design decision: **each training example's
  user turn is byte-format identical to what the module actually sends at
  inference** — the live `ChatPromptTemplate` / `RandomChatterPromptTemplate`
  with every placeholder filled from a WotLK world model (valid race/class
  combos, level-appropriate zones and dungeons, factions, guilds, gold,
  distances). Assistant turns come from hand-written per-personality reply
  banks plus a style layer (lazy caps, dropped punctuation, occasional typo
  injection, terseness) — avg 5.3 words, max 13. Coverage: 40 personalities ×
  12 message categories, 25% ambient-chatter examples, and
  sentiment-conditioned tone (hostile prompts get brush-offs, friendly ones
  get warm prefixes).
- **Training** (`gpu-box/04-train.py`, user-run on the GPU): Unsloth QLoRA
  (official AMD support), base `unsloth/Qwen3-4B-Instruct-2507`, r=16, seq 512,
  2 epochs, adamw_8bit — expected 20-60 min. Gotcha baked into the setup
  script: bitsandbytes must be a build **> 0.49.2** (older = NaNs in 4-bit on
  every AMD GPU).
- **Export** (`05-export-gguf.sh`): merge LoRA → fp16 → `convert_hf_to_gguf.py`
  → `llama-quantize Q4_K_M` → `ollama create wow-chat`. Deploy with
  `apply-gpu-config.sh <ip> wow-chat`; the base Qwen3 tag stays pulled as
  instant rollback.

## 10. Scaling to 500 bots

`Min/MaxRandomBots 250 → 500` + restart. The boot after the change created
~250 more RNDBOT characters (same looks-hung console spam as first boot).
Result: 500 online, update diff 3-5 ms. Guidance: raise further in steps of
~250, watch `server info` diffs (worry past ~150 ms sustained) and RAM.

## 11. Git repository layout

`/home/admin/git/wow` is a git repo. Development followed a
branch-per-component model, merged into `main` with merge commits:
`gpu-box` → `ollama-chat-mods` → `personalities` → `finetune`.

Deliberately **not** tracked (`.gitignore`): `azerothcore-wotlk/` and its
modules (nested git clones — our changes live as patch files in `patches/`
instead, see `patches/README.md` for apply/regenerate), `server/` (binaries,
passworded configs, 5 GB data, logs), `client/` (user's 50+ GB game client),
fine-tune model artifacts.

The `client/` client folder is itself a git repo (cloned from `devovh/wotlk`);
its realmlist/Config.wtf fixes are committed *there* (local only — don't push,
the remote is someone else's).

## 12. Operations: start.sh & systemd

Two mutually-guarded modes (details in README "Running the server"):

- **`./start.sh [start|stop|status]`** — tmux sessions `auth` + `world` with
  interactive `AC>` consoles. Refuses to start while the systemd units are
  active. `stop` prefers a graceful console `server shutdown 5` and falls back
  to signals; a 500-bot save takes ~30-60 s after the sockets close.
- **systemd units `wow-auth` / `wow-world`** (`systemd/` in repo, installed to
  `/etc/systemd/system/`, **enabled**): boot autostart ordered after
  mariadb/ollama, `Restart=on-failure`, and an `ExecStartPre` pgrep guard so
  they refuse to start over tmux-run instances. No console — use in-game GM
  commands (`.command`) or stop the units and use `start.sh`.

## 13. Troubleshooting log

Every incident hit so far and its resolution — check here first:

| Symptom | Root cause | Fix |
|---|---|---|
| Module won't compile: `mysql_ssl_mode` undeclared | MariaDB headers claim MYSQL_VERSION_ID 110806 | patch 1 (Section 3) |
| Worldserver aborts: "does not support MySQL below 8.0. Found 3.4.9" | Connector/C versions independently | patch 2 (Section 3) |
| Crash ~45 s after ready, `segfault at 0` in dmesg | `WITH … DELETE` unparseable by MariaDB; WPFatal null-write | rewrite as DELETE-JOIN (Section 4) |
| `segfault at 0 ... error 6` generally | deliberate `WPFatal` assert | read `Errors.log`, not the core dump |
| First boot "hangs" with console spam | RNDBOT account/character creation | wait (one-time, 10-40 min) |
| Bots all level 55 among the level 1s | Death Knights (class min 55) | expected |
| Client freezes after accepting ToS | D3D9 re-init under Wine + fullscreen + stale WDB | Section 12 fixes |
| `imDefLkup.c,355: key event already fabricated` spam | ibus XIM into Wine | `XMODIFIERS=@im=none` |
| "Unable to connect" at client login | authserver down (reboot had killed it; only worldserver was restarted) | systemd units now autostart both; `./start.sh status` to check |
| "invalid password" in Auth.log | password drift | reset via SRP6: compute salt+verifier in python (g=7, N=894B…9BB7, `v = g^SHA1(salt‖SHA1(USER:PASS)) mod N`, little-endian) and UPDATE `acore_auth.account` — used when no console is available |
| Worldserver running but no `AC>` console | process orphaned from dead tmux | works fine; restart via start.sh when convenient |
| `.ollama reload` didn't change bot personalities | assignments load at startup only | restart worldserver (templates *do* hot-reload) |
| Bot chat slow/laggy | GPU offload not active, MaxConcurrentQueries=2 | run gpu-box/01 + apply-gpu-config.sh (Section 6) |

---

# Part 2 — The behavior suite

Everything after the original build, in order (2026-07-02 evening → 2026-07-03).

## 14. First boot quirks (2026-07-02)

First boot quirks fixed permanently in `reset-world.sh`:

- **authserver loses the first-boot race**: on an empty auth DB, authserver
  and worldserver both try to populate it; authserver dies ("Could not
  populate the Login database"). The script now relaunches it once the schema
  exists.
- **Personalities before first roll**: the wipe drops the personality
  templates; the script now applies `personalities.sql` the moment the
  module's SQL updater recreates the table — all 75 types appear from the
  first assignment wave (verified: 73→75 distinct within minutes; previously
  bots rolled from the upstream 33 and needed a clear+restart dance).
- `--no-start` flag for staged resets.

## 15. Fine-tune pipeline, actually run (v1)

The `gpu-box/` scripts had never been fully executed.
First real run found three latent bugs (all fixed in the scripts):

1. `rocminfo | grep -m1 gfx` under `set -o pipefail` **false-fails**: grep's
   early exit SIGPIPEs rocminfo. Capture to a file, then grep.
2. llama.cpp's `requirements-convert_hf_to_gguf.txt` **replaces ROCm torch
   with a CPU build** from PyPI. Re-pin the ROCm wheels after that step.
3. `apply-gpu-config.sh` sent `.ollama reload` to the worldserver console —
   console commands are dotless.

v1 results: QLoRA of Qwen3-4B-Instruct, 4,900 examples, 2 epochs ≈ 50 min,
loss 3.26 → 0.21. Q8_0 chosen over Q4_K_M for best output (114 vs 152 tok/s —
both ~0.2 s per reply, quality wins). Eval harness added at
`finetune/eval_models.py`. Voice transformation: 4-word avg replies, 92%
lazy-caps, zero assistant-isms (base model: 13-word proper-case ramble).

## 16. Playstyles (personality-driven gameplay)

Design and verification in [BOT-BEHAVIOR.md Section 3](BOT-BEHAVIOR.md). Journal
lessons:

- The engine's `RpgStatusProbWeight` is global; the patch resolves a per-bot
  profile from the chat personality's new `playstyle` column (cross-module DB
  read, cached, `information_schema`-probed).
- **Measurement trap #1**: "[New RPG] select random grind pos" log lines fire
  in `CheckRpgStatusAvailable` — once per roll *per candidate*, chosen or not.
  They count rolls, not choices.
- **Measurement trap #2**: status durations differ wildly (Rest is short,
  GoGrind is long), so per-bot event counts are duration-confounded — idlers
  "led grinding" in the naive count purely by rolling more often.
- Resolution: log the roll outcome itself (`rolled status N` debug line),
  1,142 samples → every profile's signature status dominates as designed
  (grinder 59% grind, quester 72% quest, idler 40% rest).

## 17. Behavior suite day (2026-07-03)

**Arena coordination** — deterministic kill-target calling (healer-first,
lowest-HP, guid-ordered ties; a real player's victim overrides), plus
synchronized burst: `boost` removed from the arena default strategy set,
toggled team-wide by a burst-window trigger (teammate burst aura — 18 iconic
3.3.5 spells, human's count too — or kill target ≤50%). Rated arena auto-join
enabled (2v2×2, 3v3×1); dormant until level-70+ captains exist, then teams
auto-create. mod-playerbots does all of this; no LLM in the loop.

**Gear-inspect chat** — `{gear_context}` placeholder: weakest armor slot,
class stat priority, and a real tradeable upgrade from the bot's own bags if
one exists; recognition tiers (solid / epic raid set / PvP resilience) so
well-geared players get respect instead of nagging. Reactions are
personality-trained, not templated.

**Quest-help invites** — sentiment-gated (≥0.6), tiered odds (confirmed shared
quest 2%/check > questing nearby 0.5% > random 0.1%), say-line + real group
invite. Sentiment read crosses modules the same way playstyles do.

**Personalities** — EGIRL (socializer, ×1.6 reply chance) and
ELITE_ARENA_PVPER (pvper, temp 0.9) → 75 total.

**wow-chat v2 and the corruption incident** — dataset v2 (42 personalities,
~35% gear contexts with per-personality reaction banks). Round-2 training
produced a model emitting garbled subwords ("Billgtnrd") **with a clean,
bit-identical-to-round-3 loss curve** — and it briefly reached the realm alias
before sample reading caught it. Both quants were garbled → corruption
upstream of quantization, in the merge/export (which had run while a parallel
`make -j8` + worldserver restarts hammered the system); round 3 on identical
data/seed, exported quietly, came out clean and passed all
personality-gated gear probes (WOW_MOM gifts the actual bag item by name,
ELITE_ARENA_PVPER lectures, EGIRL compliments raid gear, GOLD_FARMER sells).
Procedure change: **stage as `wow-chat:q8-test`, eval coherence with samples,
keep `wow-chat:v1` as rollback, only then re-alias**. Loss curves do not
validate exports.

## 18. Client setup

Fresh prefix `~/.wine-wow` (wine + i386 already present), client run in place
from `client/wotlk/` (no 17 GB copy into the prefix this time). `Config.wtf`
carried the anti-freeze fixes (gxApi opengl, gxWindow 1, ToS flags);
added `gxResolution 3840x2160`, `gxMaximize 1`, `useUiScale 1` for the 4K
monitor. Stale `Cache/WDB` deleted after each world wipe. Desktop launcher
`wow-gigi.desktop` (app menu + Desktop) with the icon extracted from Wow.exe
via icoutils; launch env `XMODIFIERS=@im=none` remains mandatory (ibus/XIM
freeze).

## 19. Troubleshooting log, part 2

| Symptom | Root cause | Fix |
|---|---|---|
| authserver dead after fresh-world first boot | lost the auth-DB populate race to worldserver | reset-world.sh relaunches it (Section 15) |
| Only 33 personality types in play on fresh world | assignments rolled before personalities.sql applied | reset-world.sh loads templates before first login (Section 15) |
| `03-setup-training.sh`: "ROCm not working" but rocminfo fine | grep -m1 SIGPIPE under pipefail | capture-then-grep (Section 16) |
| torch suddenly CPU-only in training venv | llama.cpp requirements pulled PyPI torch | re-pin ROCm wheels (Section 16) |
| Idlers top the "grinding" stats | counting availability-check log lines, not roll outcomes | count `rolled status` lines (Section 17) |
| Fine-tuned model emits garbled subwords, loss curve clean | corrupted merge/export (ran under heavy load) | retrain/re-export quiet; coherence-gate deploys (Section 18) |
| Realm chat suddenly incoherent after model deploy | bad model reached the `wow-chat` alias | `ollama cp wow-chat:v1 wow-chat` (instant rollback) |
| Client window cropped / UI microscopic at 4K | native-res window without maximize/uiScale | gxMaximize 1 + useUiScale 1 (Section 19) |
| Old model still resident in VRAM after re-alias | keep_alive=-1 runners keyed by digest | `sudo systemctl restart ollama`, warm once |

## 20. Hostname addressing, env-var secrets, generic GM account (2026-07-03)

- **`gigi.local`**: `/etc/hosts` pins `127.0.0.1 gigi.local`; the client's
  `realmlist.wtf` says `set realmlist gigi.local` permanently. Combined with
  `acore_auth.realmlist.localAddress = 127.0.0.1` (loopback clients get
  loopback back from the realm list).
- **`.env` / `.env.example`**: GM account name/password and realm name moved
  out of the scripts into `WOW_GM_ACCOUNT` / `WOW_GM_PASSWORD` /
  `WOW_REALM_NAME` — `.env` is gitignored, `.env.example` carries safe
  defaults, so nothing personal needs committing.
- **GM account renamed `admin` → `admin`** (SRP6 verifier binds the username,
  so the rename required recomputing salt+verifier — password reset to the
  default in the process; characters ride along on the account row).

## 21. Guild validation + the fresh-world leader-level gotcha (2026-07-03)

Validated the personality-guild system with a 12-agent workflow (each invariant
checked vs live DB + the source defining it, then adversarially re-verified) and
a re-runnable `validate-guilds.sh`. Result: formation, leader↔archetype fit,
elite-purity/casual-mixing, membership integrity, and recruitment deployment all
PASS; LLM naming WARN (async rename dropped for a few guilds under login load).

**Gotcha found in play:** on a *fresh* world every bot is level 1 except Death
Knights (start 55), so with `PersonalityGuild.LeaderMinLevel = 10` the ONLY
eligible guild leaders were DKs — and they all recruited at the DK start zone
(map 609), invisible to a new non-DK player. Fix: `LeaderMinLevel = 1` so
leaders are drawn from all races and deploy to the actual newbie zones. Also
noted: the realm-start recruitment is a one-shot 15-min window (uptime<30min
guard), and leaders drift after teleport because their normal RPG AI keeps
running — findability/recurrence are follow-ups.

## 22. Guild lifecycle redesign: emergent, ongoing, party→guild (2026-07-03)

Reworked guilds from a one-time batch at realm start into a living lifecycle,
after play-testing showed the batch approach saturated every guild to the cap
instantly (so nothing could grow and the player couldn't be recruited) and the
realm-start teleport-to-spawn recruiter was hard to find (leaders drifted;
on a fresh world only Death Knights met the leader-level floor, so they all
recruited at the DK zone).

Three cooperating systems (`mod-playerbots`, all ticked from
`PlayerbotsWorldScript::OnUpdate`; details in
[internals/12-guilds.md](internals/12-guilds.md) and
[BOT-BEHAVIOR Section 12](BOT-BEHAVIOR.md)):
- **Emergent formation** — seed 2, then found one guild per 300 s up to 12,
  each started *small* so it has headroom to grow.
- **Ongoing recruiting** — founder bots recruit nearby unguilded bots
  (fit-respecting) and the player (sentiment-gated) during normal play.
- **Party→guild** — a good 5-min+ party crystallizes into a guild; deaths
  lower the odds; founder is the party's best leader personality or the player
  if they initiate ("let's guild up" in party chat).

The realm-start `GuildRecruitmentEvent` was decoupled (files left on disk,
uncalled). Params were deliberately **tightened** for real play: recruit
interval 45→180 s, per-pair cooldown 15→120 min, chances halved, plus a new
global 30-min cap on player invite popups across all leaders; emergent 150→300 s
and target 18→12; party base chance 15→8 %. Validate anytime with
`./validate-guilds.sh` (leader existence, archetype fit, elite purity,
membership cap, naming).

## 23. AI-reset chat leak fix + PvP respect system (2026-07-03)

**Two changes, one build.**

**The "AI was reset to defaults" leak.** Screenshots from the undead starting
zone caught three bots blurting *"AI was reset to defaults"* into public /say
around the Deathknell campfire the moment the player left the group. Cause: the
stock `ResetAiAction` (`mod-playerbots`) `TellMaster`s that status line whenever
a bot's AI resets on a routine group join/leave — fine for a real player's
controlled bot, spam for the 600 autonomous ones. Fix (`ResetAiAction.cpp`):
gate the `TellMaster` behind `!sRandomPlayerbotMgr.IsRandomBot(bot)`, so only a
real player actively controlling a bot ever sees it. Grepped the module — that
is the sole emitter of the string.

**PvP respect.** New feature the player asked for ("I like pvp… I want the bots
to appreciate if they're saved or if they have had fun… and if you gank low
levels and a nearby bot could witness it they don't like that"). New self-
contained `mod-ollama-chat_pvp.cpp/.h` hooking `PlayerScript::OnPlayerPVPKill`;
full mechanics in [BOT-BEHAVIOR Section 13](BOT-BEHAVIOR.md) and
[internals/17-pvp-respect.md](internals/17-pvp-respect.md). It reuses sentiment
+ memory + `OllamaChat_SpeakSituation`, adds no tables. Every enemy-faction kill
near fighting friendly bots classifies as **rescue** (save a low-HP bot: +0.12 +
grateful line), **appreciation** (honorable kill: +0.03 to the whole fighting
group), or **gank** (foe 10+ levels down with no bigger fight around: witnesses
in line-of-sight lose 0.05).

The design converged over several refinements from the player, all folded into
the one file before building:
- **Rescue also warms the group**, not just the saved bot ("appreciate everyone
  around and a part of the fight") — rescue and appreciation co-occur; only a
  gank suppresses appreciation.
- **Gank has real exclusions** ("if another bot initiates combat then it's fine
  and all other bots can be killed… if a player starts by fighting a high level
  or high levels are nearby then they also don't consider it a gank"): a shared
  fight (a bot already on the target), a same-size fight, or a high-level enemy
  nearby all cancel the penalty.
- **Applies to bot killers too** ("any feature I say for player pretty much
  would apply to other bots too") — bots judge each other, feeding guild
  formation. The sentiment store was already generic `guid→guid→float`;
  `RecordMemoryForPair` already no-ops for two-bot pairs, so this adds no DB
  churn.
- **Respect always moves; chatter is a personality-scaled chance** ("respect
  etc will always go up, it's the chats that are a chance… the egirl is like
  thanks for saving me :3 and the elite pvper is like good work and the quiet
  noob wouldn't say anything… generate the messages based on the fight
  context"): the spoken line rolls `baseChance × GetPersonalityReplyChanceMultiplier`
  and only fires when a real player is in earshot — so 600 bots skirmishing
  never wakes Ollama. Lines are model-generated from fight context, never canned.

Build was incremental and clean (`build.sh` → cmake reconfigure picked up the
new `.cpp`, worldserver relinked, no errors); `systemctl restart wow-world`
loaded it, world initialized in 11 s, 600 bots relogged, `Errors.log` clean.
Tunables: `OllamaChat.Pvp*` in `mod_ollama_chat.conf.dist`. Log:
`grep "\[PvpFriend\]" server/bin/Playerbots.log`.
