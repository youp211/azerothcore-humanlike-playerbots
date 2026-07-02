#!/usr/bin/env bash
# Run ON THE GPU BOX after 04-train.py. Converts the merged model to GGUF
# Q4_K_M and registers it with Ollama as 'wow-chat'.
set -euo pipefail

TRAIN_DIR="$HOME/wow-finetune"
cd "$TRAIN_DIR"
source venv/bin/activate

MERGED="${1:-wow-chat-merged}"
[ -d "$MERGED" ] || { echo "Merged model dir '$MERGED' not found - run 04-train.py first."; exit 1; }

echo "== HF -> GGUF (f16) =="
python llama.cpp/convert_hf_to_gguf.py "$MERGED" --outfile wow-chat-f16.gguf --outtype f16

echo "== Quantize Q4_K_M =="
./llama.cpp/build/bin/llama-quantize wow-chat-f16.gguf wow-chat-Q4_K_M.gguf Q4_K_M
rm wow-chat-f16.gguf

echo "== Register with Ollama =="
cat > Modelfile <<'EOF'
FROM ./wow-chat-Q4_K_M.gguf
PARAMETER temperature 0.9
PARAMETER num_ctx 4096
EOF
ollama create wow-chat -f Modelfile
ollama list | grep wow-chat

echo "== Smoke test =="
curl -s http://localhost:11434/api/generate \
  -d '{"model":"wow-chat","prompt":"You are a level 34 orc warrior in Thousand Needles. A player says: hey man hows the grind? Reply in character:","stream":false}' \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['response'].strip())"

echo
echo "Serve it to the realm from the wow server:"
echo "  ./apply-gpu-config.sh <GPU_BOX_IP> wow-chat"
echo "Rollback: ./apply-gpu-config.sh <GPU_BOX_IP> <qwen3 base tag>"
