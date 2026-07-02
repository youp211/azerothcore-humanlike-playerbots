#!/usr/bin/env bash
# Run ON THE WOW SERVER: ./02-verify-lan.sh <GPU_BOX_IP>
# Confirms the GPU box's Ollama is reachable over the LAN and measures speed.
set -euo pipefail

IP="${1:?usage: $0 <GPU_BOX_IP>}"

echo "== Reachability =="
curl -sf --max-time 5 "http://$IP:11434/api/tags" > /dev/null && echo "OK: Ollama reachable at $IP:11434" || {
    echo "FAILED: cannot reach $IP:11434 — check the GPU box firewall (port 11434/tcp) and OLLAMA_HOST override."
    exit 1
}

MODEL=$(curl -s "http://$IP:11434/api/tags" | python3 -c "import json,sys; ms=[m['name'] for m in json.load(sys.stdin)['models'] if 'qwen3' in m['name']]; print(ms[0] if ms else '')")
[ -n "$MODEL" ] || { echo "FAILED: no qwen3 model on the GPU box — run 01-install-ollama.sh there."; exit 1; }
echo "Model: $MODEL"

echo "== Timed generation over LAN =="
curl -s "http://$IP:11434/api/generate" \
  -d "{\"model\":\"$MODEL\",\"prompt\":\"Say hi like a WoW gnome in 5 words.\",\"stream\":false}" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('Reply:', d['response'].strip()); print(f\"Speed: {d['eval_count']/(d['eval_duration']/1e9):.0f} tok/s (want 100+)\")"

echo
echo "If this looks good, wire the server to the GPU:  ./apply-gpu-config.sh $IP"
