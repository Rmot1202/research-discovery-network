"""LoRA fine-tune of a small open model on training/data/train.jsonl.

Run on a free GPU notebook (Google Colab T4 or Kaggle), not on Render:
  !git clone https://github.com/<you>/research-discovery-network && cd research-discovery-network
  !pip install -r training/requirements.txt
  !python training/train_lora.py --data training/data --model unsloth/Qwen2.5-1.5B-Instruct

Outputs:
  outputs/rdn-assistant-lora/   LoRA adapter (small, keep it)
  outputs/rdn-assistant-gguf/   quantized GGUF for Ollama / llama.cpp (--gguf q4_k_m)

Only the assistant turns are trained (system prompt + archive context are masked), so the model
learns how to answer from context instead of memorizing the papers.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_rows(path: Path) -> list[dict]:
    return [{"messages": json.loads(line)["messages"]} for line in path.open(encoding="utf-8") if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="training/data")
    parser.add_argument("--model", default="unsloth/Qwen2.5-1.5B-Instruct",
                        help="Try unsloth/Qwen2.5-3B-Instruct for better answers if the GPU has room.")
    parser.add_argument("--max-seq-length", type=int, default=6144, help="Archive context is ~4-5k tokens.")
    parser.add_argument("--epochs", type=float, default=2)
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--rank", type=int, default=16)
    parser.add_argument("--out", default="outputs")
    parser.add_argument("--gguf", default="q4_k_m", help="GGUF quantization, or 'none' to skip export.")
    args = parser.parse_args()

    from unsloth import FastLanguageModel, is_bfloat16_supported  # import first: it patches transformers
    from unsloth.chat_templates import train_on_responses_only
    from datasets import Dataset
    from transformers import DataCollatorForSeq2Seq
    from trl import SFTConfig, SFTTrainer

    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=args.model, max_seq_length=args.max_seq_length, load_in_4bit=True,
    )
    model = FastLanguageModel.get_peft_model(
        model, r=args.rank, lora_alpha=args.rank, lora_dropout=0,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
        use_gradient_checkpointing="unsloth", random_state=3407,
    )

    def to_text(batch):
        return {"text": [tokenizer.apply_chat_template(m, tokenize=False) for m in batch["messages"]]}

    data = Path(args.data)
    train = Dataset.from_list(load_rows(data / "train.jsonl")).map(to_text, batched=True, remove_columns=["messages"])
    val = Dataset.from_list(load_rows(data / "val.jsonl")).map(to_text, batched=True, remove_columns=["messages"])

    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=train,
        eval_dataset=val,
        data_collator=DataCollatorForSeq2Seq(tokenizer=tokenizer),
        args=SFTConfig(
            dataset_text_field="text",
            max_seq_length=args.max_seq_length,
            per_device_train_batch_size=1,
            gradient_accumulation_steps=8,
            num_train_epochs=args.epochs,
            learning_rate=args.lr,
            warmup_ratio=0.05,
            lr_scheduler_type="cosine",
            optim="adamw_8bit",
            weight_decay=0.01,
            fp16=not is_bfloat16_supported(),
            bf16=is_bfloat16_supported(),
            logging_steps=10,
            eval_strategy="epoch",
            save_strategy="epoch",
            output_dir=f"{args.out}/checkpoints",
            report_to="none",
            seed=3407,
        ),
    )
    # Qwen2.5 chat template markers. Change these if you switch to a Llama or Gemma base model.
    trainer = train_on_responses_only(
        trainer, instruction_part="<|im_start|>user\n", response_part="<|im_start|>assistant\n",
    )
    trainer.train()

    model.save_pretrained(f"{args.out}/rdn-assistant-lora")
    tokenizer.save_pretrained(f"{args.out}/rdn-assistant-lora")
    if args.gguf != "none":
        model.save_pretrained_gguf(f"{args.out}/rdn-assistant-gguf", tokenizer, quantization_method=args.gguf)
    print("Done. Next: copy the .gguf next to training/Modelfile and run `ollama create rdn-assistant -f Modelfile`.")


if __name__ == "__main__":
    main()
