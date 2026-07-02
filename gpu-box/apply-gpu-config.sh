#!/usr/bin/env bash
# Run ON THE WOW SERVER: ./apply-gpu-config.sh <GPU_BOX_IP> [model]
# Points mod-ollama-chat at the GPU box and raises chat settings for GPU-speed inference.
# Applies live via the worldserver console (.ollama reload) - no restart needed.
set -euo pipefail

IP="${1:?usage: $0 <GPU_BOX_IP> [model]}"
MODEL="${2:-}"
CONF=/home/admin/git/wow/server/etc/modules/mod_ollama_chat.conf

if [ -z "$MODEL" ]; then
    MODEL=$(curl -s --max-time 5 "http://$IP:11434/api/tags" | python3 -c "import json,sys; ms=[m['name'] for m in json.load(sys.stdin)['models'] if 'qwen3' in m['name']]; print(ms[0] if ms else '')")
fi
[ -n "$MODEL" ] || { echo "No qwen3 model found on $IP - pass one explicitly as arg 2."; exit 1; }

cp "$CONF" "$CONF.bak.$(date +%s)"
sed -i \
  -e "s|^OllamaChat.Url = .*|OllamaChat.Url = http://$IP:11434/api/generate|" \
  -e "s|^OllamaChat.Model = .*|OllamaChat.Model = $MODEL|" \
  -e "s|^OllamaChat.MaxConcurrentQueries = .*|OllamaChat.MaxConcurrentQueries = 8|" \
  "$CONF"

# Enable typing simulation for realism now that replies are fast
grep -q "^OllamaChat.EnableTypingSimulation" "$CONF" \
  && sed -i "s|^OllamaChat.EnableTypingSimulation = .*|OllamaChat.EnableTypingSimulation = 1|" "$CONF" \
  || echo "OllamaChat.EnableTypingSimulation = 1" >> "$CONF"

echo "Applied: Url=http://$IP:11434, Model=$MODEL, MaxConcurrentQueries=8, TypingSimulation=1"

# Hot-reload if the worldserver is running in the 'world' tmux session
if tmux has-session -t world 2>/dev/null; then
    tmux send-keys -t world '.ollama reload' Enter
    echo "Sent .ollama reload to worldserver console."
else
    echo "worldserver tmux session not found - run '.ollama reload' in its console yourself."
fi

echo "Rollback: restore the newest $CONF.bak.* and reload again."
