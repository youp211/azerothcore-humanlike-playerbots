#!/usr/bin/env bash
# (Re)install the desktop + app-menu shortcut for the WoW client.
# Run it once, or again after moving this repo - it bakes the repo's current
# location into the launcher entry and extracts the icon from Wow.exe.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ICON_DIR="$HOME/.local/share/icons"
APP_DIR="$HOME/.local/share/applications"
mkdir -p "$ICON_DIR" "$APP_DIR"

# Icon: extract the biggest one embedded in Wow.exe (needs icoutils)
if [ ! -f "$ICON_DIR/wow-gigi.png" ]; then
    if command -v wrestool > /dev/null && command -v icotool > /dev/null; then
        tmp=$(mktemp -d)
        wrestool -x -t14 "$ROOT/wotlk/wotlk/Wow.exe" -o "$tmp/wow.ico" 2>/dev/null || true
        icotool -x "$tmp/wow.ico" -o "$tmp" 2>/dev/null || true
        biggest=$(ls -S "$tmp"/wow_*.png 2>/dev/null | head -1 || true)
        [ -n "$biggest" ] && cp "$biggest" "$ICON_DIR/wow-gigi.png"
        rm -rf "$tmp"
    else
        echo "note: icoutils not installed (sudo apt install icoutils) - shortcut will use a generic icon"
    fi
fi

cat > "$APP_DIR/wow-gigi.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=World of Warcraft (Gigi)
Comment=WotLK 3.3.5a - local realm (self-healing launcher)
Exec=$ROOT/launch-client.sh
Path=$ROOT/wotlk/wotlk
Icon=$ICON_DIR/wow-gigi.png
Categories=Game;
StartupNotify=false
EOF

if [ -d "$HOME/Desktop" ]; then
    cp "$APP_DIR/wow-gigi.desktop" "$HOME/Desktop/"
    chmod +x "$HOME/Desktop/wow-gigi.desktop"
    gio set "$HOME/Desktop/wow-gigi.desktop" metadata::trusted true 2>/dev/null || true
fi
update-desktop-database "$APP_DIR" 2>/dev/null || true

echo "Shortcut installed -> $ROOT/launch-client.sh"
