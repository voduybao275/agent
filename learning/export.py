from __future__ import annotations

import json
from pathlib import Path


def export_sft(turns: list[dict], system_prompt: str, out_path: Path) -> int:
    records = []
    for turn in turns:
        records.append(
            {
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": turn["user_input"]},
                    {"role": "assistant", "content": turn["corrected"] or turn["assistant_output"]},
                ]
            }
        )
    return _write(records, out_path)


def export_dpo(turns: list[dict], out_path: Path) -> int:
    records = [
        {
            "prompt": turn["user_input"],
            "chosen": turn["corrected"],
            "rejected": turn["assistant_output"],
        }
        for turn in turns
    ]
    return _write(records, out_path)


def _write(records: list[dict], out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return len(records)
