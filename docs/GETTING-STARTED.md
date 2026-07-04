# Getting Started — Overview & Install Guide

## What this is

A private **World of Warcraft: Wrath of the Lich King (3.3.5a)** server that
you run on your own hardware — except the world isn't empty. It's populated by
**500 autonomous bots that behave like real 2008 players**: they quest, grind,
travel, rest at inns, run dungeons and battlegrounds, buy and sell on the
auction house, form guilds, make friends and rivals, and **talk to you** —
each in a distinct personality, through a locally-run, fine-tuned language
model. No cloud, no subscription, no other players required for the world to
feel alive.

It's built on [AzerothCore](https://www.azerothcore.org/) (the Playerbot fork)
plus two community modules that I've extended heavily.

## What makes it feel alive

- **75 personalities.** Every bot has a stable persona — the LFG spammer, the
  stoic paladin, the unhinged troll, the e-girl, the 2200-rated arena elitist,
  the mom who just wants everyone to have fun. It shapes how talkative they are,
  their tone, and how they treat you.
- **Personalities drive *gameplay*, not just chat.** Grinders farm mobs,
  socializers loiter in cities, idlers nap at inns, PvPers hunt world PvP.
- **A real economy.** Bots inspect your gear and react in character — a kind
  one mails you a spare item, a merchant sells it to you, an elitist tells you
  to get good. Bots list their own loot on the auction house at
  personality-driven prices.
- **Relationships that evolve.** Duel someone and they cool on you; group up or
  share a guild and they warm up. Bots remember meaningful moments with you
  (that arena you ran together) and bring them up later.
- **Living guilds.** Guilds *emerge over time* on their own, led by fitting
  personalities and named by the LLM. They recruit you as you play — and if you
  run with a party of bots and it goes well, that party can spontaneously *become*
  a guild.
- **An authentic voice.** Chat runs through `wow-chat`, a QLoRA fine-tune of
  Qwen3-4B trained to talk like a terse, lazy-caps 2008 player — served locally
  on an AMD GPU at ~0.2 s per reply.

Full behavior details: [BOT-BEHAVIOR.md](BOT-BEHAVIOR.md) and
[BOT-ECONOMY.md](BOT-ECONOMY.md). Code-level reference: [internals/](internals/README.md).

## What you need

- **Linux** (built and run on LMDE 7 / Debian 13; any modern Debian-family
  distro works). ~64 GB free disk.
- **CPU + RAM:** a modern multi-core CPU and **32 GB RAM** (the worldserver
  with 500 bots grows to ~8–11 GB; the build peaks high).
- **A GPU for the language model** — an **AMD RX 7900 XT** (ROCm) is what this
  was built on (~114 tok/s). An NVIDIA card works too (Ollama supports CUDA);
  CPU-only inference works but is slow. Without any GPU you can still run the
  server — chat is just sluggish.
- A **WoW 3.3.5a (build 12340) client** you provide (needed only to play, not
  to run the server).

## Install, step by step

Everything lives under one directory (here `~/git/wow`). Commands assume you're
in it.

**1. System packages.** Install the toolchain + libraries:
```bash
sudo apt install -y $(grep -E '^[a-z]' DEPENDENCIES.txt | tr '\n' ' ')
```
(That's git, cmake, clang, boost, MariaDB, libssl, libfmt, etc. — the full list
with notes is in `DEPENDENCIES.txt`.)

**2. Build the server.** `build.sh` clones the core fork + both modules (first
run) and compiles them with clang into `server/`:
```bash
./build.sh          # ~35 min the first time
```
> **Then apply our patches** — the modules need local changes to build on
> MariaDB and to add all the custom behavior. This is required:
> ```bash
> cd azerothcore-wotlk        && git apply ../patches/azerothcore-mariadb-compat.patch
> cd modules/mod-ollama-chat  && git apply ../../../patches/mod-ollama-chat-enhancements.patch
> cd ../mod-playerbots        && git apply ../../../patches/mod-playerbots-playstyles.patch
> cd ../../.. && ./build.sh   # rebuild with the patches
> ```
> Details and how to regenerate them: `patches/README.md`.

**3. Database (MariaDB).** Create the `acore` user and the four databases:
```bash
sudo mariadb < azerothcore-wotlk/data/sql/create/create_mysql.sql
sudo mariadb -e "CREATE DATABASE IF NOT EXISTS acore_playerbots;
  GRANT ALL ON acore_playerbots.* TO 'acore'@'localhost'; FLUSH PRIVILEGES;"
```
Content and schema import themselves on first worldserver boot
(`Updates.AutoSetup=1`). Recommended InnoDB tuning goes in
`/etc/mysql/mariadb.conf.d/60-azerothcore.cnf` (6 GB buffer pool — see the
README).

**4. Game data.** Download the pre-extracted maps/vmaps/mmaps/dbc (needed for
bot navigation) and unzip into `server/data/`:
```
https://github.com/wowgaming/client-data/releases/download/v19/Data.zip  (~1.2 GB)
```

**5. The language model (Ollama).** Install Ollama, then get a chat model:
```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull llama3.2:3b        # simple starting model
```
To run the **fine-tuned `wow-chat`** voice instead (recommended, much better in
character): the whole pipeline — dataset generator, QLoRA training on the GPU,
GGUF export, and deployment — is in `finetune/` and `gpu-box/` with their own
READMEs. Point the chat module at your model in
`server/etc/modules/mod_ollama_chat.conf` (`OllamaChat.Model`).

**6. First boot.** The cleanest way to start a fresh world (creates the bots,
loads all 75 personalities before they're assigned, sets up the GM account):
```bash
./reset-world.sh          # asks for confirmation, then does everything
```
First boot creates ~1000 bot characters — a few minutes of console spam that
looks like a hang but isn't. When bots start appearing, you're live.

**7. Secrets (optional).** Copy `.env.example` to `.env` to set your GM
account name/password and realm name; scripts fall back to `admin` /
`changeme123` / `Gigi` otherwise.

## Playing

- Point your 3.3.5a client's `Data/enUS/realmlist.wtf` at the server
  (`set realmlist <server-ip>`), log in with the GM account, create a
  character, and `/who` to see the bots.
- On the *same* machine, `./install-shortcut.sh` creates a desktop launcher
  that self-heals the realm address and starts the Wine client.
- Then just play — say hi to bots, group up, and watch the world react. The
  in-game test tour is in [TESTING.md](TESTING.md).

## Day-to-day operation

- **Start/stop:** `sudo systemctl start|stop wow-auth wow-world` (boot
  autostart), or `./start.sh` for tmux consoles. Details in the
  [README](../README.md).
- **Start the world over:** `./reset-world.sh` (add `--full` to also re-import
  static content).
- **Update the server:** `./build.sh --update`, then re-apply the three
  patches, then restart.

## Where to go next

| Doc | For |
|---|---|
| [README](../README.md) | Operating reference (running, config, accounts) |
| [BOT-BEHAVIOR](BOT-BEHAVIOR.md) | How every bot system behaves |
| [BOT-ECONOMY](BOT-ECONOMY.md) | The economy & social layer |
| [TESTING](TESTING.md) | In-game test checklist |
| [internals/](internals/README.md) | Function-level code reference |
| [BUILD-NOTES](BUILD-NOTES.md) | Full build history, every problem & fix |
