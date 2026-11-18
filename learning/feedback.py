from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from agent.skills import EXAMPLES_FILE, EXAMPLES_HEADER, EXAMPLE_MARKER

if TYPE_CHECKING:
    from agent.memory import Memory
    from agent.skills import SkillManager


def apply_feedback(
    memory: "Memory",
    skills: "SkillManager",
    turn: dict,
    rating: int,
    corrected: str | None = None,
    note: str | None = None,
) -> list[str]:
    """Lưu đánh giá cho một lượt và ghi examples.md nếu đủ điều kiện. Trả về các dòng thông báo."""
    memory.rate(turn["id"], rating, corrected, note)
    messages = [f"Đã lưu đánh giá {rating}/5 cho lượt #{turn['id']}."]

    used = [name for name in (turn.get("skills") or "").split(",") if name]
    if not used:
        messages.append("Lượt này không dùng skill nên không ghi examples.md.")
        return messages
    if rating == 3 or (rating <= 2 and not corrected):
        messages.append("Không ghi examples.md (chỉ ghi khi rating>=4, hoặc rating<=2 kèm bản sửa).")
        return messages

    for name in used:
        skill = skills.skills.get(name)
        if skill is None:
            messages.append(f"Bỏ qua skill lạ '{name}'.")
            continue
        path = append_example(skill.path, turn["user_input"], turn["assistant_output"], rating, corrected)
        if path:
            messages.append(f"Đã ghi ví dụ vào {path}")
    return messages


def append_example(
    skill_dir: Path,
    user_input: str,
    output: str,
    rating: int,
    corrected: str | None = None,
) -> str:
    """Ghi một ví dụ vào examples.md của skill. Trả về đường dẫn file, hoặc '' nếu không ghi."""
    if rating == 3 or (rating <= 2 and not corrected):
        return ""

    file = skill_dir / EXAMPLES_FILE
    if not file.is_file():
        file.write_text(EXAMPLES_HEADER, encoding="utf-8")

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    if rating >= 4:
        block = (
            f"{EXAMPLE_MARKER}\n"
            f"## {stamp} · rating {rating}\n"
            f"**Hỏi:** {user_input.strip()}\n\n"
            f"**Đáp tốt:**\n{output.strip()}\n"
        )
    else:
        block = (
            f"{EXAMPLE_MARKER}\n"
            f"## {stamp} · rating {rating} · đã sửa\n"
            f"**Hỏi:** {user_input.strip()}\n\n"
            f"**Đáp chưa tốt:**\n{output.strip()[:1200]}\n\n"
            f"**Đáp đúng:**\n{corrected.strip()}\n"
        )

    with file.open("a", encoding="utf-8") as handle:
        handle.write("\n" + block)
    return str(file)
