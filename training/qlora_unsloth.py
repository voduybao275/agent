from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _init_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for lineno, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path}:{lineno}: JSON lỗi: {exc}") from exc
    if not rows:
        raise SystemExit(f"{path}: không có dữ liệu.")
    return rows


def validate(rows: list[dict], mode: str) -> None:
    if mode == "sft":
        for index, row in enumerate(rows, 1):
            if not isinstance(row.get("messages"), list) or not row["messages"]:
                raise SystemExit(f"Dòng {index}: SFT cần trường 'messages'.")
    else:
        required = {"prompt", "chosen", "rejected"}
        for index, row in enumerate(rows, 1):
            missing = required - row.keys()
            if missing:
                raise SystemExit(f"Dòng {index}: DPO thiếu {', '.join(sorted(missing))}.")


def estimate_tokens(rows: list[dict], mode: str) -> int:
    chars = 0
    for row in rows:
        if mode == "sft":
            chars += sum(len(str(message.get("content", ""))) for message in row["messages"])
        else:
            chars += sum(len(str(row[key])) for key in ("prompt", "chosen", "rejected"))
    return chars // 4


def print_env() -> None:
    try:
        import torch
    except ImportError:
        print("torch: chưa cài (chỉ cần khi train thật)")
        return
    if torch.cuda.is_available():
        print(f"torch {torch.__version__} | CUDA {torch.version.cuda} | GPU: {torch.cuda.get_device_name(0)}")
    else:
        print(f"torch {torch.__version__} | CUDA: không có")


def dry_run(rows: list[dict], mode: str, args) -> None:
    print(f"mode={mode} | rows={len(rows)} | model={args.model}")
    print(
        f"epochs={args.epochs} batch={args.batch_size} grad_accum={args.grad_accum} "
        f"lr={args.lr} max_seq_len={args.max_seq_len}"
    )
    print(f"ước lượng dữ liệu ~{estimate_tokens(rows, mode):,} tokens (thô: ký tự/4)")
    print("mẫu:", json.dumps(rows[0], ensure_ascii=False)[:400])
    print_env()
    print("Dry-run OK — chưa import unsloth/trl, chưa train.")


def require_cuda() -> None:
    try:
        import torch
    except ImportError as exc:
        raise SystemExit("Thiếu torch (bản CUDA). Cài torch trước khi train thật.") from exc
    if not torch.cuda.is_available():
        raise SystemExit("Không thấy GPU CUDA — QLoRA cần GPU NVIDIA. Dùng --dry-run để kiểm tra dữ liệu.")


def load_model(args):
    from unsloth import FastLanguageModel

    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=args.model,
        max_seq_length=args.max_seq_len,
        dtype=None,
        load_in_4bit=not args.no_4bit,
    )
    model = FastLanguageModel.get_peft_model(
        model,
        r=args.lora_r,
        target_modules=[
            "q_proj", "k_proj", "v_proj", "o_proj",
            "gate_proj", "up_proj", "down_proj",
        ],
        lora_alpha=args.lora_r,
        lora_dropout=0.0,
        bias="none",
        use_gradient_checkpointing="unsloth",
        random_state=3407,
    )
    return model, tokenizer


def common_training_kwargs(args) -> dict:
    return dict(
        per_device_train_batch_size=args.batch_size,
        gradient_accumulation_steps=args.grad_accum,
        warmup_steps=5,
        num_train_epochs=args.epochs,
        learning_rate=args.lr,
        logging_steps=1,
        optim="adamw_8bit",
        lr_scheduler_type="linear",
        seed=3407,
        output_dir=args.output,
        report_to="none",
    )


def train_sft(rows: list[dict], args) -> None:
    from datasets import Dataset
    from transformers import TrainingArguments
    from trl import SFTTrainer
    from unsloth import is_bfloat16_supported

    model, tokenizer = load_model(args)

    dataset = Dataset.from_list(rows).map(
        lambda batch: {
            "text": [
                tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=False)
                for messages in batch["messages"]
            ]
        },
        batched=True,
    )

    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=dataset,
        dataset_text_field="text",
        max_seq_length=args.max_seq_len,
        packing=False,
        args=TrainingArguments(
            **common_training_kwargs(args),
            weight_decay=0.01,
            fp16=not is_bfloat16_supported(),
            bf16=is_bfloat16_supported(),
        ),
    )
    trainer.train()
    _save(model, tokenizer, args.output)


def train_dpo(rows: list[dict], args) -> None:
    from datasets import Dataset
    from trl import DPOConfig, DPOTrainer
    from unsloth import is_bfloat16_supported

    model, tokenizer = load_model(args)
    dataset = Dataset.from_list(
        [
            {"prompt": row["prompt"], "chosen": row["chosen"], "rejected": row["rejected"]}
            for row in rows
        ]
    )

    trainer = DPOTrainer(
        model=model,
        ref_model=None,
        tokenizer=tokenizer,
        train_dataset=dataset,
        args=DPOConfig(
            **common_training_kwargs(args),
            beta=0.1,
            max_length=args.max_seq_len,
            max_prompt_length=args.max_seq_len // 2,
            fp16=not is_bfloat16_supported(),
            bf16=is_bfloat16_supported(),
        ),
    )
    trainer.train()
    _save(model, tokenizer, args.output)


def _save(model, tokenizer, output: str) -> None:
    model.save_pretrained(output)
    tokenizer.save_pretrained(output)
    print(f"Đã lưu LoRA adapter vào {output}")
    print("Để dùng với Ollama: merge adapter với base model rồi convert sang GGUF (Modelfile ADAPTER).")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fine-tune LoRA/QLoRA với Unsloth (cần GPU NVIDIA).")
    parser.add_argument("--data", default="data/sft.jsonl", help="File JSONL từ lệnh export")
    parser.add_argument("--mode", choices=["sft", "dpo"], default="sft")
    parser.add_argument("--model", default="unsloth/Qwen2.5-3B-Instruct")
    parser.add_argument("--output", default="models/qwen-agent-lora")
    parser.add_argument("--epochs", type=float, default=3)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--grad-accum", type=int, default=4)
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--max-seq-len", type=int, default=2048)
    parser.add_argument("--lora-r", type=int, default=16)
    parser.add_argument("--no-4bit", action="store_true", help="Tắt load_in_4bit nếu đủ VRAM")
    parser.add_argument("--dry-run", action="store_true", help="Chỉ kiểm tra dữ liệu, không cần GPU")
    return parser


def main() -> int:
    _init_streams()
    args = build_parser().parse_args()

    data_path = Path(args.data)
    if not data_path.is_file():
        print(f"Không thấy file dữ liệu: {data_path}", file=sys.stderr)
        return 2

    rows = load_jsonl(data_path)
    validate(rows, args.mode)

    if args.dry_run:
        dry_run(rows, args.mode, args)
        return 0

    require_cuda()
    Path(args.output).mkdir(parents=True, exist_ok=True)
    if args.mode == "sft":
        train_sft(rows, args)
    else:
        train_dpo(rows, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
