from __future__ import annotations

import re
from pathlib import Path

from agent.llm import LLM
from agent.skills import Skill, examples_text

AUTO_START = "<!-- auto-learned:start -->"
AUTO_END = "<!-- auto-learned:end -->"
MAX_INPUT_CHARS = 6000


def distill_skill(skill: Skill, llm: LLM) -> tuple[bool, str]:
    examples = examples_text(skill.path)
    if not examples.strip():
        return False, f"Bỏ qua {skill.name}: chưa có ví dụ nào trong examples.md."

    prompt = (
        f'Dưới đây là các cặp hỏi-đáp đã được người dùng đánh giá cho skill "{skill.name}".\n'
        "Hãy rút ra tối đa 6 quy tắc ngắn gọn giúp lần sau trả lời tốt hơn. "
        'Mỗi quy tắc một dòng, bắt đầu bằng "- ". Chỉ trả về danh sách quy tắc, không giải thích.\n\n'
        + examples[:MAX_INPUT_CHARS]
    )
    response = llm.chat([{"role": "user", "content": prompt}])
    rules = (response.get("content") or "").strip()
    if not rules:
        return False, f"Bỏ qua {skill.name}: model không trả về nội dung."

    _update_skill_md(skill.path / "SKILL.md", rules)
    return True, f"Đã cập nhật {skill.name}/SKILL.md ({len(rules.splitlines())} dòng bài học)."


def _update_skill_md(path: Path, rules: str) -> None:
    text = path.read_text(encoding="utf-8")
    section = f"{AUTO_START}\n## Bài học tự động\n{rules}\n{AUTO_END}"
    pattern = re.compile(re.escape(AUTO_START) + r".*?" + re.escape(AUTO_END), re.S)
    if pattern.search(text):
        text = pattern.sub(section, text)
    else:
        text = text.rstrip() + "\n\n" + section + "\n"
    path.write_text(text, encoding="utf-8")
