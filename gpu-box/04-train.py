#!/usr/bin/env python3
"""QLoRA fine-tune of Qwen3-4B into the 'wow-chat' voice model.

Run ON THE GPU BOX inside the venv from 03-setup-training.sh:
    python 04-train.py [--dataset dataset/train.jsonl] [--epochs 2]

Expects the JSONL produced by finetune/generate_dataset.py on the wow server:
one object per line: {"messages": [{"role": "system", ...}, {"role": "user", ...},
{"role": "assistant", ...}]}.

Output: ./wow-chat-merged/ (merged fp16 HF model, ready for 05-export-gguf.sh).
"""
import argparse
import os

from unsloth import FastLanguageModel
from unsloth.chat_templates import get_chat_template
from datasets import load_dataset
from trl import SFTConfig, SFTTrainer

BASE_MODEL = "unsloth/Qwen3-4B-Instruct-2507"
MAX_SEQ_LEN = 512  # replies are <=20 words; context blocks are short


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="dataset/train.jsonl")
    ap.add_argument("--epochs", type=int, default=2)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--out", default="wow-chat-merged")
    args = ap.parse_args()

    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=BASE_MODEL,
        max_seq_length=MAX_SEQ_LEN,
        load_in_4bit=True,  # QLoRA
    )
    model = FastLanguageModel.get_peft_model(
        model,
        r=16,
        lora_alpha=32,
        lora_dropout=0.0,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj",
                        "gate_proj", "up_proj", "down_proj"],
        use_gradient_checkpointing="unsloth",
        random_state=42,
    )

    tokenizer = get_chat_template(tokenizer, chat_template="qwen3-instruct")

    ds = load_dataset("json", data_files=args.dataset, split="train")

    def to_text(example):
        return {"text": tokenizer.apply_chat_template(
            example["messages"], tokenize=False, add_generation_prompt=False)}

    ds = ds.map(to_text, remove_columns=ds.column_names)

    trainer = SFTTrainer(
        model=model,
        processing_class=tokenizer,
        train_dataset=ds,
        args=SFTConfig(
            dataset_text_field="text",
            per_device_train_batch_size=args.batch,
            gradient_accumulation_steps=2,
            num_train_epochs=args.epochs,
            learning_rate=2e-4,
            lr_scheduler_type="cosine",
            warmup_ratio=0.05,
            logging_steps=20,
            optim="adamw_8bit",
            seed=42,
            output_dir="checkpoints",
            report_to="none",
        ),
    )
    trainer.train()

    print("Merging LoRA into base weights (fp16)...")
    model.save_pretrained_merged(args.out, tokenizer, save_method="merged_16bit")
    print(f"Done -> {os.path.abspath(args.out)}  (now run 05-export-gguf.sh)")


if __name__ == "__main__":
    main()
