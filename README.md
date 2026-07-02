# WoW 3.3.5a Server — AzerothCore + Humanlike Playerbots

A private WotLK (3.3.5a, build 12340) server whose world is populated by autonomous
player bots: they quest, grind, travel between zones, rest at inns, queue for
battlegrounds and dungeons, upgrade their gear, and chat in-character via a local LLM.

Built 2026-07-02 on LMDE 7 (Debian 13 "trixie"), i7-1185G7, 32 GB RAM.

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

```bash
cd /home/admin/git/wow/server/bin
tmux new-session -d -s auth  './authserver'
tmux new-session -d -s world './worldserver'     # or: tmux new -d -s world /home/admin/git/wow/restart-world.sh
```

- `tmux attach -t world` gives you the interactive `AC>` console (detach: `Ctrl-b d`).
- Stop cleanly: type `server shutdown 10` in the console (or `tmux send-keys -t world 'server shutdown 10' Enter`).
- MariaDB and Ollama are systemd services and start on boot; the game servers currently do not (start them via tmux as above).

### Accounts & realm

- GM account: `admin` (gmlevel 3). The initial password was a placeholder — change it:
  console `account set password admin <new> <new>`.
- Create player accounts: `account create <name> <pass>` in the console.
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

## ⚠️ Local patches (uncommitted — survive `git status`, not `git checkout`)

The fork and the chat module assume Oracle MySQL 8; this box runs MariaDB. Three local
patches make it work. **They are uncommitted working-tree changes.** After any
`git pull` / `./build.sh --update`, verify they're still present (`git diff` in
`azerothcore-wotlk/` and in `modules/mod-ollama-chat/`) before rebuilding:

1. **`src/server/database/Database/MySQLConnection.cpp`** — MariaDB headers report
   `MYSQL_VERSION_ID = 110806`, tripping MySQL-8-only code paths. Added
   `!defined(MARIADB_VERSION_ID)` guards around the `MYSQL_OPT_SSL_MODE` block and both
   `mysql_stmt_bind_named_param` sites (falls back to `MYSQL_OPT_SSL_ENFORCE` /
   `mysql_stmt_bind_param`). Without this: compile fails.
2. **`src/server/database/Database/DatabaseWorkerPool.cpp`** — MariaDB Connector/C
   reports client version 3.x, failing the `>= 8.0` and `client == compiled` fatal checks
   (wiki error ACE00043/46); also its server-version parser can't read `"11.8.6-MariaDB"`.
   Added a `LIBMARIADB` branch accepting Connector/C ≥ 3.2.3 and MariaDB server ≥ 10.5.
   Without this: worldserver aborts at startup.
3. **`modules/mod-ollama-chat/src/mod-ollama-chat_handler.cpp`** — the chat-history
   cleanup used `WITH … DELETE`, which MariaDB cannot parse; a failed statement is fatal.
   Rewritten as `DELETE … JOIN (derived table)`. Without this: worldserver crashes
   ~45 s after "ready" once bot chat history is pruned.

Debugging tip: a worldserver "segfault at 0 … error 6" in `dmesg` is AzerothCore's
`WPFatal` assertion (a deliberate null write), not memory corruption — the real message
is in `server/bin/Errors.log`.

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
