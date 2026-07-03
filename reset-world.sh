#!/usr/bin/env bash
# Completely wipe the realm and start over: all characters, bots, accounts,
# guilds, auction house, chat history, sentiment - gone. The world re-seeds
# itself on the next boot (fresh level-1 bots), the full personality pool is
# re-applied before bots roll their assignments, and the GM account is
# recreated automatically (name/password from .env, default admin/changeme123).
#
#   ./reset-world.sh          wipe auth + characters + playerbots DBs
#   ./reset-world.sh --full   also drop acore_world (static content re-imports
#                             from SQL on boot; adds ~5-15 min, rarely needed)
#
# DESTRUCTIVE AND IRREVERSIBLE. Asks for confirmation.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Secure/user-specific settings come from .env (gitignored; see .env.example)
[ -f "$ROOT/.env" ] && . "$ROOT/.env"
GM_ACCOUNT="${WOW_GM_ACCOUNT:-admin}"
GM_PASSWORD="${WOW_GM_PASSWORD:-changeme123}"
REALM_NAME="${WOW_REALM_NAME:-Gigi}"

FULL=0
NO_START=0
for arg in "$@"; do
    case "$arg" in
    --full)     FULL=1 ;;
    --no-start) NO_START=1 ;;   # wipe only; leave the realm down
    *) echo "unknown option: $arg"; exit 1 ;;
    esac
done

echo "This will PERMANENTLY DELETE all characters, bots, accounts and world state."
[ "$FULL" = 1 ] && echo "(--full: the static world DB will also be dropped and re-imported.)"
read -r -p "Type WIPE to continue: " answer
[ "$answer" = "WIPE" ] || { echo "aborted"; exit 1; }

echo "== Stopping servers =="
sudo systemctl stop wow-world wow-auth 2>/dev/null || true
"$ROOT/start.sh" stop || true
for i in $(seq 1 24); do pgrep -x worldserver > /dev/null || break; sleep 5; done
pgrep -x worldserver > /dev/null && { echo "worldserver refuses to die - investigate"; exit 1; }

echo "== Dropping databases =="
DBS="acore_auth acore_characters acore_playerbots"
[ "$FULL" = 1 ] && DBS="$DBS acore_world"
for db in $DBS; do
    sudo mariadb -e "DROP DATABASE IF EXISTS $db;"
done
sudo mariadb <<'EOF'
CREATE DATABASE IF NOT EXISTS acore_auth        DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS acore_characters  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS acore_playerbots  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS acore_world       DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
GRANT ALL PRIVILEGES ON acore_auth.*       TO 'acore'@'localhost';
GRANT ALL PRIVILEGES ON acore_characters.* TO 'acore'@'localhost';
GRANT ALL PRIVILEGES ON acore_playerbots.* TO 'acore'@'localhost';
GRANT ALL PRIVILEGES ON acore_world.*      TO 'acore'@'localhost';
FLUSH PRIVILEGES;
EOF

if [ "$NO_START" = 1 ]; then
    echo
    echo "Wiped. Realm left DOWN (--no-start). When you start it (./start.sh or"
    echo "systemd), the world re-imports and re-seeds itself; then the GM"
    echo "account, realm row, and the 73-personality pool still need applying -"
    echo "this script's tail does all three (or run it without --no-start next"
    echo "time for the full cycle)."
    exit 0
fi

echo "== Starting servers (first boot re-imports everything; RNDBOT creation"
echo "   takes 10-40 min of console spam - it is NOT hung) =="
"$ROOT/start.sh" start

echo "== Waiting for the personality templates table, then loading all 73 =="
# The wipe drops the module's personality templates with the rest of the
# characters DB; re-apply the 40 extra archetypes the moment the module's SQL
# updater recreates the table, so bots roll from the weighted 73 pool from the
# first login instead of the upstream 33 (which otherwise requires a
# clear-assignments-and-restart dance later).
until sudo mariadb -N -e "SELECT 1 FROM acore_characters.mod_ollama_chat_personality_templates LIMIT 1;" > /dev/null 2>&1; do
    pgrep -x worldserver > /dev/null || { echo "worldserver died during import - check server/bin/Errors.log"; exit 1; }
    sleep 5
done
sudo mariadb acore_characters < "$ROOT/personalities.sql"
echo "personalities loaded: $(sudo mariadb -N -e "SELECT COUNT(*) FROM acore_characters.mod_ollama_chat_personality_templates;") templates"

echo "== Waiting for the auth schema, then recreating the GM account =="
until sudo mariadb -N -e "SELECT 1 FROM acore_auth.account LIMIT 1;" > /dev/null 2>&1; do
    pgrep -x worldserver > /dev/null || { echo "worldserver died during import - check server/bin/Errors.log"; exit 1; }
    sleep 15
done

# On the first boot after a wipe, authserver can lose the race to populate the
# empty auth DB against worldserver's importer and exit ("Could not populate
# the Login database"). Now that the schema exists, bring it back if it died.
if ! pgrep -x authserver > /dev/null; then
    tmux kill-session -t auth 2>/dev/null || true
    tmux new-session -d -s auth -c "$ROOT/server/bin" ./authserver
    echo "authserver relaunched (lost the first-boot DB race)"
fi

GM_ACCOUNT="$GM_ACCOUNT" GM_PASSWORD="$GM_PASSWORD" python3 - <<'EOF'
import hashlib, os, subprocess
# AzerothCore SRP6: v = g^SHA1(salt || SHA1(USER:PASS)) mod N, little-endian
N = int("894B645E89E1535BBDAD5B8B290650530801B18EBFBF5E8FAB3C82872A3E9BB7", 16)
user = os.environ["GM_ACCOUNT"].upper()
pw = os.environ["GM_PASSWORD"].upper()
salt = os.urandom(32)
h = hashlib.sha1(salt + hashlib.sha1(f"{user}:{pw}".encode()).digest()).digest()
v = pow(7, int.from_bytes(h, "little"), N).to_bytes(32, "little")
sql = (
    f"INSERT INTO acore_auth.account (username, salt, verifier, expansion) "
    f"VALUES ('{user}', 0x{salt.hex()}, 0x{v.hex()}, 2) "
    f"ON DUPLICATE KEY UPDATE salt=0x{salt.hex()}, verifier=0x{v.hex()};"
    f"INSERT INTO acore_auth.account_access (id, gmlevel, RealmID) "
    f"SELECT id, 3, -1 FROM acore_auth.account WHERE username='{user}' "
    f"ON DUPLICATE KEY UPDATE gmlevel=3;"
)
subprocess.run(["sudo", "mariadb", "-e", sql], check=True)
print(f"GM account '{user.lower()}' recreated (GM level 3)")
EOF

echo "== Restoring realm name/address =="
# The box is on DHCP - derive the current IP rather than hardcoding one
REALM_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')"
sudo mariadb -e "UPDATE acore_auth.realmlist SET name='$REALM_NAME', address='${REALM_IP:-127.0.0.1}', localAddress='127.0.0.1' WHERE id=1;"

# In case the module read the templates before our insert landed, hot-reload
# them; template reload is safe live (assignments are unaffected).
if tmux has-session -t world 2>/dev/null; then
    tmux send-keys -t world 'ollama reload' Enter
fi

echo
echo "Wipe complete. The world is re-seeding: watch bots appear with"
echo "  sudo mariadb -N -e \"SELECT COUNT(*) FROM acore_characters.characters WHERE online=1;\""
echo "Log in as $GM_ACCOUNT once bots start showing up."
