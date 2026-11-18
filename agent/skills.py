from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path

from .tools import ToolRegistry, ToolSpec

EXAMPLES_FILE = "examples.md"
EXAMPLES_HEADER = "# Ví dụ đã học (tự động sinh bởi lệnh learn)\n"
EXAMPLE_MARKER = "<!-- example -->"
MAX_EXAMPLE_CONTEXT = 2500


@dataclass
class Skill:
    name: str
    description: str
    when_to_use: str
    path: Path
    tools_loaded: bool = False

    def read_body(self) -> str:
        text = (self.path / "SKILL.md").read_text(encoding="utf-8")
        _, body = parse_frontmatter(text)
        return body.strip() or "(SKILL.md rỗng)"


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    block = text[3:end]
    body = text[end + 4:]
    meta: dict[str, str] = {}
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip().lower()] = value.strip().strip("'\"")
    return meta, body


def _example_entries(skill_path: Path) -> list[str]:
    file = skill_path / EXAMPLES_FILE
    if not file.is_file():
        return []
    text = file.read_text(encoding="utf-8", errors="replace")
    return [part.strip() for part in text.split(EXAMPLE_MARKER)[1:] if part.strip()]


def examples_text(skill_path: Path) -> str:
    return "\n\n".join(_example_entries(skill_path))


def recent_examples(skill_path: Path, limit: int) -> str:
    if limit <= 0:
        return ""
    text = "\n\n".join(_example_entries(skill_path)[-limit:])
    return text[:MAX_EXAMPLE_CONTEXT]


class SkillManager:
    """Quét thư mục skills, chỉ inject metadata vào prompt; body + tool nạp khi LLM gọi load_skill."""

    def __init__(self, skills_dir: Path, examples_in_context: int = 3) -> None:
        self.skills_dir = skills_dir
        self.examples_in_context = examples_in_context
        self.skills: dict[str, Skill] = {}
        self._discover()

    def _discover(self) -> None:
        if not self.skills_dir.is_dir():
            return
        for directory in sorted(self.skills_dir.iterdir()):
            skill_file = directory / "SKILL.md"
            if not skill_file.is_file():
                continue
            try:
                meta, _ = parse_frontmatter(skill_file.read_text(encoding="utf-8"))
            except OSError as exc:
                print(f"[skills] bỏ qua {directory.name}: {exc}")
                continue
            name = (meta.get("name") or directory.name).strip()
            self.skills[name] = Skill(
                name=name,
                description=meta.get("description", ""),
                when_to_use=meta.get("when_to_use", ""),
                path=directory,
            )

    def names(self) -> list[str]:
        return sorted(self.skills)

    def reset_tool_state(self) -> None:
        for skill in self.skills.values():
            skill.tools_loaded = False

    def catalog(self) -> str:
        if not self.skills:
            return "(chưa có skill nào)"
        lines: list[str] = []
        for skill in self.skills.values():
            entry = f"- {skill.name}: {skill.description}"
            if skill.when_to_use:
                entry += f" (dùng khi: {skill.when_to_use})"
            lines.append(entry)
        return "\n".join(lines)

    def load(self, name: str, registry: ToolRegistry) -> str:
        skill = self.skills.get(name)
        if skill is None:
            available = ", ".join(self.names()) or "(không có)"
            return f"ERROR: không có skill '{name}'. Skill hiện có: {available}"

        body = skill.read_body()
        parts = [f"# Skill: {skill.name}", body]
        examples = recent_examples(skill.path, self.examples_in_context)
        if examples:
            parts.append("## Ví dụ đã học (tham khảo cách trả lời tốt)\n\n" + examples)

        tools_py = skill.path / "tools.py"
        if tools_py.is_file() and not skill.tools_loaded:
            registered = self._load_tools(skill, tools_py, registry)
            skill.tools_loaded = True
            if registered:
                parts.append(
                    f"(Đã nạp tool của skill: {', '.join(registered)} — có thể dùng từ bước tiếp theo.)"
                )
        return "\n\n".join(parts)

    def _load_tools(self, skill: Skill, tools_py: Path, registry: ToolRegistry) -> list[str]:
        module_name = f"agent_skill_{skill.name.replace('-', '_')}"
        spec = importlib.util.spec_from_file_location(module_name, tools_py)
        if spec is None or spec.loader is None:
            return []
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            print(f"[skills] lỗi import tools.py của skill '{skill.name}': {exc}")
            return []

        registered: list[str] = []
        for item in getattr(module, "TOOLS", []):
            if not isinstance(item, ToolSpec):
                print(f"[skills] {skill.name}/tools.py: TOOLS phải chứa ToolSpec, bỏ qua {item!r}")
                continue
            try:
                registry.register(item)
                registered.append(item.name)
            except ValueError as exc:
                print(f"[skills] {exc}")
        return registered

    def register_load_tool(self, registry: ToolRegistry) -> None:
        def load_skill(name: str) -> str:
            return self.load(name, registry)

        names = self.names()
        name_schema: dict = {"type": "string", "description": "Tên skill cần nạp"}
        if names:
            name_schema["enum"] = names

        registry.register(
            ToolSpec(
                name="load_skill",
                description=(
                    "Nạp nội dung đầy đủ của một skill cùng tool đi kèm. "
                    "Gọi TRƯỚC khi làm theo hướng dẫn của skill."
                ),
                parameters={
                    "type": "object",
                    "properties": {"name": name_schema},
                    "required": ["name"],
                },
                func=load_skill,
            )
        )
