#!/usr/bin/env bash
# Run ON THE GPU BOX (7900XT, Linux).
# Installs Ollama with ROCm support, exposes it on the LAN, pulls the chat model.
set -euo pipefail

echo "== Installing Ollama (includes ROCm runtime for gfx1100) =="
curl -fsSL https://ollama.com/install.sh | sh

echo "== Configuring service: LAN bind, keep model resident, 4 parallel requests =="
sudo mkdir -p /etc/systemd/system/ollama.service.d
sudo tee /etc/systemd/system/ollama.service.d/override.conf > /dev/null <<'EOF'
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_KEEP_ALIVE=-1"
Environment="OLLAMA_NUM_PARALLEL=4"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now ollama
sudo systemctl restart ollama
sleep 3

echo "== Pulling Qwen3-4B instruct (tries most specific tag first) =="
ollama pull qwen3:4b-instruct-2507-q4_K_M \
  || ollama pull qwen3:4b-instruct \
  || ollama pull qwen3:4b
MODEL=$(ollama list | awk '/qwen3/{print $1; exit}')
echo "Pulled model: $MODEL"

echo "== GPU sanity check =="
# Should list the model running on GPU (100% GPU), not CPU
curl -s http://localhost:11434/api/generate \
  -d "{\"model\":\"$MODEL\",\"prompt\":\"Say hi like a WoW orc in 5 words.\",\"stream\":false}" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('Reply:', d['response'].strip()); print(f\"Speed: {d['eval_count']/(d['eval_duration']/1e9):.0f} tok/s\")"
ollama ps

echo
echo "Done. Expect 100+ tok/s. If speed is <30 tok/s it fell back to CPU:"
echo "  - check 'journalctl -u ollama | grep -i rocm' for driver errors"
echo "  - verify ROCm sees the card: rocminfo | grep gfx  (expect gfx1100)"
echo
echo "Note your LAN IP for the wow server:  ip -4 addr show scope global"
