# Operations: reset, services, client — Developer Internals

Companion to [BOT-BEHAVIOR](../BOT-BEHAVIOR.md) and [BOT-ECONOMY](../BOT-ECONOMY.md)
(behavior-level framing) and [BUILD-NOTES](../BUILD-NOTES.md) (chronological
journal). Those explain *what* the realm does; this explains, step by step, the
five shell/unit files that **boot, wipe, cycle, and connect to** the realm, for
a developer who will edit or debug them.

Files covered (all paths absolute from repo root `/home/admin/git/wow`):

| file | role |
|---|---|
| `reset-world.sh` | one-shot destructive wipe → boot → reseed → GM/ahbot/realm restore |
| `start.sh` | tmux-mode start/stop/status, with a systemd mutual-exclusion guard |
| `launch-client.sh` | self-healing client launcher (the desktop shortcut runs this) |
| `install-shortcut.sh` | (re)installs the `.desktop` entry + extracts the icon |
| `systemd/wow-world.service`, `systemd/wow-auth.service` | headless auto-start/auto-restart units |
| `.env` / `.env.example` | GM + realm secrets sourced by `reset-world.sh` |

---

## 1. Purpose

This subsystem is the realm's **lifecycle and connectivity plumbing**: it wipes
and re-seeds the world from empty, starts/stops the two AzerothCore daemons two
different ways (interactive tmux vs headless systemd), and gets a WoW 3.3.5a
client onto the realm It exists because the fresh
world re-imports and re-seeds itself on first boot (static content, 500 level-1
bots), which turns "wipe and start over" from a manual multi-step DB dance into
`./reset-world.sh`.

---

## 2. Entry points & call graph

Four human/OS entry points, no long-running code of its own — everything is a
linear shell flow plus two embedded Python SRP6 blocks.

**`reset-world.sh [--full] [--no-start]`** (destructive, interactive):

```
reset-world.sh
 ├─ source $ROOT/.env  → GM_ACCOUNT / GM_PASSWORD / REALM_NAME (+ WOW_AHBOT_ACCOUNT later)
 ├─ parse args         → FULL, NO_START
 ├─ read -p "Type WIPE" (abort unless == WIPE)
 ├─ STOP  sudo systemctl stop wow-world wow-auth
 │        start.sh stop
 │        for i in 1..24: pgrep -x worldserver || break; sleep 5   (else exit 1)
 ├─ DROP  DROP DATABASE acore_auth/characters/playerbots [+world if --full]
 │        CREATE DATABASE … + GRANT … TO 'acore'@'localhost'
 ├─ [--no-start] → exit 0   (tail below is skipped)
 ├─ BOOT  start.sh start                       (tmux sessions: auth, world)
 ├─ WAIT  until SELECT gear_give_chance FROM …personality_templates   (liveness-guarded)
 │        └─ sudo mariadb acore_characters < personalities.sql
 ├─ WAIT  until SELECT 1 FROM acore_auth.account                       (liveness-guarded)
 │        └─ pgrep -x authserver || tmux new-session -d -s auth ./authserver   (first-boot race)
 ├─ GM    python3 SRP6 → INSERT account + account_access (gmlevel 3, RealmID -1)
 ├─ AHBOT python3 SRP6 → INSERT account (deterministic id 2, random pw)
 ├─ REALM REALM_IP = ip -4 route get 1.1.1.1 → UPDATE realmlist name/address/localAddress
 └─ RELOAD tmux has-session world && tmux send-keys -t world 'ollama reload'
```

**`start.sh {start|stop|status}`** — dispatched by a `case "${1:-start}"`:

```
start   → guard: systemctl is-active wow-auth||wow-world → exit 1 if either active
          pgrep authserver  || tmux new-session -d -s auth  ./authserver
          pgrep worldserver || tmux new-session -d -s world ./worldserver
stop    → tmux has-session world ? send-keys 'server shutdown 5' : pkill -x worldserver
          sleep 8 ; pkill -x authserver ; tmux kill-session world/auth
status  → pgrep reports ; ss -tln :(3724|8085) ; tmux ls ; systemctl is-active wow-world
```

**`launch-client.sh`** (desktop shortcut `Exec=`):

```
launch-client.sh
 ├─ grep gigi.local /etc/hosts || sudo -n tee -a /etc/hosts  ("127.0.0.1 gigi.local")
 ├─ echo "set realmlist gigi.local" > client/wotlk/Data/enUS/realmlist.wtf
 ├─ IP = ip -4 route get 1.1.1.1
 │   CUR = SELECT address FROM realmlist ; if CUR != IP:
 │     UPDATE realmlist SET address=IP ; sudo -n systemctl try-restart wow-auth ; sleep 2
 ├─ rm -rf client/wotlk/Cache/WDB
 └─ exec wine client/wotlk/Wow.exe   (WINEPREFIX=~/.wine-wow)
```

**`install-shortcut.sh`** (run once / after a repo move):

```
install-shortcut.sh
 ├─ wrestool -x -t14 Wow.exe → icotool -x → biggest wow_*.png → $ICON_DIR/wow-gigi.png
 ├─ heredoc → $APP_DIR/wow-gigi.desktop  (Exec=$ROOT/launch-client.sh, Path, Icon)
 └─ cp to ~/Desktop + gio metadata::trusted + update-desktop-database
```

**systemd** (the *other* start path — boot target, not a script): `multi-user.target`
→ `wow-auth.service` → `wow-world.service` (ordered `After=… wow-auth.service`).

---

## 3. Function-by-function

Shell scripts have no named functions here (except the embedded Python); the
units of work are the stages above. Each is documented with the real command
lines, variables, and non-obvious logic.

### 3.1 `reset-world.sh`

**Header / config resolution** (lines 13–21)

```bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$ROOT/.env" ] && . "$ROOT/.env"
GM_ACCOUNT="${WOW_GM_ACCOUNT:-admin}"
GM_PASSWORD="${WOW_GM_PASSWORD:-changeme123}"
REALM_NAME="${WOW_REALM_NAME:-Gigi}"
```

`ROOT` is resolved from `BASH_SOURCE` so the script is location-independent.
`.env` is *sourced* (dotted in), so any variable it defines enters the
environment; the three `${VAR:-default}` expansions give working defaults when
`.env` is absent (the repo ships none — it's gitignored). `set -euo pipefail`
makes the whole run fail-fast: any non-zero command (except those explicitly
`|| true`'d) aborts, unset variables error, pipe failures propagate.

**Argument parsing** (lines 23–31) — two boolean flags via a `for arg` /
`case` loop: `--full` sets `FULL=1` (adds `acore_world` to the drop list),
`--no-start` sets `NO_START=1` (wipe only, leave the realm down). Any other
token prints `unknown option` and `exit 1`.

**Confirmation gate** (lines 33–36)

```bash
read -r -p "Type WIPE to continue: " answer
[ "$answer" = "WIPE" ] || { echo "aborted"; exit 1; }
```

The only interactive prompt. It requires the literal string `WIPE` (not just
`y`), because the operation is irreversible.

**STOP stage** (lines 38–42)

```bash
sudo systemctl stop wow-world wow-auth 2>/dev/null || true
"$ROOT/start.sh" stop || true
for i in $(seq 1 24); do pgrep -x worldserver > /dev/null || break; sleep 5; done
pgrep -x worldserver > /dev/null && { echo "worldserver refuses to die - investigate"; exit 1; }
```

Stops **both** possible owners of the process — the systemd units *and* the
tmux sessions (`start.sh stop`) — because either could be running. Both are
`|| true`'d so a not-running unit/session isn't fatal. Then it polls for
worldserver's exit for up to **24 × 5 s = 120 s**; only `worldserver` is
polled (authserver is cheap to kill and has no DB write to flush). If the
process is still alive after 120 s it hard-fails rather than dropping the DBs
out from under a live server. Side effect: kills daemons, no DB change yet.

**DROP / recreate stage** (lines 44–60)

```bash
DBS="acore_auth acore_characters acore_playerbots"
[ "$FULL" = 1 ] && DBS="$DBS acore_world"
for db in $DBS; do sudo mariadb -e "DROP DATABASE IF EXISTS $db;"; done
sudo mariadb <<'EOF'
CREATE DATABASE IF NOT EXISTS acore_auth  … utf8mb4 COLLATE utf8mb4_unicode_ci;
… (characters, playerbots, world)
GRANT ALL PRIVILEGES ON acore_auth.* TO 'acore'@'localhost';
… FLUSH PRIVILEGES;
EOF
```

`acore_world` is dropped **only** with `--full` (default keeps it — static
content re-imports are slow, ~5–15 min, rarely needed). All four DBs are then
(re)created `IF NOT EXISTS` with charset `utf8mb4` / collation
`utf8mb4_unicode_ci` and re-granted to `'acore'@'localhost'`. `acore_world` is
always created even when not dropped, so a first-ever run has all four present.
The empty schemas are what worldserver's DB updater fills on the next boot.

**`--no-start` early exit** (lines 62–70) — prints that the realm is left down
and that the GM account / realm row / personality pool still need applying (the
tail of this same script), then `exit 0`. The wipe is complete but **none of
the reseed tail runs**.

**BOOT stage** (lines 72–74) — `"$ROOT/start.sh" start`. This launches under
**tmux** (see Section 3.2), which is deliberate: the reseed tail uses
`tmux send-keys -t world 'ollama reload'` and a tmux `authserver` relaunch, both
of which need tmux-owned sessions.

**Personality-schema wait + load** (lines 76–87)

```bash
until sudo mariadb -N -e "SELECT gear_give_chance FROM acore_characters.mod_ollama_chat_personality_templates LIMIT 1;" > /dev/null 2>&1; do
    pgrep -x worldserver > /dev/null || { echo "worldserver died during import - check server/bin/Errors.log"; exit 1; }
    sleep 5
done
sudo mariadb acore_characters < "$ROOT/personalities.sql"
echo "personalities loaded: $(sudo mariadb -N -e "SELECT COUNT(*) FROM acore_characters.mod_ollama_chat_personality_templates;") templates"
```

The wipe dropped the module's personality templates along with the characters
DB; worldserver's migration updater recreates them at some point during first
boot. This loop busy-waits until they exist, then applies `personalities.sql`
(the 42 custom archetypes). **Non-obvious:** it probes the *newest migration's
column* `gear_give_chance`, not merely the table. `personalities.sql` does not
`INSERT` that column — both `INSERT` statements use an identical fixed 7-column
list (`key`,`prompt`,`manual_only`,`weight`,`reply_chance_multiplier`,
`num_predict_override`,`temperature_override`); `playstyle` and
`gear_give_chance` are set afterward by separate `UPDATE` statements. Those
UPDATEs would still fail if the table existed but the latest migration hadn't
added the column yet, so probing `gear_give_chance` guards the whole load
against a half-migrated table. This is the "apply personalities
before first roll" guarantee: templates are complete before any bot logs in and
rolls its weighted assignment (assignments are stable for life — see
[BOT-BEHAVIOR Section 2](../BOT-BEHAVIOR.md)). Each iteration also checks worldserver
liveness with `pgrep`, so a crashed import aborts the wait instead of spinning
forever.

**Auth-schema wait** (lines 89–93) — the same `until` pattern on
`SELECT 1 FROM acore_auth.account`, `sleep 15` between tries, same
worldserver-liveness guard. Confirms the auth DB is populated before writing
accounts into it.

**Authserver first-boot-race relaunch** (lines 96–102)

```bash
if ! pgrep -x authserver > /dev/null; then
    tmux kill-session -t auth 2>/dev/null || true
    tmux new-session -d -s auth -c "$ROOT/server/bin" ./authserver
    echo "authserver relaunched (lost the first-boot DB race)"
fi
```

On the first boot into an empty auth DB, authserver and worldserver's importer
race to populate the Login database; authserver can lose and exit with *"Could
not populate the Login database"*. Now that the schema exists (the wait above
proved it), if authserver is gone it's relaunched in a fresh tmux `auth`
session. Non-obvious: only reachable *after* the auth-schema wait, so the
relaunched authserver finds a ready DB and stays up.

**GM account creation** (lines 104–123) — a Python heredoc doing AzerothCore
SRP6 verifier math (see Section 5 for the algorithm). Emits two statements in one
`sudo mariadb -e`:

```sql
INSERT INTO acore_auth.account (username, salt, verifier, expansion)
  VALUES ('<USER>', 0x…, 0x…, 2)
  ON DUPLICATE KEY UPDATE salt=0x…, verifier=0x…;
INSERT INTO acore_auth.account_access (id, gmlevel, RealmID)
  SELECT id, 3, -1 FROM acore_auth.account WHERE username='<USER>'
  ON DUPLICATE KEY UPDATE gmlevel=3;
```

`expansion=2` = WotLK. `gmlevel=3` = full GM, `RealmID=-1` = all realms. The
`account_access` insert **re-selects the account id by `username`** rather than
assuming it — it never hardcodes the id, so it's correct whether the account is
new or updated. `ON DUPLICATE KEY UPDATE` on both makes the whole thing
idempotent (re-running rotates the salt/verifier but keeps one row).

**ahbot service account** (lines 125–140) — a second, nearly identical SRP6
heredoc, but with the env vars *reused* for different values:

```bash
GM_ACCOUNT="${WOW_AHBOT_ACCOUNT:-ahbot}" GM_PASSWORD="ahbot$(od -An -N4 -tx4 /dev/urandom | tr -d ' ')" python3 - <<'EOF' … EOF
```

It writes only the `account` row (no `account_access` — it's not a GM), with a
**throwaway random password** (`/dev/urandom` hex) since nobody logs into it,
and `ON DUPLICATE KEY UPDATE username=username` (a deliberate no-op so a re-run
doesn't rotate its credentials). **Determinism contract:** it's created
*immediately after* the GM account, so on a fresh wipe its auto-increment id is
**2** — which `mod_ahbot.conf`'s `AuctionHouseBot.Account` relies on. Break the
ordering and mod-ah-bot points at the wrong account.

**Realm row restore** (lines 142–145)

```bash
REALM_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')"
sudo mariadb -e "UPDATE acore_auth.realmlist SET name='$REALM_NAME', address='${REALM_IP:-127.0.0.1}', localAddress='127.0.0.1' WHERE id=1;"
```

`address` is derived at run time from
the kernel's chosen source IP toward `1.1.1.1` (a route lookup, no packet sent;
`awk '{print $7}'` picks the `src` field). `${REALM_IP:-127.0.0.1}` falls back
to loopback if the route lookup returns nothing. `localAddress='127.0.0.1'` is
what sends **loopback clients back to loopback** for the world server — the
piece that makes the `gigi.local` client path (Section 3.3) work regardless of the
external IP.

**Template hot-reload** (lines 148–151)

```bash
if tmux has-session -t world 2>/dev/null; then
    tmux send-keys -t world 'ollama reload' Enter
fi
```

In case the chat module already read the templates before `personalities.sql`
landed, it pokes the worldserver console (dotless `ollama reload` — console
syntax, not the in-game `.ollama`) to hot-reload *templates*. Safe live because
reload never touches per-bot *assignments*. Guarded by `has-session` so it's a
no-op if not in tmux mode.

### 3.2 `start.sh`

Single `case "${1:-start}"` dispatch (default subcommand is `start`).
`BIN=/home/admin/git/wow/server/bin` is hardcoded.

**`start`** (lines 18–36)

```bash
if systemctl is-active --quiet wow-auth.service 2>/dev/null \
|| systemctl is-active --quiet wow-world.service 2>/dev/null; then
    echo "systemd units are active - stop them first: sudo systemctl stop wow-auth wow-world"
    exit 1
fi
```

The **mutual-exclusion guard**: if either systemd unit is active, it refuses to
start a competing tmux copy (two worldservers on the same DB/ports would be a
disaster). Otherwise it starts each daemon only if not already running
(`pgrep -x`), each in its own detached tmux session:
`tmux new-session -d -s auth -c "$BIN" ./authserver` and likewise `-s world`.
The `-d` detaches (background); attach later with `tmux attach -t world`.

**`stop`** (lines 37–48) — prefers a **graceful** shutdown: if the `world`
tmux session exists, `tmux send-keys -t world 'server shutdown 5' Enter` (the
worldserver's own 5-second countdown, which flushes state); otherwise
`pkill -x worldserver`. Then `sleep 8`, `pkill -x authserver`, and kill both
tmux sessions (`|| true`). Note the asymmetry: worldserver gets a clean
shutdown, authserver is just killed (stateless enough to not need flushing).

**`status`** (lines 49–55) — pure reporting, no state change:
`pgrep -x` for each daemon, a listening-port count
`ss -tln | grep -cE ':(3724|8085) '` ("N listening of 2"), `tmux ls`, and a
`systemctl is-active --quiet wow-world` line that prints `systemd: wow-world
ACTIVE` only when systemd owns it. Ports **3724** = authserver, **8085** =
worldserver.

### 3.3 `launch-client.sh`

`set -u` only — deliberately **not** `-e`/`pipefail`. A self-healing launcher
must tolerate individual heal steps failing (no passwordless sudo, DB down)
and still launch the game; every heal is `|| true`'d or uses `sudo -n`.

**Step 1 — name pinning** (lines 22–24)

```bash
grep -q "gigi.local" /etc/hosts 2>/dev/null || \
    echo "127.0.0.1 gigi.local" | sudo -n tee -a /etc/hosts > /dev/null 2>&1 || true
echo "set realmlist gigi.local" > "$CLIENT/Data/enUS/realmlist.wtf"
```

Idempotently appends `127.0.0.1 gigi.local` to `/etc/hosts` (only if missing)
and overwrites the client's `realmlist.wtf` to point at that hostname. The
client therefore always connects to `gigi.local`, which resolves to loopback —
`sudo -n` (non-interactive) means if passwordless
sudo isn't available it silently skips the hosts edit rather than prompting.

**Step 2 — realm external-address refresh** (lines 27–35)

```bash
IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')
if [ -n "${IP:-}" ]; then
    CUR=$(sudo -n mariadb -N -e "SELECT address FROM acore_auth.realmlist WHERE id=1;" 2>/dev/null)
    if [ -n "$CUR" ] && [ "$CUR" != "$IP" ]; then
        sudo -n mariadb -e "UPDATE acore_auth.realmlist SET address='$IP' WHERE id=1;"
        sudo -n systemctl try-restart wow-auth 2>/dev/null || true
        sleep 2
    fi
fi
```

Same route-lookup IP derivation as reset. Updates `realmlist.address` **only if
it drifted** (`CUR != IP`). This address matters only for *other* devices on the
LAN — the local client uses `gigi.local` — but keeping it current lets phones
etc. connect. Non-obvious: it uses `systemctl try-restart` (not `restart`),
which restarts the unit **only if it's currently active**. Under tmux mode the
`wow-auth` unit is inactive, so `try-restart` is a graceful no-op and the tmux
authserver isn't disturbed; under systemd mode it picks up the new realm
address. authserver caches the realm address at startup, hence the bounce.

**Step 3 — WDB cache clear** (line 38) — `rm -rf "$CLIENT/Cache/WDB"`. Stale
cached server data from a previous world/wipe hangs the login handshake; wiping
`Cache/WDB` forces a clean re-fetch.

**Launch** (line 40) — `exec env XMODIFIERS="@im=none"
WINEPREFIX="$HOME/.wine-wow" wine "$CLIENT/Wow.exe"`. `exec` replaces the shell
(no lingering wrapper process); `XMODIFIERS="@im=none"` disables input-method
interference with the game's keyboard handling; the WoW-specific Wine prefix is
`~/.wine-wow`.

### 3.4 `install-shortcut.sh`

`set -euo pipefail`. Bakes the *current* repo location into a `.desktop` entry
so the shortcut survives a repo move (re-run after moving).

**Icon extraction** (lines 13–24) — only if `wow-gigi.png` doesn't already
exist and `wrestool`+`icotool` (the `icoutils` package) are present:
`wrestool -x -t14 … Wow.exe -o …/wow.ico` pulls resource type 14 (group icon)
out of the PE binary, `icotool -x` explodes it to PNGs, `ls -S … | head -1`
picks the largest. Missing `icoutils` → a printed note and a generic icon
(everything `|| true`'d).

**Desktop entry** (lines 26–36) — a heredoc writing
`$HOME/.local/share/applications/wow-gigi.desktop` with
`Exec=$ROOT/launch-client.sh`, `Path=$ROOT/client/wotlk`, and the extracted
`Icon=`. `$ROOT` is interpolated so the entry is absolute and repo-relative.

**Desktop placement** (lines 38–43) — if `~/Desktop` exists, copies the entry
there, `chmod +x`, and marks it trusted with `gio set … metadata::trusted true`
(so GNOME doesn't nag). `update-desktop-database` refreshes the menu cache.
Both trailing calls `|| true` so a non-GNOME desktop doesn't break the install.

### 3.5 systemd units

`wow-auth.service` and `wow-world.service` — the **headless** alternative to
`start.sh` (no console, auto-start on boot, auto-restart on crash).

`wow-auth.service`:
```
After=network-online.target mariadb.service
ExecStartPre=/bin/sh -c '! pgrep -x authserver'
ExecStart=/home/admin/git/wow/server/bin/authserver
Restart=on-failure ; RestartSec=5
```

`wow-world.service`:
```
After=network-online.target mariadb.service ollama.service wow-auth.service
ExecStartPre=/bin/sh -c '! pgrep -x worldserver'
ExecStart=/home/admin/git/wow/server/bin/worldserver
Restart=on-failure ; RestartSec=10 ; TimeoutStopSec=90 ; LimitNOFILE=65536
```

Key mechanics: both run as `User=admin` with `WorkingDirectory` = the bin dir.
`ExecStartPre=/bin/sh -c '! pgrep -x <daemon>'` is a **double-start guard** — if
the daemon is already running (e.g. under tmux), `pgrep` succeeds, `!` inverts
it to failure, and systemd refuses to start a second copy (the mirror of
`start.sh`'s guard). Ordering: world is `After=… wow-auth.service` **and**
`ollama.service` (chat needs the LLM up) and `mariadb.service`. `Restart=on-failure`
gives crash-recovery; `TimeoutStopSec=90` on world allows the long save-flush on
shutdown; `LimitNOFILE=65536` because 500 bots + client sockets exhaust the
default fd limit.

---

## 4. Data structures & DB

**Shell "globals"** (`reset-world.sh` unless noted): `ROOT` (script dir),
`FULL`/`NO_START` (arg flags), `GM_ACCOUNT`/`GM_PASSWORD`/`REALM_NAME`
(from `.env`), `DBS` (space-list of databases to drop), `REALM_IP` (derived),
plus in the Python blocks `N` (SRP6 modulus), `salt`, `h`, `v`. In
`launch-client.sh`: `CLIENT` (client dir), `IP`, `CUR`. In `start.sh`: `BIN`.

**Databases** created/granted: `acore_auth`, `acore_characters`,
`acore_playerbots`, `acore_world` — charset `utf8mb4`, collation
`utf8mb4_unicode_ci`, all `GRANT ALL … TO 'acore'@'localhost'`.

**Tables & columns touched:**

| table | columns | op | where |
|---|---|---|---|
| `acore_characters.mod_ollama_chat_personality_templates` | `gear_give_chance` (probe), `COUNT(*)` | read | reset schema-wait + count |
| — (same, via `personalities.sql`) | `key`,`prompt`,`manual_only`,`weight`,`reply_chance_multiplier`,`num_predict_override`,`temperature_override` (insert); `playstyle`,`gear_give_chance` (update) | delete+insert+update | reset load |
| `acore_auth.account` | `username`,`salt`,`verifier`,`expansion` | insert / on-dup-update | GM + ahbot |
| `acore_auth.account_access` | `id`,`gmlevel`,`RealmID` | insert…select / on-dup-update | GM only |
| `acore_auth.realmlist` | `name`,`address`,`localAddress` (`WHERE id=1`) | read (`address`) + update | reset restore + client refresh |

The `updates` table in each DB is written implicitly by worldserver's migration
updater on boot (`Updates.AutoSetup=1`); the reset script's schema-wait loops
depend on that updater having run.

**Files written / removed:** `/etc/hosts` (append `127.0.0.1 gigi.local`),
`client/wotlk/Data/enUS/realmlist.wtf` (overwrite), `client/wotlk/Cache/WDB`
(`rm -rf`), `~/.local/share/icons/wow-gigi.png`,
`~/.local/share/applications/wow-gigi.desktop` (+ a copy on `~/Desktop`).
Wine state lives in `~/.wine-wow`.

---

## 5. Concurrency & threading

No application threads — this is shell/systemd — but there are three real
concurrency concerns:

1. **worldserver vs authserver first-boot DB race.** On an empty `acore_auth`,
   worldserver's importer and authserver both try to populate the Login DB;
   authserver can lose and exit. Handled by the *serialized* reset flow: wait
   until `acore_auth.account` exists (proof the DB is populated), *then*
   `pgrep -x authserver` and relaunch it if it died. Serialization, not a lock.

2. **Reset script vs worldserver import.** The two `until … sleep` loops are
   busy-wait polls against a *concurrently importing* worldserver. Each
   iteration re-checks `pgrep -x worldserver`, so if the import crashes the
   waiter aborts (`exit 1`) instead of hanging forever. The `personalities.sql`
   load is timed to land right after the migration updater creates the table
   but (ideally) before the first bot logs in and rolls — the whole point of
   probing `gear_give_chance` first.

3. **tmux mode vs systemd mode — mutual exclusion.** The realm can be owned by
   *either* the tmux sessions (`start.sh`) *or* the systemd units, never both:
   `start.sh start` refuses if a unit is active, and each unit's
   `ExecStartPre '! pgrep …'` refuses if a process already exists. **Reset runs
   in tmux mode** (it calls `start.sh start` after `systemctl stop`), so during
   and after a reset the daemons are tmux-owned. Consequence a developer must
   remember: `sudo systemctl restart wow-world` will **not** cycle a
   tmux-launched worldserver (systemd doesn't own the pid) — cycle it with
   `./start.sh stop && ./start.sh start`. This is also why `launch-client.sh`
   uses `systemctl try-restart wow-auth` (a no-op when the unit is inactive)
   rather than `restart`: it must not error out in tmux mode.

Idempotency stands in for locking on the DB writes: every `INSERT` uses
`ON DUPLICATE KEY UPDATE`, so a re-run or a concurrent re-apply converges to one
row rather than erroring or duplicating.

---

## 6. Config keys

The ops layer reads **no `sConfigMgr` options** — it is shell, not module C++.
Its configuration surface is environment variables plus a handful of *other*
subsystems' config values it must stay consistent with.

**Environment variables** (sourced from `.env`; defaults in `reset-world.sh`):

| var | default | consumer | meaning |
|---|---|---|---|
| `WOW_GM_ACCOUNT` | `admin` | reset GM block | GM account username (uppercased for SRP6) |
| `WOW_GM_PASSWORD` | `changeme123` | reset GM block | GM password |
| `WOW_REALM_NAME` | `Gigi` | reset realm restore | `realmlist.name` |
| `WOW_AHBOT_ACCOUNT` | `ahbot` | reset ahbot block | auction-house service account name — **note: present in `reset-world.sh` but NOT in `.env.example`** |
| (ahbot password) | `ahbot<8 hex>` random | reset ahbot block | throwaway; account is never logged into |

**Cross-subsystem values ops depends on** (set in the referenced conf files, not
here — but a change to ops must respect them):

| value | file | why ops cares |
|---|---|---|
| `AuctionHouseBot.Account = 2` | `server/etc/modules/mod_ahbot.conf` | reset creates the ahbot account **second** so its id is deterministically 2 |
| `Updates.AutoSetup = 1` | `server/etc/worldserver.conf` | reset's schema-wait loops assume the boot-time DB updater runs |
| `realmlist` ports 3724 / 8085 | DB / core defaults | `start.sh status` counts listeners on these |
| `OLLAMA_KEEP_ALIVE`, `ollama.service` | Ollama env / unit | `wow-world.service` orders `After=ollama.service` |

Config for the two C++ modules (`OllamaChat.*`, `AiPlayerbot.*`) is documented
in [BOT-BEHAVIOR](../BOT-BEHAVIOR.md) / [BOT-ECONOMY](../BOT-ECONOMY.md); ops
only re-applies `personalities.sql` and pokes `ollama reload`.

**SRP6 verifier (the Python blocks), for reference** — AzerothCore's login
crypto, so a developer editing account creation gets it right:

```
N = 0x894B645E89E1535BBDAD5B8B290650530801B18EBFBF5E8FAB3C82872A3E9BB7   # 256-bit safe prime
g = 7
h = SHA1( salt || SHA1( "USER:PASS" ) )        # USER, PASS uppercased; salt = 32 random bytes
v = ( g ^ int(h, little-endian) ) mod N         # stored little-endian, 32 bytes
```

`salt`/`verifier` go into `account` as `0x…` hex literals; `expansion=2` (WotLK).

---

## 7. Failure modes & gotchas

- **worldserver refuses to die** — the 120 s (24×5 s) `pgrep` poll after stop;
  on timeout the script aborts (`exit 1`) rather than dropping DBs under a live
  server. Investigate a stuck worldserver by hand.
- **Import crash mid-wait** — both `until` loops re-check `pgrep -x worldserver`
  each iteration and `exit 1` with a *check `server/bin/Errors.log`* hint if it
  died, so a failed import can't spin forever.
- **Probe the newest column, not the table** — the personality wait selects
  `gear_give_chance` specifically. If a future migration adds another behavior
  column that `personalities.sql` writes, **update the probe to the new newest
  column**, or the load can race a half-migrated table and fail.
- **authserver first-boot DB race** — *"Could not populate the Login database"*;
  auto-recovered by the post-wait relaunch. If it recurs outside reset (e.g.
  systemd cold boot), the `After=… wow-auth.service` ordering plus
  `Restart=on-failure RestartSec=5` on the unit is the safety net.
  `ip -4 route get 1.1.1.1` every run; reset falls back to `127.0.0.1`
  (`${REALM_IP:-127.0.0.1}`) if the lookup is empty. The client sidesteps drift
  entirely via `gigi.local` → loopback + `localAddress=127.0.0.1`.
- **Stale client cache** — a WDB from a previous world hangs the login
  handshake; `launch-client.sh` `rm -rf`s `Cache/WDB` every launch. If a client
  won't get past "connecting" after a wipe, this is why.
- **`--no-start` leaves the tail un-run** — the GM account, ahbot account, realm
  row, and personality pool are **not** applied; you must re-run
  `reset-world.sh` *without* `--no-start` (or apply them manually) before the
  realm is usable.
- **ahbot id determinism** — relies on GM (id 1) being created immediately
  before ahbot (id 2) on a *fresh* auth DB. On a non-fresh DB the ids won't be
  1/2; `mod_ahbot.conf`'s `AuctionHouseBot.Account=2` would then point wrong.
  Reset-after-wipe is the supported path.
- **`sudo -n` in the launcher** — every privileged step in `launch-client.sh`
  is non-interactive and `|| true`'d, so on a system without passwordless sudo the
  `/etc/hosts` pin and realmlist refresh silently no-op; the game still
  launches. Debug by checking whether `/etc/hosts` actually contains the pin.
- **tmux vs systemd ownership confusion** — `start.sh status` shows which owns
  the realm; after a reset it's tmux, so use `start.sh` (not `systemctl`) to
  cycle. See Section 5.
- **`icoutils` optional** — `install-shortcut.sh` degrades to a generic icon if
  `wrestool`/`icotool` are missing; not fatal.
- **`.env` is gitignored** — the repo ships only `.env.example`; a checkout with
  no `.env` runs on the built-in defaults (`admin`/`changeme123`/`Gigi`).
  `WOW_AHBOT_ACCOUNT` has no `.env.example` line, so it always defaults to
  `ahbot` unless exported manually.

---

## 8. Cross-references

- [../BOT-BEHAVIOR.md](../BOT-BEHAVIOR.md) — Section 2 Personalities (why
  `personalities.sql` must load *before* the first bot rolls its assignment;
  `ollama reload` reloads templates not assignments), Section 3 Playstyles
  (`playstyle` column that reset's SQL supplies).
- [../BOT-ECONOMY.md](../BOT-ECONOMY.md) — the `gear_give_chance` column the
  reset schema-wait probes; Section 3 the organic bot AH and its coexistence with
  `mod-ah-bot` (the deterministic `ahbot` account reset creates); Section 5
  `Updates.AutoSetup=1` migration behavior.
- [../BUILD-NOTES.md](../BUILD-NOTES.md) — chronological history of the
  authserver DB-race fix, and the
  reset-before-first-login timing.
- [../TESTING.md](../TESTING.md) — the post-reset smoke checks.
- [../../README.md](../../README.md) — operating reference, directory layout,
  the realm address, and the tmux-vs-systemd overview.
- Companion documents in this `docs/internals/` series cover the C++
  subsystems that this ops layer boots and reseeds (the mod-ollama-chat handler
  and personality/sentiment tables, the mod-playerbots RPG/economy engine, and
  the SQL migration set) — this document is the entry point for *how the realm
  is started, wiped, and connected to*.
```