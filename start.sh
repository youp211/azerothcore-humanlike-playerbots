#!/usr/bin/env bash
# Start/stop/status for the realm (authserver + worldserver).
#
#   ./start.sh          start both in tmux sessions (with AC> consoles)
#   ./start.sh stop     graceful shutdown of both
#   ./start.sh status   processes, ports, sessions
#
# Consoles: tmux attach -t world   /   tmux attach -t auth   (detach: Ctrl-b d)
#
# Alternative: systemd units wow-auth/wow-world (no console, auto-start on
# boot, auto-restart on crash). Don't run both at once — this script refuses
# to start while the units are active.
set -euo pipefail

BIN=/home/admin/git/wow/server/bin

case "${1:-start}" in
start)
    if systemctl is-active --quiet wow-auth.service 2>/dev/null \
    || systemctl is-active --quiet wow-world.service 2>/dev/null; then
        echo "systemd units are active - stop them first: sudo systemctl stop wow-auth wow-world"
        exit 1
    fi
    if pgrep -x authserver > /dev/null; then
        echo "authserver already running"
    else
        tmux new-session -d -s auth -c "$BIN" ./authserver
        echo "authserver started (tmux session: auth)"
    fi
    if pgrep -x worldserver > /dev/null; then
        echo "worldserver already running"
    else
        tmux new-session -d -s world -c "$BIN" ./worldserver
        echo "worldserver started (tmux session: world) - first minutes: DB updates + bot logins"
    fi
    ;;
stop)
    if tmux has-session -t world 2>/dev/null; then
        tmux send-keys -t world 'server shutdown 5' Enter
        echo "sent graceful shutdown to worldserver console"
    else
        pkill -x worldserver 2>/dev/null && echo "worldserver killed (no console session)" || true
    fi
    sleep 8
    pkill -x authserver 2>/dev/null && echo "authserver stopped" || true
    tmux kill-session -t world 2>/dev/null || true
    tmux kill-session -t auth 2>/dev/null || true
    ;;
status)
    printf "authserver:  %s\n" "$(pgrep -x authserver > /dev/null && echo RUNNING || echo down)"
    printf "worldserver: %s\n" "$(pgrep -x worldserver > /dev/null && echo RUNNING || echo down)"
    printf "ports:       %s\n" "$(ss -tln | grep -cE ':(3724|8085) ') listening of 2"
    printf "tmux:        %s\n" "$(tmux ls 2>/dev/null | tr '\n' ' ' || echo none)"
    systemctl is-active --quiet wow-world.service 2>/dev/null && echo "systemd:     wow-world ACTIVE" || true
    ;;
*)
    echo "usage: $0 [start|stop|status]"
    exit 1
    ;;
esac
