# wow-chat fine-tune dataset

Synthetic SFT dataset that teaches Qwen3-4B to talk like 2008 WoW players.

- `generate_dataset.py` — deterministic generator. The **user turn is byte-format
  identical to what mod-ollama-chat sends at inference** (its live
  `ChatPromptTemplate` and `RandomChatterPromptTemplate` with all placeholders
  filled from a WotLK world model: valid race/class combos, level-appropriate
  zones/dungeons, factions, guilds). The assistant turn is a short in-character
  reply from per-personality reply banks with a style layer (lazy caps,
  punctuation dropping, typo injection, terseness) so each personality has a
  recognizable voice.
- Coverage: 40 personalities × 12 message categories (greetings, directions,
  class advice, LFG, trade, insults, quest help, duels, BGs, smalltalk) +
  ambient random-chatter examples (25%) + sentiment-conditioned tone (friendly
  prefix / hostile brush-offs when the prompt carries a sentiment block).
- `dataset/train.jsonl` + `dataset/eval.jsonl` — `{"messages": [user, assistant]}`
  per line, ready for `gpu-box/04-train.py`.

## Regenerate

```bash
python3 generate_dataset.py --n 5000        # writes dataset/
```

## Use

Copy `dataset/` to the GPU box (`scp -r dataset admin@<gpu-ip>:~/wow-finetune/`),
then run `gpu-box/04-train.py` and `05-export-gguf.sh` there.
