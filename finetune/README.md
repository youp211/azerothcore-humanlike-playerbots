# wow-chat fine-tune dataset & eval

Synthetic SFT dataset that teaches Qwen3-4B to talk like 2008 WoW players —
in 42 distinct personalities, with gear awareness.

## Files

- `generate_dataset.py` — deterministic generator. The **user turn is
  byte-format identical to what mod-ollama-chat sends at inference** (its live
  `ChatPromptTemplate` / `RandomChatterPromptTemplate` with all placeholders
  filled from a WotLK world model: valid race/class combos, level-appropriate
  zones/dungeons, factions, guilds — and, since v2, `{gear_context}` blocks
  that mirror `GenerateGearContext()` in the C++ **string-for-string**; if you
  change one side, change the other). The assistant turn comes from
  per-personality reply banks + a style layer (lazy caps, dropped punctuation,
  typo injection, terseness).
- Coverage: 42 personalities × 12 message categories, 25% ambient chatter,
  sentiment-conditioned tone, ~35% gear-inspect contexts with
  **per-personality gear reactions** (`GEAR_BANKS`): helpers gift the item,
  merchants sell it, elitists flame, and *everyone* respects raid-set /
  resilience / solid-for-level recognition contexts instead of nagging.
- `dataset/train.jsonl` + `dataset/eval.jsonl` — 4,900/100,
  `{"messages": [user, assistant]}` per line.
- `eval_models.py` — A/B harness over `eval.jsonl` via the Ollama API:
  `python3 eval_models.py wow-chat wow-chat:v1 --n 10 --show 6`.
  Form metrics (avg words, ≤13-word %, lazy-caps %, assistant-isms, latency)
  **plus printed samples — read them** (see deploy gate below).

## Retrain + deploy procedure (the safe path)

```bash
# 1. regenerate after any generator change
python3 generate_dataset.py --n 5000

# 2. train on this box (~55 min on the 7900 XT). Free VRAM first.
ollama stop wow-chat
rm -rf ~/wow-finetune/dataset ~/wow-finetune/checkpoints ~/wow-finetune/wow-chat-merged
cp -r dataset ~/wow-finetune/ && cd ~/wow-finetune
source venv/bin/activate && python 04-train.py

# 3. export to a STAGING tag - never straight to production
python llama.cpp/convert_hf_to_gguf.py wow-chat-merged --outfile wow-chat-f16.gguf --outtype f16
./llama.cpp/build/bin/llama-quantize wow-chat-f16.gguf wow-chat-Q8_0.gguf Q8_0
ollama create wow-chat:q8-test -f Modelfile-q8   # point Modelfile at the new gguf

# 4. COHERENCE GATE - loss curves do not validate exports (see below)
python3 /home/admin/git/wow/finetune/eval_models.py wow-chat:q8-test wow-chat:v1 --n 8 --show 8

# 5. only if samples read clean: promote
ollama cp wow-chat wow-chat:v1        # rollback tag (previous prod)
ollama create wow-chat:q8 -f Modelfile-q8
ollama cp wow-chat:q8 wow-chat        # realm picks it up next generation, no restart
sudo systemctl restart ollama         # evict stale keep_alive=-1 runners
```

Rollback at any time: `ollama cp wow-chat:v1 wow-chat`.

## Why the gate exists

One retrain produced a model emitting garbled subwords ("Billgtnrd",
"hahaapparnted") with a **perfectly clean loss curve, bit-identical to the
later good run** (deterministic seed). Both quants were garbled → the merge or
GGUF export was corrupted (it had run while heavy builds and server restarts
were competing for the box). A re-run of the identical training on a quiet
machine came out clean. Morals:

1. Never point the realm at a model you haven't read samples from.
2. Keep the merge/export step off a loaded machine.
3. Keep the previous model as an Ollama rollback tag, always.

## Gotchas

- Killing a training run: `pkill -f 04-train.py` — and note `pgrep`/`pkill -f`
  matches your own wrapper shell; verify with VRAM
  (`/sys/class/drm/card1/device/mem_info_vram_used`), not process greps.
- bitsandbytes must be > 0.49.2 (setup script pins a preview wheel) — older
  builds NaN in 4-bit on all AMD GPUs.
- Training wants ~17.5 GB VRAM: evict Ollama models first; ComfyUI holds VRAM
  until you POST `{"unload_models":true,"free_memory":true}` to
  `127.0.0.1:8188/free`.
