# WoW 3.3.5a Server — AzerothCore + Humanlike Playerbots

A private WotLK (3.3.5a, build 12340) server whose world is populated by 500
autonomous player bots: they quest, grind, travel between zones, rest at inns,
queue for battlegrounds and dungeons, upgrade their gear, and chat in-character
via an LLM — with 73 distinct personalities, per-personality talkativeness/voice,
and relationships that evolve through duels, grouping, and guild life.

Built 2026-07-02 on LMDE 7 (Debian 13 "trixie"), i7-1185G7, 32 GB RAM.
LLM inference offloads to a second machine (RX 7900 XT) — see `gpu-box/README.md`.

Repo layout: work was developed on branches (`gpu-box`, `ollama-chat-mods`,
`personalities`, `finetune`) merged into `main`.

## Stack

| Component | What / where |
|---|---|
| Core | [liyunfan1223/azerothcore-wotlk](https://github.com/liyunfan1223/azerothcore-wotlk), branch `Playerbot` (custom fork — **required**, stock AzerothCore will not compile the bot module) |
| Bots | [liyunfan1223/mod-playerbots](https://github.com/liyunfan1223/mod-playerbots), branch `master` |
| LLM chat | [DustinHendrickson/mod-ollama-chat](https://github.com/DustinHendrickson/mod-ollama-chat) + local [Ollama](https://ollama.com) running `llama3.2:3b` |
| Database | MariaDB 11.8 (Debian packages) — **needs the local patches below** |
| Client data | [wowgaming/client-data](https://github.com/wowgaming/client-data) release v19 (maps/vmaps/mmaps/dbc) |

## Directory layout

```
/home/admin/git/wow/
├── README.md                     # this file
├── DEPENDENCIES.txt              # every apt package + external dependency
├── build.sh                      # clone (first run) + compile + install
├── restart-world.sh              # crash-restart loop for worldserver
├── azerothcore-wotlk/            # sources (fork + modules/), build/ inside
└── server/                       # runtime install (CMAKE_INSTALL_PREFIX)
    ├── bin/                      # authserver, worldserver (run from here)
    ├── etc/                      # worldserver.conf, authserver.conf
    │   └── modules/              # playerbots.conf, mod_ollama_chat.conf
    ├── data/                     # Cameras/ dbc/ maps/ mmaps/ vmaps/
    └── bin/*.log                 # Server.log, Errors.log, Playerbots.log, Auth.log
```

## Running the server

Two ways — don't mix them (the script refuses to start while the units are active):

**Manual with consoles** (day-to-day):

```bash
./start.sh            # both servers in tmux, with AC> consoles
./start.sh status     # processes / ports / sessions
./start.sh stop       # graceful shutdown
```

`tmux attach -t world` (or `-t auth`) for the console; detach with `Ctrl-b d`.

**systemd** (survives reboots, auto-restarts on crash, no console — use in-game
GM commands instead). Units are installed at `/etc/systemd/system/` (tracked
copies in `systemd/`) and **enabled**, so a reboot brings the realm up by itself:

```bash
sudo systemctl start|stop|status wow-auth wow-world
journalctl -u wow-world -f        # server output
```

If you want console mode after a reboot: `sudo systemctl stop wow-auth wow-world`
then `./start.sh`. MariaDB and Ollama are separate systemd services (also on boot).

### Quick test from the client

1. **Is the server up?** `pgrep authserver worldserver` should show both, and
   `ss -tln | grep -E "3724|8085"` both ports. Bots online:
   `sudo mariadb -N -e "SELECT COUNT(*) FROM acore_characters.characters WHERE online=1;"`
2. **No new user needed** — the `admin` account already exists (GM level 3,
   initial password `changeme123`). Log in with it, create a character, `/who`
   to see the bots.
3. Local Wine client: the copy in the `~/.jwgui/prefixes/WoW` prefix is already
   pointed at the realm. Launch with the input method disabled:
   `XMODIFIERS="@im=none" WINEPREFIX=~/.jwgui/prefixes/WoW wine "C:\Program Files (x86)\World of Warcraft\Wow.exe"`

### Accounts & realm

- GM account: `admin` (gmlevel 3). The initial password was a placeholder — change it:
  console `account set password admin <new> <new>`.
- Create player accounts (e.g. for friends): `account create <name> <pass>` in the
  worldserver console (`tmux attach -t world`), or as a GM in-game: `.account create <name> <pass>`.
- Realm: `Gigi` at `127.0.0.1:8085` (world), auth on `3724`. Both bind `0.0.0.0`.
  For WAN play, port-forward 3724 + 8085 and point the realm at your public IP/DDNS:
  ```sql
  UPDATE acore_auth.realmlist SET address='your.public.ip.or.ddns' WHERE id=1;
  ```
- Client side: WoW 3.3.5a enUS client, `Data/enUS/realmlist.wtf` → `set realmlist 127.0.0.1`.

## Databases

MariaDB 11.8, user `acore` / password `acore` (localhost only). Databases:
`acore_auth`, `acore_world`, `acore_characters`, plus the module's `acore_playerbots`.
Schema updates apply automatically at worldserver startup (`Updates.AutoSetup=1`).
InnoDB tuning lives in `/etc/mysql/mariadb.conf.d/60-azerothcore.cnf`.

Handy queries (Debian root uses unix_socket — use `sudo mariadb`):

```sql
SELECT COUNT(*) FROM acore_characters.characters WHERE online=1;   -- bots in world
SELECT level, COUNT(*) FROM acore_characters.characters WHERE online=1 GROUP BY level;
SELECT COUNT(*) FROM acore_auth.account WHERE username LIKE 'RNDBOT%';
```

## Configuration highlights

All configs are the `.conf` files next to their `.conf.dist` templates (dist = documented
defaults; conf = live values). Diff against the dist to see every local change.

**`server/etc/worldserver.conf`** — `DataDir` points at `server/data`, `MapUpdate.Threads = 4`.

**`server/etc/modules/playerbots.conf`** — the "living world" settings:

- `MinRandomBots / MaxRandomBots = 250` — online bot population. The module keeps ~2×
  that many characters total across auto-created `RNDBOT*` accounts.
- Fresh realm: `DisableRandomLevels = 1`, `RandombotStartingLevel = 1` (note the lowercase
  `b` — the key really is spelled that way), `RandomBotFixedLevel = 0` → everyone starts
  at level 1 and levels naturally. Death Knight bots start at 55 (class minimum — expected).
- `EnableNewRpgStrategy = 1` — bots autonomously wander, quest, grind, rest, train.
- `RandomBotJoinBG = 1`, `RandomBotJoinLfg = 1`, `AutoUpgradeEquip = 1`,
  `EquipAndSpecPersistence = 1`, `RandomBotTalk = 1`, `RandomBotSayWithoutMaster = 1`.

**`server/etc/modules/mod_ollama_chat.conf`** — LLM chat:

- `Model = llama3.2:3b`, endpoint `http://localhost:11434/api/generate`.
- `MaxConcurrentQueries = 2` — **do not set 0 (unlimited) on this CPU-only box**.
- `EnableRPPersonalities = 1` (33 personalities), chat history 5 turns per bot/player pair.
- Bots only chatter near *real* players (`RandomChatterRealPlayerDistance`), so Ollama is
  idle until someone logs in.

## ⚠️ Local patches to the nested repos

The fork and the chat module assume Oracle MySQL 8; this box runs MariaDB. All our
changes to the nested clones (MariaDB compatibility + the module enhancements below)
are captured as patch files in **`patches/`** with apply/regenerate instructions.
After any `git pull` / `./build.sh --update` in the nested repos, re-apply from
there before rebuilding. See `patches/README.md` for the full change list.

Debugging tip: a worldserver "segfault at 0 … error 6" in `dmesg` is AzerothCore's
`WPFatal` assertion (a deliberate null write), not memory corruption — the real message
is in `server/bin/Errors.log`.

## Personalities & relationships (our module extensions)

- **73 personalities** live in `acore_characters.mod_ollama_chat_personality_templates`
  (33 upstream + 40 from `personalities.sql`). Rows hot-reload with `.ollama reload`.
- **Per-personality behavior columns** (our addition): `weight` (assignment
  commonness), `reply_chance_multiplier` (talkativeness — the one-word grunter is
  0.6×, the LFG spammer 2×), `num_predict_override` (verbosity cap),
  `temperature_override` (chaos — stoic paladin 0.5, unhinged troll 1.3).
- **Stable core, evolving relationships**: a bot's personality never changes once
  assigned; the *sentiment* system (enabled) tracks per-pair relationship scores
  that shift from chat tone and world events — duels sour a pair, grouping and
  guild joins warm them (`OllamaChat.SentimentEvent*Adjustment`). Inspect with
  `.ollama sentiment view <bot> <player>`.
- New conf key `OllamaChat.RequestTimeout` (HTTP timeout to Ollama, default 120 s).

## GPU box & fine-tuning

`gpu-box/` has numbered scripts for the 7900XT machine: Ollama serving on the LAN
(Qwen3-4B, 100+ tok/s), then a QLoRA fine-tune pipeline (Unsloth/ROCm) that trains
the `wow-chat` voice model on the synthetic dataset in `finetune/` and exports it
back into Ollama. `gpu-box/apply-gpu-config.sh <ip>` flips the realm onto the GPU.

## Updating

```bash
cd /home/admin/git/wow
./build.sh --update        # git pull core + both modules, rebuild, make install
# then: check the three patches survived (git diff), restart worldserver
```

If a pull conflicts with or reverts a patch, re-apply it (details above) before building.

## Monitoring & tuning

- Console `server info` → *Update time diff*: <50 ms great, >150 ms sustained = reduce load.
- `tail -f server/bin/Playerbots.log` — bot activity; `Errors.log` — problems.
- LLM chat: `journalctl -u ollama -f` shows generate requests when bots talk to you.
- Scale the world: raise `MinRandomBots`/`MaxRandomBots` (this box has headroom for
  500–1000; new bot characters are created automatically on next boot). If update diffs
  climb or chat lags, switch `OllamaChat.Model` to `llama3.2:1b` first — it's the cheaper lever.

## Rough resource picture

250 bots idle at ~1 ms update diff; worldserver RAM grows toward ~8–11 GB as bots explore
more maps (maps never unload). Ollama holds the 3b model resident (~3 GB,
`OLLAMA_KEEP_ALIVE=-1`). MariaDB buffer pool is capped at 6 GB.
