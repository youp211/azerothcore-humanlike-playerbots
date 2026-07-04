# WoW 3.3.5a Server — AzerothCore + Humanlike Playerbots

A private WotLK (3.3.5a, build 12340) server whose world is populated by 500
autonomous player bots: they quest, grind, travel, rest at inns, queue for
battlegrounds and dungeons, upgrade their gear — and behave like *people*:

- **75 chat personalities** with per-personality talkativeness, verbosity, and
  chaos, speaking through a locally fine-tuned LLM (`wow-chat`) in an
  authentic 2008 voice.
- **Playstyles**: a bot's personality drives its *gameplay* — grinders farm
  mobs, socializers loiter in town, idlers nap at inns, pvpers hunt world PvP.
- **Gear awareness**: bots inspect players they talk to and react in-voice —
  helpful types offer real spare items from their bags, merchants try to sell,
  elitists mock, and everyone recognizes raid sets and PvP resilience gear.
- **Relationships**: per-pair sentiment evolves through chat tone, duels,
  grouping, and guild life. Bots in good standing occasionally *offer to
  group* with you — most likely when they can confirm you share their quest.
- **Arena coordination**: bot arena teams focus one kill target (yours, if you
  attack first) and hold burst cooldowns until a teammate — including you —
  pops theirs. (Activates once bots level to 70+.)
- **World PvP respect**: fighting well beside bots earns their respect, *saving*
  one earns a lot (and a grateful "ty for the save"), and ganking a lowbie in
  front of them costs it. Bots judge each other's conduct too. Rising respect
  feeds the same guild/help/gift gates — so honorable PvP wins you friends.

**New here? Start with [docs/GETTING-STARTED.md](docs/GETTING-STARTED.md) — overview + install guide.**
**Deep dive on every behavior system: [docs/BOT-BEHAVIOR.md](docs/BOT-BEHAVIOR.md).**
**Function-level code reference for every subsystem: [docs/internals/](docs/internals/README.md).**
**Full build/migration history with every problem and fix: [docs/BUILD-NOTES.md](docs/BUILD-NOTES.md).**

Current host (since 2026-07-02): i7-14700K, 32 GB RAM, **RX 7900 XT** — the
realm and its LLM inference run on the same box (Ollama/ROCm, ~114 tok/s,
~0.2 s per chat reply). Originally built on an i7-1185G7 box and migrated;
the realm address is **127.0.0.1**.

## Stack

| Component | What / where |
|---|---|
| Core | [liyunfan1223/azerothcore-wotlk](https://github.com/liyunfan1223/azerothcore-wotlk), branch `Playerbot` (custom fork — **required**, stock AzerothCore will not compile the bot module) |
| Bots | [liyunfan1223/mod-playerbots](https://github.com/liyunfan1223/mod-playerbots), branch `master` + our patches |
| LLM chat | [DustinHendrickson/mod-ollama-chat](https://github.com/DustinHendrickson/mod-ollama-chat) + our patches + local [Ollama](https://ollama.com) (ROCm) serving **`wow-chat`** (fine-tuned Qwen3-4B, Q8_0) |
| Database | MariaDB 11.8 (Debian packages) — **needs the local patches** |
| Client data | [wowgaming/client-data](https://github.com/wowgaming/client-data) release v19 (maps/vmaps/mmaps/dbc) |

## Directory layout

```
/home/admin/git/wow/
├── README.md                     # this file (operating reference)
├── docs/BOT-BEHAVIOR.md          # every bot system, in depth
├── docs/BUILD-NOTES.md           # chronological journal, problems + fixes
├── DEPENDENCIES.txt              # every apt package + external dependency
├── build.sh                      # clone (first run) + compile + install
├── start.sh                      # tmux-mode start/stop/status (AC> consoles)
├── reset-world.sh                # one-shot world wipe + reseed (see below)
├── restart-world.sh              # crash-restart loop (legacy; systemd covers this)
├── personalities.sql             # 42 custom personalities + playstyle mapping
├── patches/                      # ALL changes to the nested repos (see its README)
├── finetune/                     # wow-chat dataset generator + eval harness
├── gpu-box/                      # Ollama serving + QLoRA training scripts
├── systemd/                      # wow-auth / wow-world units (installed + enabled)
├── azerothcore-wotlk/            # sources (fork + modules/), build/ inside
└── server/                       # runtime install (CMAKE_INSTALL_PREFIX)
    ├── bin/                      # authserver, worldserver, *.log
    ├── etc/                      # worldserver.conf, authserver.conf
    │   └── modules/              # playerbots.conf, mod_ollama_chat.conf
    └── data/                     # Cameras/ dbc/ maps/ mmaps/ vmaps/
```

## Running the server

Two ways — don't mix them (each refuses to start over the other):

**systemd** (default: boot autostart, crash restart, no console):

```bash
sudo systemctl start|stop|restart|status wow-auth wow-world
journalctl -u wow-world -f
```

**Manual with consoles** (when you want the `AC>` console):

```bash
sudo systemctl stop wow-auth wow-world   # if units are running
./start.sh            # both servers in tmux
./start.sh status     # processes / ports / sessions
./start.sh stop       # graceful shutdown
tmux attach -t world  # console (detach: Ctrl-b d)
```

MariaDB and Ollama are separate systemd services (also on boot).

**Start the world over**: `./reset-world.sh` — asks for confirmation, then
one-shot: wipes auth/characters/playerbots DBs, restarts, loads all 75
personalities *before* the first bot rolls, recreates the GM account, sets the
realm row (name + current IP + loopback localAddress), and relaunches
authserver if it loses the first-boot DB race. `--no-start` wipes and leaves
the realm down; `--full` also re-imports the static world DB. Bot re-seeding
takes minutes on this box (the docs' "10-40 min" was the old machine).

**Secrets / user-specific settings** live in `.env` (gitignored — copy
`.env.example`): GM account name + password, realm name. Scripts fall back to
the example defaults (`admin` / `changeme123` / `Gigi`) when no `.env` exists.

## Playing

- **This box**: launch **"World of Warcraft (Gigi)"** from the app menu /
  Desktop shortcut. The shortcut runs **`launch-client.sh`**, which
  self-heals before starting the game: this box is on **DHCP**, so the IP can
  drift ("unable to connect" at login) — the launcher rewrites the client's
  `realmlist.wtf` to the current IP, fixes `acore_auth.realmlist` + bounces
  authserver if stale, and clears the client's `Cache/WDB`. (`reset-world.sh`
  also derives the IP dynamically now.) Re-create the shortcut any time —
  e.g. after moving the repo — with `./install-shortcut.sh`.
  Client is configured for 4K borderless (`gxMaximize`) with max UI scale;
  `Config.wtf` carries the OpenGL + windowed fixes that prevent the
  post-ToS freeze under Wine.
- **Accounts**: `admin` (GM 3, initial password `changeme123` — change it:
  console `account set password admin <new> <new>`; defaults configurable via
  `.env`). Create more: `account create <name> <pass>` in the console or
  `.account create` in-game.
- **Realm addressing**: the local client connects to **`gigi.local`** — pinned
  to `127.0.0.1` in `/etc/hosts` — so it is immune to DHCP address changes;
  the realm row's `localAddress=127.0.0.1` routes loopback clients back to
  loopback for the world server. Other LAN devices use the box's current IP
  (the launcher keeps the realm row's external `address` up to date). Auth on
  `3724`, world on `8085`, both bind 0.0.0.0. For WAN play, port-forward both
  and point `acore_auth.realmlist.address` at your public IP/DDNS.

## Databases

MariaDB 11.8, user `acore` / password `acore` (localhost only): `acore_auth`,
`acore_world`, `acore_characters`, `acore_playerbots`. Schema updates apply at
worldserver startup (`Updates.AutoSetup=1`). InnoDB tuning:
`/etc/mysql/mariadb.conf.d/60-azerothcore.cnf` (6G buffer pool).

Handy queries (Debian root uses unix_socket — `sudo mariadb`):

```sql
SELECT COUNT(*) FROM acore_characters.characters WHERE online=1;   -- bots in world
SELECT level, COUNT(*) FROM acore_characters.characters WHERE online=1 GROUP BY level;
SELECT playstyle, COUNT(*) FROM acore_characters.mod_ollama_chat_personality_templates GROUP BY playstyle;
```

## Configuration highlights

Live values are the `.conf` files next to their `.conf.dist` templates; diff
against the dist to see every local change. Full explanation of the behavior
keys: [docs/BOT-BEHAVIOR.md](docs/BOT-BEHAVIOR.md).

**`server/etc/modules/playerbots.conf`**:

- `MinRandomBots / MaxRandomBots = 500`, fresh-realm leveling
  (`DisableRandomLevels = 1`, `RandombotStartingLevel = 1` — lowercase `b` is
  real), `EnableNewRpgStrategy = 1` (the living-world brain), BG/LFG/auto-gear
  on.
- **Playstyle profiles** (ours): `AiPlayerbot.RpgStatusProbWeight.<Profile>.<Status>`
  overrides; defaults documented in the conf.
- **Quest-help invites** (ours): `AiPlayerbot.QuestHelp{SentimentThreshold,ConfirmedChance,NearbyChance,RandomChance}`.
- **Rated arena** (upstream, enabled): `RandomBotAutoJoinBGRatedArena2v2Count = 2`,
  `3v3Count = 1`; teams auto-create when 70+ captains exist.

**`server/etc/modules/mod_ollama_chat.conf`**:

- `Model = wow-chat`, `Url = http://127.0.0.1:11434/api/generate`,
  `MaxConcurrentQueries = 8`, `EnableTypingSimulation = 1`,
  `OllamaChat.RequestTimeout = 120` (our conf key).
- `ChatPromptTemplate` includes `{gear_context}` (our placeholder — bots
  inspect the player they talk to).
- `EnableRPPersonalities = 1`; sentiment tracking + our event nudges
  (`SentimentEvent{Duel,Group,Guild}Adjustment`).
- Bots only chatter near *real* players, so Ollama is idle until someone logs in.

## ⚠️ Local patches to the nested repos

The nested clones (core fork + both modules) carry substantial local changes,
captured as **three patch files in `patches/`** with apply/regenerate
instructions in `patches/README.md`:

1. `azerothcore-mariadb-compat.patch` — build/run on MariaDB instead of MySQL 8.
2. `mod-ollama-chat-enhancements.patch` — MariaDB fix, per-personality behavior
   columns, sentiment event nudges, `{gear_context}`, playstyle migration,
   guild-hook registration bugfix, `RequestTimeout`.
3. `mod-playerbots-playstyles.patch` — playstyle profiles, arena coordination,
   quest-help invites.

**After any `git pull` in the nested repos, re-apply before rebuilding.**
Rebuild: `cd azerothcore-wotlk/build && make -j8 install` (incremental;
full toolchain in `DEPENDENCIES.txt`), then restart worldserver.

Debugging tip: a worldserver "segfault at 0 … error 6" in `dmesg` is
AzerothCore's `WPFatal` assertion, not memory corruption — the real message is
in `server/bin/Errors.log`.

## The wow-chat model & fine-tuning

`wow-chat` (Ollama alias → Q8_0 quant of our QLoRA fine-tune of
Qwen3-4B-Instruct) is what the bots speak through. The previous model is
always kept as `wow-chat:v1` — **rollback is
`ollama cp wow-chat:v1 wow-chat`**, no server restart needed.

Retraining (new personalities, new context types): edit
`finetune/generate_dataset.py`, regenerate, train on this box (~55 min), and
**follow the staged deploy in `finetune/README.md`** — one training round
produced a corrupted model with a perfectly clean loss curve; the coherence
gate exists for a reason.

## Monitoring & tuning

- Console `server info` → *Update time diff*: <50 ms great (this box idles at
  1 ms with 500 bots).
- `tail -f server/bin/Playerbots.log` — bot activity; `grep "\[Playstyle\]"`,
  `"\[QuestHelp\]"`, `"\[Arena\]"` for our systems; `Errors.log` — problems.
- `journalctl -u ollama -f` — chat generations when players are near bots.
- Scale: raise `MinRandomBots`/`MaxRandomBots` (headroom for 1000+ here); new
  bot characters are created automatically on next boot.

## Rough resource picture

500 bots: 1–5 ms update diff; worldserver RAM grows toward ~8–11 GB as bots
explore (maps never unload). Ollama holds wow-chat resident (~5 GB VRAM,
`OLLAMA_KEEP_ALIVE=-1`); QLoRA training peaks ~17.5 GB VRAM — evict resident
models first (`ollama stop wow-chat`). MariaDB buffer pool capped at 6 GB.
