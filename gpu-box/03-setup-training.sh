#!/usr/bin/env bash
# Run ON THE GPU BOX. One-time setup of the QLoRA fine-tuning environment
# (Unsloth with official AMD/ROCm support + llama.cpp for GGUF export).
# Requires: ROCm installed (rocminfo shows gfx1100), python3.10+, git, cmake.
set -euo pipefail

TRAIN_DIR="$HOME/wow-finetune"
mkdir -p "$TRAIN_DIR" && cd "$TRAIN_DIR"

echo "== ROCm check =="
# NB: don't pipe rocminfo straight into grep -m1 — early grep exit SIGPIPEs
# rocminfo, which pipefail turns into a bogus failure
rocminfo > /tmp/rocminfo.$$ 2>&1 || true
grep -m1 gfx /tmp/rocminfo.$$ || { rm -f /tmp/rocminfo.$$; echo "ROCm not working - install ROCm 7.x first (amdgpu-install --usecase=rocm)"; exit 1; }
rm -f /tmp/rocminfo.$$

echo "== Python venv =="
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip uv

echo "== PyTorch ROCm wheels =="
# Match the rocmX.Y suffix to your installed ROCm major.minor if different
uv pip install "torch>=2.4,<2.11.0" "torchvision<0.26.0" "torchaudio<2.11.0" \
    --index-url https://download.pytorch.org/whl/rocm7.1 --upgrade --force-reinstall

echo "== Unsloth (AMD) =="
uv pip install "unsloth[amd]"

echo "== ROCm bitsandbytes (4-bit QLoRA) =="
# MUST be > 0.49.2: older builds have a 4-bit decode NaN bug on all AMD GPUs
pip install --force-reinstall --no-cache-dir --no-deps \
  "https://github.com/bitsandbytes-foundation/bitsandbytes/releases/download/continuous-release_main/bitsandbytes-1.33.7.preview-py3-none-manylinux_2_24_x86_64.whl"

echo "== llama.cpp (GGUF conversion + quantize) =="
if [ ! -d llama.cpp ]; then
    git clone --depth 1 https://github.com/ggml-org/llama.cpp
fi
cmake -S llama.cpp -B llama.cpp/build -DGGML_CUDA=OFF -DGGML_HIP=OFF
cmake --build llama.cpp/build --target llama-quantize -j "$(nproc)"
pip install -r llama.cpp/requirements/requirements-convert_hf_to_gguf.txt
# llama.cpp's requirements pull in a CPU torch from PyPI over the ROCm build —
# re-pin the ROCm wheels afterwards or the GPU disappears from torch
uv pip install "torch>=2.4,<2.11.0" "torchvision<0.26.0" "torchaudio<2.11.0" \
    --index-url https://download.pytorch.org/whl/rocm7.1 --upgrade --force-reinstall

echo "== GPU visible to torch? =="
python - <<'EOF'
import torch
assert torch.cuda.is_available(), "torch does not see the GPU (ROCm builds expose it via the cuda API)"
print("GPU:", torch.cuda.get_device_name(0))
EOF

echo
echo "Setup complete. Copy the dataset from the wow server, then train:"
echo "  scp -r admin@<WOW_SERVER_IP>:/home/admin/git/wow/finetune/dataset $TRAIN_DIR/"
echo "  source $TRAIN_DIR/venv/bin/activate && python 04-train.py"
