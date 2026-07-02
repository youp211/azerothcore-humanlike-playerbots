# GPU Box (RX 7900 XT) — Chat Inference + Fine-Tuning

Scripts for the second machine (7900XT / 14th-gen i7 / 32 GB, Linux). The wow
server offloads all bot-chat LLM inference here, and the same card fine-tunes
the chat model.

## Order of operations

| # | Script | Runs on | What it does |
|---|--------|---------|--------------|
| 1 | `01-install-ollama.sh` | **GPU box** | Ollama + ROCm, LAN bind (0.0.0.0:11434), pulls Qwen3-4B, verifies GPU speed |
| 2 | `02-verify-lan.sh <ip>` | **wow server** | Confirms reachability + tok/s over the LAN |
| 3 | `apply-gpu-config.sh <ip>` | **wow server** | Points mod-ollama-chat at the GPU box, raises concurrency, hot-reloads |
| 4 | `03-setup-training.sh` | **GPU box** | venv: PyTorch ROCm + Unsloth(AMD) + bitsandbytes + llama.cpp |
| 5 | `04-train.py` | **GPU box** | QLoRA fine-tune of Qwen3-4B on the dataset (copy `finetune/dataset/` over first) |
| 6 | `05-export-gguf.sh` | **GPU box** | Merge → GGUF Q4_K_M → `ollama create wow-chat` |
| 7 | `apply-gpu-config.sh <ip> wow-chat` | **wow server** | Switch the realm to the fine-tuned model |

Steps 1–3 give the immediate win (fast chat). Steps 4–7 are the fine-tune, doable any time later.

## Requirements on the GPU box

- Linux with ROCm 7.x installed (`rocminfo | grep gfx` → `gfx1100`). The 7900XT
  is officially supported — no `HSA_OVERRIDE_GFX_VERSION` needed.
- Open TCP 11434 to the LAN (inference) — firewall is on you.
- ~20 GB disk for the training env + models.

## Expectations

- Inference: 120–170 tok/s for Qwen3-4B Q4 (vs ~5–10 tok/s the wow server's CPU managed).
- Training: 4–6k short examples, 2 epochs ≈ 20–60 min. QLoRA of a 4B fits in
  ~15 GB VRAM; the 20 GB card has headroom (raise `--batch` if underused).
- `bitsandbytes` must be the preview wheel > 0.49.2 (script handles it) — older
  releases produce NaNs in 4-bit on AMD.

## Troubleshooting

- **Slow (<30 tok/s)**: model fell back to CPU. `journalctl -u ollama | grep -i rocm`;
  confirm the amdgpu driver + ROCm versions match.
- **`02-verify-lan.sh` unreachable**: GPU box firewall, or the systemd override
  didn't apply (`systemctl show ollama | grep OLLAMA_HOST`).
- **Realm chat dies when GPU box is off**: on the wow server, restore the newest
  `mod_ollama_chat.conf.bak.*` (points back at localhost) and `.ollama reload`.
- **Training OOM**: lower `--batch` to 4 (gradient accumulation keeps the
  effective batch reasonable).
