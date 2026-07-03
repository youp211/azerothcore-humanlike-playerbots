#!/usr/bin/env bash
# Completely wipe the realm and start over: all characters, bots, accounts,
# guilds, auction house, chat history, sentiment - gone. The world re-seeds
# itself on the next boot (fresh level-1 bots), and the 'admin' GM account is
# recreated automatically (password: changeme123).
#
#   ./reset-world.sh          wipe auth + characters + playerbots DBs
#   ./reset-world.sh --full   also drop acore_world (static content re-imports
#                             from SQL on boot; adds ~5-15 min, rarely needed)
#
# DESTRUCTIVE AND IRREVERSIBLE. Asks for confirmation.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FULL=0
[ "${1:-}" = "--full" ] && FULL=1

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

echo "== Starting servers (first boot re-imports everything; RNDBOT creation"
echo "   takes 10-40 min of console spam - it is NOT hung) =="
"$ROOT/start.sh" start

echo "== Waiting for the auth schema, then recreating the admin account =="
until sudo mariadb -N -e "SELECT 1 FROM acore_auth.account LIMIT 1;" > /dev/null 2>&1; do
    pgrep -x worldserver > /dev/null || { echo "worldserver died during import - check server/bin/Errors.log"; exit 1; }
    sleep 15
done

python3 - <<'EOF'
import hashlib, os, subprocess
# AzerothCore SRP6: v = g^SHA1(salt || SHA1(USER:PASS)) mod N, little-endian
N = int("894B645E89E1535BBDAD5B8B290650530801B18EBFBF5E8FAB3C82872A3E9BB7", 16)
user, pw = "ADMIN", "CHANGEME123"
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
print("account 'admin' recreated (password: changeme123, GM level 3)")
EOF

echo "== Restoring realm name/address =="
sudo mariadb -e "UPDATE acore_auth.realmlist SET name='Gigi', address='127.0.0.1' WHERE id=1;"

echo
echo "Wipe complete. The world is re-seeding: watch bots appear with"
echo "  sudo mariadb -N -e \"SELECT COUNT(*) FROM acore_characters.characters WHERE online=1;\""
echo "Log in as admin / changeme123 once bots start showing up."
