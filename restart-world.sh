#!/usr/bin/env bash
# Keep worldserver running; restarts it 10s after any crash/exit.
# Run inside tmux: tmux new -s world '/home/admin/git/wow/restart-world.sh'
cd /home/admin/git/wow/server/bin
while true; do
    ./worldserver
    echo "worldserver exited ($?), restarting in 10s — Ctrl-C to stop"
    sleep 10
done
