# 15 — The wow-chat fine-tune pipeline (developer internals)

Function-by-function reference for the offline toolchain that produces the
`wow-chat` voice model: dataset synthesis, QLoRA training, GGUF export, and the
A/B eval harness that gates deployment. This is the deep companion to the
behavior-level framing in [BOT-BEHAVIOR Section 8](../BOT-BEHAVIOR.md) ("The wow-chat
voice model") and [BOT-ECONOMY Section 4](../BOT-ECONOMY.md)
(`OllamaChat_SpeakSituation`); read those first for *what* the model does in
the game. This doc explains *how the generator/trainer/exporter code works* for
someone about to modify or debug it.

Source of truth (all paths absolute):

| file | role |
|---|---|
| `/home/admin/git/wow/finetune/generate_dataset.py` | synthetic SFT dataset generator |
| `/home/admin/git/wow/gpu-box/04-train.py` | QLoRA fine-tune (runs on the 7900 XT box) |
| `/home/admin/git/wow/gpu-box/05-export-gguf.sh` | merge → GGUF → quantize → `ollama create` |
| `/home/admin/git/wow/finetune/eval_models.py` | A/B voice-metric harness over `eval.jsonl` |
| `/home/admin/git/wow/finetune/README.md` | the *documented* safe retrain/deploy path |

---

## 1. Purpose

`generate_dataset.py` synthesizes JSONL chat examples whose **user turn is
byte-format identical to the prompt strings mod-ollama-chat sends at inference**
(`ChatPromptTemplate`, `RandomChatterPromptTemplate`, the `SpeakSituation` and
guild-rename prompts, and the `{gear_context}` blocks from `GenerateGearContext()`),
paired with a terse, lazy-caps, in-character 2008-WoW-player reply. `04-train.py`
QLoRA-fine-tunes `unsloth/Qwen3-4B-Instruct-2507` on that data; `05-export-gguf.sh`
merges, converts to GGUF, quantizes, and registers it with Ollama as `wow-chat`.
Training on the *real* inference format is the whole point — it is what makes the
persona/gear/sentiment behavior stick instead of drifting into generic-assistant
diction.

---

## 2. Entry points & call graph

There is no hook/trigger/timer here — these are four standalone offline programs.
Execution enters at each `main()` / script top. The **output** of the pipeline
(an Ollama model tag) is what the live C++ detached-worker chat path consumes at
runtime; that runtime path is documented in BOT-BEHAVIOR/BOT-ECONOMY, not here.

**Dataset generation** — `generate_dataset.py main()`:

```
main()
├─ rng = random.Random(args.seed)          # deterministic for a seed
└─ loop until len(rows) == args.n:
   r = rng.random()
   ├─ r < 0.03  → gen_guild_name_example(rng)      # ~3%  guild-name intent
   ├─ r < 0.10  → gen_situation_example(rng)       # ~7%  SpeakSituation lines
   ├─ r < 0.33  → gen_random_chatter_example(rng)  # ~23% ambient chatter
   └─ else      → gen_chat_example(rng)            # ~67% player-directed chat
        each generator calls:
          make_actor(rng)            → bot / player dicts
          pick_zone(rng, lvl, fac)   → level+faction-legal zone
          make_gear_context(rng, …)  → {gear_context} string (chat only, ~35%)
          fill_slots(...) / apply_style(...)
        then rng.shuffle(rows); split eval = rows[:n_eval], train = rows[n_eval:]
        write dataset/train.jsonl + dataset/eval.jsonl
```

**Training** — `04-train.py main()`:

```
main()
├─ FastLanguageModel.from_pretrained(BASE_MODEL, load_in_4bit=True)   # QLoRA base
├─ FastLanguageModel.get_peft_model(r=16, lora_alpha=32, …)           # attach LoRA
├─ tokenizer = get_chat_template(tokenizer, "qwen3-instruct")
├─ load_dataset("json", data_files=args.dataset)  →  .map(to_text)    # render chat template
├─ SFTTrainer(...).train()                                            # 2 epochs
└─ model.save_pretrained_merged(out, save_method="merged_16bit")      # fp16 HF model
```

**Export** — `05-export-gguf.sh` (linear shell):

```
convert_hf_to_gguf.py wow-chat-merged --outtype f16   → wow-chat-f16.gguf
llama-quantize … Q4_K_M                                → wow-chat-Q4_K_M.gguf  (rm f16)
ollama create wow-chat -f Modelfile                    # FROM the gguf + PARAMETERs
curl …/api/generate                                    # smoke test
```

**Eval** — `eval_models.py main()`:

```
main()
├─ parse MODEL args + --n / --show
├─ picks = random.sample(eval.jsonl lines, n)   # random.seed(7)
└─ for each pick, for each model: generate(model, prompt)   # HTTP, serial
   → per-model metrics (avg words, ≤13w%, lowercase-start%, assistant-isms%, latency)
   → printed sample transcripts
```

---

## 3. Function-by-function

### 3.1 `generate_dataset.py`

#### `armor_class_for(pclass, level)`
```python
def armor_class_for(pclass, level):
```
Pure classifier returning `"plate"|"mail"|"leather"|"cloth"`. Encodes WotLK
armor-proficiency-by-level: plate classes (`Warrior`/`Paladin`/`Death Knight`)
wear `mail` under 40 and `plate` from 40; `Hunter`/`Shaman` wear `leather` under
40, `mail` from 40; `Rogue`/`Druid` are always `leather`; everything else
`cloth`. Used only to pick a plausible item name from `GEAR_ITEMS` when the bot
"mails" a gift. No side effects.

#### `make_gear_context(rng, player)`
```python
def make_gear_context(rng, player):
    """Returns (ctx, kind, slot, stat, item); kind in weak|solid|raid|pvp."""
```
Builds one `{gear_context}` string **mirroring `GenerateGearContext()` in
`mod-ollama-chat_handler.cpp` byte-for-byte** (the comment in the file is
explicit about this obligation). Returns a 5-tuple `(ctx, kind, slot, stat, item)`;
`kind` actually takes eight values (`solid`, `raid`, `pvp`, `weak`, `gift`,
`cod`, `park_gift`, `park_cod`) — the docstring lists only the recognition tiers.

Step by step, driven by `r = rng.random()`:
- `r < 0.20` → **solid**: *"(You inspected `<name>`: their gear is solid for their
  level - no weak spots worth mentioning.)"*, `slot=None`, `item=None`.
- `r < 0.32` **and** `level >= 60` → **raid**: *"…decked in epic raid gear[, set
  pieces and all] - nothing you carry comes close.)"* (the `, set pieces and all`
  suffix appears 60% of the time via a nested roll).
- `r < 0.40` **and** `level >= 60` → **pvp**: *"…serious PvP resilience gear - not
  someone to lecture about gear.)"*
- otherwise → **weak slot**: picks `slot = rng.choice(GEAR_SLOTS)`, an item level
  (`0` = "empty" 30% of the time, else `player.level - rng.randint(8,20)` floored
  at 1), and states the class stat priority from `CLASS_STAT`. Then a second roll
  `r2` decides whether the bot *also* mailed/parked a real item — these branches
  are the ones that mirror the economy paths (BOT-ECONOMY Section 1):
  - `r2 < 0.08` → **gift** (mailed free), appends *"You just mailed them your
    `<item>` as a gift…"*
  - `r2 < 0.11` → **cod** (mailed COD), appends *"…with a `<N>` silver COD…pay up."*
  - `r2 < 0.19` → **park_gift** (parked for in-person trade, free)
  - `r2 < 0.23` → **park_cod** (parked for trade, priced)
  - else → `kind="weak"`, no item.

  Item names come from `GEAR_ITEMS[armor_class_for(player.class, player.level)]`.
The string always closes with `)`. **Non-obvious**: the recognition tiers (solid/
raid/pvp) never carry a slot or item, so downstream they always map to the
"respect the geared player" reply category.

#### `apply_style(text, style, rng)`
```python
def apply_style(text: str, style: set, rng: random.Random) -> str:
```
The 2008-diction layer, applied to every generated *reply* (not to prompts).
`style` is the second element of each `P[...]` tuple. Behavior by flag:
- `"formal"` → **early return, unchanged** (roleplayers/paladins keep proper grammar).
- `"caps"` → lowercase the first char, and downcase standalone `"I "` / `" I'"`.
- `"punct"` → strip a trailing `.` and remove all commas.
- `"typo"` → 25% chance (only if `len > 8`): swap two adjacent alpha chars at a
  random interior index — a realistic transposition typo.
- `"excl"` → 40% chance (if not already ending `!`/`?`): append 1–2 `!`.
Order matters: `formal` short-circuits before anything else, so combining
`formal` with other flags is a no-op (no personality does this). Pure w.r.t.
global state; consumes `rng`.

#### `pick_zone(rng, level, faction)`
```python
def pick_zone(rng, level, faction):
```
Filters `ZONES` to those whose `(lo, hi)` bracket contains `level` and whose
faction tag is `"B"` (both) or matches `faction`. Returns a random legal zone;
falls back to `"The Barrens"` if the filter is empty. Keeps prompts internally
consistent (a level-12 Alliance bot is never placed in Icecrown).

#### `make_actor(rng)`
```python
def make_actor(rng):
```
Rolls a coherent character dict used for both bot and player:
`{class, race, faction, f, level, role, name, gender, gold}`. Race is chosen
from `CLASSES[class]` (only valid WotLK race/class pairs), faction is derived
from `ALLIANCE_RACES`, `role` from `ROLES[class]`, gold is `"<0-400> gold"`.
**Death Knight floor**: `lo = 55 if cls == "Death Knight" else 1` — DKs start at
55, so no level-3 DK is ever generated. `f` is the single-letter faction
(`"A"`/`"H"`) used by `pick_zone`; `faction` is the full word used in prompts.

#### `fill_slots(text, rng, bot, player, zone)`
```python
def fill_slots(text, rng, bot, player, zone):
```
Replaces the reply/message template slots `{zone} {dungeon} {pname} {pclass}
{pclass_lower} {bclass} {role} {level} {dir}`. `{dungeon}` is resolved from
`DUNGEONS_BY_LEVEL` filtered to entries within ±3 of the bot's level (fallback
`"Deadmines"`). Note `{role}` here is re-rolled fresh each call from
`["tank","healer","dps"]` — it is *not* the actor's stored `role`; and
`{item}`/`{slot}`/`{stat}` are **not** handled here (gear replies do their own
`.replace()` in `gen_chat_example`).

#### `gen_chat_example(rng)`  ← the ~67% majority path
```python
def gen_chat_example(rng):
```
Produces one player-directed `CHAT_TEMPLATE` example. Steps:
1. Pick a personality key `pkey = rng.choice(list(P))`; unpack `(ptext, style)`.
2. `make_actor` twice; then **force the player onto the bot's faction**
   (`player["f"] = bot["f"]; player["faction"] = bot["faction"]`) and clamp the
   player level to `bot.level ± 4` — same-faction, similar-level so the exchange
   is realistic.
3. Pick a message category `cat = rng.choice(list(MSGS))` and fill an incoming
   `msg`.
4. **Sentiment**: `rng.choices(SENTIMENTS, weights=[8,1,1])` → ~80% no sentiment
   line, ~10% friendly (0.9), ~10% hostile (0.1).
5. **Reply selection**: `bank = BANKS.get(pkey, {})`, then
   `replies = bank.get(cat) or GENERIC[cat]` — personality bank if it has that
   category, else the shared `GENERIC` bank. `fill_slots` the chosen reply.
6. **Sentiment override**: if `hostile` and `pkey` not in the "stay nice" set
   (`WOW_MOM`, `STOIC_PALADIN`, `WANDERING_RP`), replace the whole reply with a
   `HOSTILE_REPLIES` line. If `friendly`, 50% chance (and only when `"formal"`
   not in style) prepend a `FRIENDLY_PREFIX`.
7. `apply_style(reply, style, rng)`.
8. **Gear context (~35%)**: `if rng.random() < 0.35`, call `make_gear_context`,
   append it (with a trailing space) to `gear_ctx`, and decide whether the reply
   should *react* to it:
   - `gcat` maps the gear `kind` to a reply category:
     `{"gift":"gear_gift","cod":"gear_cod","park_gift":"gear_park_gift",
     "park_cod":"gear_park_cod","weak":"gear_advice"}.get(gkind, "gear_good")`
     (so `solid`/`raid`/`pvp` → `"gear_good"`, the respect bank).
   - `must_react = gkind in ("gift","cod","park_gift","park_cod")` — if the bot
     actually mailed/parked an item it **always** talks about it (no silent gifts).
   - `commentary_ok = cat in ("greeting","hows_it_going","smalltalk","compliment",
     "insult")` — **gear commentary may only displace idle chatter**; a real
     request (lfg/directions/quest_help/trade/…) is always answered on its own
     terms even when a `{gear_context}` is in the prompt.
   - Fire when `must_react` OR (`commentary_ok` AND 50% AND not hostile). The
     gear reply is looked up `GEAR_BANKS.get(pkey, {}).get(gcat) or GENERIC[gcat]`,
     with `{slot}`/`{stat}`/`{item}`/`{pclass_lower}` substituted inline
     (`gslot or "gear"`, `gitem or "spare piece"` guard the `None` cases), then
     restyled.
9. Format `CHAT_TEMPLATE` with every field and return
   `{"messages": [{"role":"user",...}, {"role":"assistant",...}]}`.
   `bot_guild=rng.choice(GUILDS) or "No Guild"` (three empty strings in `GUILDS`
   make "guildless" common); the player is always `Guild: No Guild` in the
   template literal.

#### `gen_random_chatter_example(rng)`  ← ambient (~23%)
```python
def gen_random_chatter_example(rng):
```
Uses `RANDOM_TEMPLATE` (mirrors `RandomChatterPromptTemplate`). No player, no
message: rolls one `(env_tmpl, env_replies)` from `ENV_SITUATIONS`, fills the
`{creature}/{quest_hub}/{vendor}/{dungeon}` subs (dungeon again level-filtered),
formats the observed `{environment}` clause, and styles a reply from that
situation's own reply list. Returns the same two-message shape.

#### `gen_guild_name_example(rng)`  ← guild-name intent (~3%)
```python
def gen_guild_name_example(rng):
```
Uses `GUILDNAME_TEMPLATE` (mirrors `OllamaChat_RenameGuildInVoice` in
`mod-ollama-chat_guildnames.cpp`). Picks an `archetype` (`raid`/`pvp`/`casual`),
then a personality **from that archetype's list** (`GUILD_ARCHETYPES`), and emits
a 2–4 word guild name from `GUILD_NAME_BANKS.get(pkey, GUILD_NAME_GENERIC)`.
`ptext = P.get(pkey, ("You lead a guild.", set()))[0]` — the `.get` fallback
matters because several archetype keys (`RAGER`, `MENTOR`, `JOLLY_BEER_LOVER`,
`HEROIC_LEADER`) are **not** in `P`; those personas exist only in the SQL
templates, so the generator supplies a stub persona line. `bot_level` is floored
at 10 (`max(bot["level"], 10)`) since a guild founder isn't level 3.

#### `gen_situation_example(rng)`  ← SpeakSituation (~7%)
```python
def gen_situation_example(rng):
```
Uses `SITUATION_TEMPLATE` (mirrors the one-shot prompt built inside
`OllamaChat_SpeakSituation`, BOT-ECONOMY Section 4). Picks any `pkey` from `P`, one
`(situation, bank)` from `SITUATIONS` (invite/decline/disambiguate/trade-window/
mail-thanks/recruitment/guild-invite lines), a random `target` name, and styles a
reply from that situation's generic bank. The persona block carries the voice;
the bank only fixes the *register* per situation.

#### `main()`
```python
def main():
```
Arg parse (`--n` default 5000, `--seed` 42, `--out` `"dataset"`), seed the RNG,
generate with the 3/10/33 split shown in Section 2, **dedup** on
`key = (prompt[:120], reply)` via a `seen` set (skips collisions), shuffle, split
`n_eval = max(50, n // 50)` (100 for the default 5000), write `eval.jsonl =
rows[:n_eval]` and `train.jsonl = rows[n_eval:]`, and print counts + one sample
reply.

### 3.2 `04-train.py`

#### module constants
```python
BASE_MODEL = "unsloth/Qwen3-4B-Instruct-2507"
MAX_SEQ_LEN = 512   # replies are <=20 words; context blocks are short
```

#### `main()`
```python
def main() -> None:
```
1. Args: `--dataset dataset/train.jsonl`, `--epochs 2`, `--batch 8`,
   `--out wow-chat-merged`.
2. `FastLanguageModel.from_pretrained(model_name=BASE_MODEL,
   max_seq_length=MAX_SEQ_LEN, load_in_4bit=True)` — 4-bit base = the "Q" in QLoRA.
3. `get_peft_model(model, r=16, lora_alpha=32, lora_dropout=0.0,
   target_modules=["q_proj","k_proj","v_proj","o_proj","gate_proj","up_proj",
   "down_proj"], use_gradient_checkpointing="unsloth", random_state=42)` — LoRA on
   all attention + MLP projections, α/r = 2.0.
4. `tokenizer = get_chat_template(tokenizer, "qwen3-instruct")`.
5. `load_dataset("json", …)` then `.map(to_text, remove_columns=…)`.

   ```python
   def to_text(example):
       return {"text": tokenizer.apply_chat_template(
           example["messages"], tokenize=False, add_generation_prompt=False)}
   ```
   Renders each `messages` list into one training string via the qwen3-instruct
   chat template. `add_generation_prompt=False` because the assistant turn is
   already present (this is SFT, not generation). **No response-only loss masking
   is configured** — `SFTTrainer` trains on the full rendered text including the
   (long, templated) user prompt. Given the goal is to lock in the exact prompt
   *format*, that is acceptable, but a maintainer adding new prompt shapes should
   know the model is also fitting the prompt tokens.
6. `SFTTrainer(model, processing_class=tokenizer, train_dataset=ds, args=SFTConfig(
   dataset_text_field="text", per_device_train_batch_size=args.batch,
   gradient_accumulation_steps=2, num_train_epochs=args.epochs, learning_rate=2e-4,
   lr_scheduler_type="cosine", warmup_ratio=0.05, logging_steps=20,
   optim="adamw_8bit", seed=42, output_dir="checkpoints", report_to="none"))`.
   Effective batch = `batch × grad_accum` = 16.
7. `trainer.train()`.
8. `model.save_pretrained_merged(args.out, tokenizer, save_method="merged_16bit")`
   — **merges the LoRA adapter back into the base and writes an fp16 HF model** to
   `wow-chat-merged/` (this merge step, run on a loaded machine, is what corrupted
   a round; see Section 7).

### 3.3 `05-export-gguf.sh`

Linear `set -euo pipefail` script; `TRAIN_DIR="$HOME/wow-finetune"`, activates the
venv. Steps:
1. `MERGED="${1:-wow-chat-merged}"`; hard-fail if the dir is missing.
2. `python llama.cpp/convert_hf_to_gguf.py "$MERGED" --outfile wow-chat-f16.gguf
   --outtype f16` — HF → GGUF f16.
3. `./llama.cpp/build/bin/llama-quantize wow-chat-f16.gguf wow-chat-Q4_K_M.gguf
   Q4_K_M`, then `rm wow-chat-f16.gguf`.
4. Write a heredoc `Modelfile`:
   ```
   FROM ./wow-chat-Q4_K_M.gguf
   PARAMETER temperature 0.9
   PARAMETER num_ctx 4096
   ```
   and `ollama create wow-chat -f Modelfile`; `ollama list | grep wow-chat`.
5. Smoke test: `curl …/api/generate` with an **ad-hoc prompt** (`"…A player says:
   hey man hows the grind? Reply in character:"`) — note this is *not* the training
   `CHAT_TEMPLATE`, so it exercises Ollama plumbing, not the real prompt format.
6. Prints the serve/rollback hints (`apply-gpu-config.sh <IP> wow-chat`).

**Divergence to know**: this committed script quantizes **Q4_K_M** and creates
the **production `wow-chat` tag directly**. The *documented safe path*
(`finetune/README.md`, and the deploy gate below) instead quantizes **Q8_0**,
stages it as **`wow-chat:q8-test`**, runs the coherence gate, and only then
promotes. The shell script does **not** implement the staging gate — the gate is
the manual procedure. Live prod is Q8_0 (`wow-chat = wow-chat:q8`, digest
`c456b968d394`), with `wow-chat:q4` kept as a faster comparator.

### 3.4 `eval_models.py`

#### `generate(model, prompt)`
```python
def generate(model, prompt):
```
POSTs `{"model", "prompt", "stream": False, "options": {"num_predict": 60}}` to
`http://localhost:11434/api/generate` (180 s timeout), returns
`(response.strip(), elapsed_seconds)`. One blocking request per call.

#### `main()`
```python
def main():
```
- Parses positional **model names** by filtering out `--flags` and the value that
  follows `--n`/`--show` (the `skip` set). `--n` (default 12) and `--show` (6) are
  re-read directly from `sys.argv`.
- Loads `/home/admin/git/wow/finetune/dataset/eval.jsonl` (hardcoded absolute
  path), `random.seed(7)`, `picks = random.sample(lines, n)` (fixed subset across
  runs → comparable A/B).
- For each pick × each model: `generate(...)`; on exception stores
  `("<ERROR ...>", -1)`.
- **Metrics** (dropping the `dt < 0` error rows for word/latency stats):
  `avg words`, `<=13w %` (dataset's ≤13-word cap), `lowercase-start %`
  (`r[2][:1].islower()` — the 2008 lazy-caps signal), `assistant-isms %` (any of
  `ASSISTANT_ISMS` substrings — *"as an ai"*, *"feel free"*, *"how can i assist"*,
  …), `median latency`.
- Prints `--- samples ---`: `show` transcripts, each with a persona label, the
  reference reply, and every model's response (truncated to 160 chars).

This is the **coherence-and-form gate**: metrics catch drift; the printed samples
catch corruption that metrics miss (see Section 7).

---

## 4. Data structures & DB

**No database.** This subsystem never touches `acore_characters` or any table —
it reads/writes only files (`dataset/train.jsonl`, `dataset/eval.jsonl`, the HF
model dir, GGUF files, `Modelfile`) and talks to Ollama over HTTP. The DB tables
that the *game* couples to the model output — `mod_ollama_chat_personality_templates`
(persona `prompt`/`weight`/`playstyle`/overrides), `mod_ollama_chat_personality`,
`mod_ollama_chat_bot_player_sentiments`, `mod_ollama_chat_pending_gives` — are
described in BOT-BEHAVIOR Section 2/Section 4 and BOT-ECONOMY Section 2. The generator *models* them in
Python instead.

Key module-level globals in `generate_dataset.py` (all `rng.choice` fodder unless
noted):

| global | shape | role |
|---|---|---|
| `ZONES` | `name → (lvl_lo, lvl_hi, "A"/"H"/"B", map)` | world model for `pick_zone` + `{bot_map}` |
| `CLASSES` | `class → [valid races]` | drives `make_actor` race/class validity |
| `ALLIANCE_RACES` | set | faction derivation |
| `ROLES` | `class → [specs]` | actor `role` |
| `NAMES`, `GUILDS` | lists (`GUILDS` has 3 `""` → guildless) | names / guild slot |
| `DUNGEONS_BY_LEVEL` | `[(lo, hi, name)]` | level-appropriate `{dungeon}` |
| `P` | `key → (persona_prompt, style_set)` | **the 42 personalities** injected into every prompt |
| `GEAR_SLOTS`, `CLASS_STAT`, `GEAR_ITEMS` | slot list / class→stat / armorclass→items | `make_gear_context` |
| `MSGS` | `category → [incoming player lines]` | the 13 message categories |
| `GENERIC` | `category → [replies]` | fallback reply bank (incl. `gear_*`) |
| `BANKS` | `pkey → {category → [replies]}` | per-personality reply overrides |
| `GEAR_BANKS` | `pkey → {gear_cat → [replies]}` | per-personality gear reactions |
| `ENV_SITUATIONS`, `CREATURES`, `QUEST_HUBS`, `VENDORS`, `DIRS` | ambient-chatter vocab | `gen_random_chatter_example`, `{dir}` |
| `CHAT_TEMPLATE` | format string | mirror of live `ChatPromptTemplate` |
| `RANDOM_TEMPLATE` | format string | mirror of `RandomChatterPromptTemplate` |
| `SITUATION_TEMPLATE` | format string | mirror of `OllamaChat_SpeakSituation` prompt |
| `GUILDNAME_TEMPLATE` | format string | mirror of `OllamaChat_RenameGuildInVoice` |
| `SENTIMENTS`, `FRIENDLY_PREFIX`, `HOSTILE_REPLIES` | sentiment lines + reply modifiers | tone conditioning |
| `SITUATIONS` | `[(situation_text, reply_bank)]` | SpeakSituation coverage |
| `GUILD_NAME_BANKS`, `GUILD_NAME_GENERIC`, `GUILD_ARCHETYPES` | guild-name vocab | `gen_guild_name_example` |

**Emitted record schema** (every generator): a single dict
`{"messages": [{"role": "user", "content": <prompt>}, {"role": "assistant",
"content": <reply>}]}` — exactly two turns, **no `system` turn**. (The `04-train.py`
docstring's example shows a three-role `[system, user, assistant]` list; the
generator never produces `system`. Harmless — `apply_chat_template` handles a
two-message list — but the docstring is stale.)

In `04-train.py`: `BASE_MODEL`, `MAX_SEQ_LEN=512`, the LoRA hyperparameters, and
the `SFTConfig` fields are the tunable surface.

---

## 5. Concurrency & threading

**Single-process, no world thread, no mutexes, no shared caches.** These are
offline batch tools:
- `generate_dataset.py` is deterministic and single-threaded; all randomness flows
  through one seeded `random.Random`, so a given `--seed` reproduces the exact
  train/eval split byte-for-byte (this determinism is what made the corruption
  incident diagnosable — see Section 7).
- `04-train.py` is one training process on one GPU (the 7900 XT via ROCm).
- `eval_models.py` issues Ollama requests **serially** in a nested `for` loop
  (one prompt → each model in turn); it does not exploit `OLLAMA_NUM_PARALLEL`.
  Latency numbers are therefore per-request wall time, not throughput.

The only concurrency that matters is *negative*: the merge/GGUF-export step
(`save_pretrained_merged` + `convert_hf_to_gguf.py`) must **not** run while the
box is under competing load. A round of merge/export that ran while heavy builds
and server restarts fought for the machine produced a corrupted model with a
clean loss curve (Section 7). Threading of the *live* inference path (mod-ollama-chat's
detached worker thread, GUID-capture-and-reacquire, the `MaxConcurrentQueries`
queue) is out of scope here — see BOT-ECONOMY Section 4 (`OllamaChat_SpeakSituation`
captures raw GUIDs into a detached `std::thread` and reacquires via
`ObjectAccessor::FindPlayer`).

---

## 6. Config keys

This subsystem exposes **no `sConfigMgr` options of its own** — its "config" is
CLI flags, LoRA hyperparameters, and the Ollama `Modelfile`. The `sConfigMgr`
keys live on the C++ consumer side and are the *contract* the generator must
mirror.

**Command-line / hyperparameter surface:**

| where | key | default |
|---|---|---|
| `generate_dataset.py` | `--n` | 5000 |
| | `--seed` | 42 |
| | `--out` | `dataset` |
| `04-train.py` | `--dataset` | `dataset/train.jsonl` |
| | `--epochs` | 2 |
| | `--batch` | 8 |
| | `--out` | `wow-chat-merged` |
| | `BASE_MODEL` | `unsloth/Qwen3-4B-Instruct-2507` |
| | `MAX_SEQ_LEN` | 512 |
| | LoRA | `r=16`, `lora_alpha=32`, `lora_dropout=0.0`, 7 target modules |
| | train | `lr=2e-4`, cosine, `warmup_ratio=0.05`, `grad_accum=2`, `optim=adamw_8bit`, `seed=42` |
| `05-export-gguf.sh` | positional `$1` (merged dir) | `wow-chat-merged` |
| | quant | `Q4_K_M` (README/prod path uses `Q8_0`) |
| | `Modelfile` | `temperature 0.9`, `num_ctx 4096` |
| `eval_models.py` | `--n` | 12 |
| | `--show` | 6 |
| | Ollama endpoint | `http://localhost:11434/api/generate` |
| | `num_predict` | 60 |
| | request timeout | 180 s |

**Serving-side `sConfigMgr` keys the pipeline output feeds** (defined/consumed in
mod-ollama-chat, not here — details in BOT-BEHAVIOR Section 8): `OllamaChat.Model`
(= `wow-chat`), `OllamaChat.Url` (`http://127.0.0.1:11434/api/generate`),
`OllamaChat.MaxConcurrentQueries` (8), `OllamaChat.RequestTimeout` (120),
`OllamaChat.EnableTypingSimulation` (1), and critically
**`OllamaChat.ChatPromptTemplate` / `OllamaChat.RandomChatterPromptTemplate`** —
the two live templates that `CHAT_TEMPLATE`/`RANDOM_TEMPLATE` must stay
byte-identical to. **If you edit a prompt template on either side, edit both.**

---

## 7. Failure modes & gotchas

**The deploy gate (why it exists).** One retrain produced a model emitting garbled
subwords (*"Billgtnrd"*, *"hahaapparnted"*) with a **perfectly clean loss curve
that was bit-identical to a later good run** (deterministic seed made the two
curves comparable). Loss does not validate an export — the merge or GGUF step had
run on a machine also doing heavy builds/restarts, and both quants came out
garbled; the identical training on a quiet box was clean. Consequences baked into
the process:
1. **Never re-alias `wow-chat` to a model you haven't read samples from.** Run
   `python3 finetune/eval_models.py <candidate> wow-chat:v1 --n 8 --show 8` and
   *read the `--show` transcripts* — corruption shows as garbled subwords with
   otherwise clean form metrics (avg words, ≤13w%, lazy-caps%).
2. **Stage, don't overwrite**: quantize to `wow-chat:q8-test`, gate, then
   `ollama cp wow-chat wow-chat:v1` (keep the old prod as rollback) → promote →
   `ollama cp wow-chat:q8 wow-chat`. Rollback is `ollama cp wow-chat:v1 wow-chat`.
3. **Keep merge/export off a loaded machine.**

**Graceful-degradation / robustness edges in the generator:**
- `gen_guild_name_example` uses `P.get(pkey, ("You lead a guild.", set()))` because
  `GUILD_ARCHETYPES` references personas (`RAGER`, `MENTOR`, `JOLLY_BEER_LOVER`,
  `HEROIC_LEADER`) that exist in the SQL templates but **not in `P`** — the `.get`
  keeps generation from `KeyError`ing.
- `make_gear_context` returns `slot=None`/`item=None` for the recognition tiers;
  `gen_chat_example` guards every substitution with `gslot or "gear"` /
  `gitem or "spare piece"`, so a `None` never reaches the output string.
- `pick_zone` falls back to `"The Barrens"` and `fill_slots` falls back to
  `"Deadmines"` when a level filter is empty — no crash on an unusual level.
- `main`'s dedup keys on `(prompt[:120], reply)` — only the **first 120 chars** of
  the prompt count, so two prompts that differ only past char 120 with the same
  reply are treated as duplicates and one is dropped. Intentional (kills
  near-identical rows) but worth knowing if you expect an exact `--n` distribution.

**`eval_models.py` gotchas:**
- **Broken persona label in samples**: the header line does
  `p.split("personality, WHICH IS:")[-1]`, but the template literal is
  `"...PERSONALITY, WHICH IS:"` (uppercase `PERSONALITY`). The split substring
  never matches, so `[-1]` returns the whole prompt and `[i] persona:` prints the
  prompt's first 60 chars, not the persona name. Cosmetic (metrics are unaffected),
  but misleading when eyeballing which persona a sample came from.
- `random.sample(lines, n)` raises `ValueError` if `--n` exceeds the eval-set size
  (100 rows by default) — asking for more samples than exist is a hard error, not
  a clamp.
- The eval path is hardcoded to `/home/admin/git/wow/finetune/dataset/eval.jsonl`;
  it does not honor a `--out` and won't find a dataset generated elsewhere.
- Error responses are stored as `("<ERROR ...>", -1)` and **excluded** from word/
  latency stats (rows with `dt >= 0` only) but still counted in the `lowercase-
  start`/`assistant-isms` denominators (which iterate all `rows`), so a model that
  errors out skews those two percentages downward.

**Format-coupling gotchas:**
- `MAX_SEQ_LEN = 512` in training vs `num_ctx 4096` in the `Modelfile` — training
  truncates anything over 512 tokens. Prompts with a long `{gear_context}` plus
  sentiment are near that ceiling; a substantially longer future template could
  silently truncate training examples while still fitting at inference.
- The `05-export-gguf.sh` smoke-test prompt is **not** in `CHAT_TEMPLATE` format,
  so a green smoke test proves Ollama loads the model, not that it behaves in the
  real prompt shape. Use `eval_models.py` for the real check.
- The committed export script produces **Q4_K_M** and writes prod `wow-chat`
  directly; production actually runs **Q8_0** via the README's staged path. Don't
  assume running `05-export-gguf.sh` reproduces the live model — it produces the
  Q4 comparator and skips the gate.

---

## 8. Cross-references

- [`../BOT-BEHAVIOR.md`](../BOT-BEHAVIOR.md) — Section 2 personalities & the
  `mod_ollama_chat_personality_templates` schema, Section 4 sentiment, Section 5 the C++
  `GenerateGearContext()` this generator mirrors, Section 8 the voice model + deploy gate
  at behavior level.
- [`../BOT-ECONOMY.md`](../BOT-ECONOMY.md) — Section 1 the real gear-give mail path the
  gift/COD gear contexts model, Section 2 the pending-give trade hand-over, Section 4
  `OllamaChat_SpeakSituation` (the prompt `SITUATION_TEMPLATE` mirrors) and the
  weak-symbol cross-module wiring.
- [`../BUILD-NOTES.md`](../BUILD-NOTES.md) — chronological journal; the training-
  corruption incident and the ROCm/`pkill` gotchas live in the retrain entries.
- [`../../finetune/README.md`](../../finetune/README.md) — the canonical safe
  retrain+deploy runbook (Q8_0 staging tag, coherence gate, promotion commands).
- [`../../gpu-box/README.md`](../../gpu-box/README.md) — GPU-box setup
  (`03-setup-training.sh` env), `apply-gpu-config.sh` serve/rollback.

*(This is the first file under `docs/internals/`; future sibling internals docs
should link back here for the fine-tune pipeline.)*
