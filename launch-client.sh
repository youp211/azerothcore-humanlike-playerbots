#!/usr/bin/env bash
# Self-healing WoW client launcher (the desktop shortcut runs this).
#
# The local client connects via the hostname `gigi.local` (pinned to loopback
# in /etc/hosts), which makes it immune to DHCP IP drift:
#   client -> gigi.local (127.0.0.1) -> authserver, and the realm row's
#   localAddress=127.0.0.1 sends loopback clients back to loopback for the
#   world server.
#
# Before starting the game this script:
#   1. ensures the /etc/hosts pin and the client realmlist point at gigi.local
#   2. updates acore_auth.realmlist's EXTERNAL address to the box's current IP
#      (only matters for other devices on the LAN) + bounces authserver if it
#      was stale
#   3. clears the client's stale server cache (Cache/WDB)
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLIENT="$ROOT/client/wotlk"

# 1. name pinning (idempotent)
grep -q "gigi.local" /etc/hosts 2>/dev/null || \
    echo "127.0.0.1 gigi.local" | sudo -n tee -a /etc/hosts > /dev/null 2>&1 || true
echo "set realmlist gigi.local" > "$CLIENT/Data/enUS/realmlist.wtf"

# 2. keep the realm row's external address current for LAN players
IP=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{print $7; exit}')
if [ -n "${IP:-}" ]; then
    CUR=$(sudo -n mariadb -N -e "SELECT address FROM acore_auth.realmlist WHERE id=1;" 2>/dev/null)
    if [ -n "$CUR" ] && [ "$CUR" != "$IP" ]; then
        sudo -n mariadb -e "UPDATE acore_auth.realmlist SET address='$IP' WHERE id=1;"
        sudo -n systemctl try-restart wow-auth 2>/dev/null || true
        sleep 2
    fi
fi

# 3. stale cache from previous worlds/wipes hangs the login handshake
rm -rf "$CLIENT/Cache/WDB"

exec env XMODIFIERS="@im=none" WINEPREFIX="$HOME/.wine-wow" wine "$CLIENT/Wow.exe"
