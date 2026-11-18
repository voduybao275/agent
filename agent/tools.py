from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

MAX_TOOL_OUTPUT = 8000


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., str]

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"Tool trùng tên: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self) -> list[dict]:
        return [tool.schema() for tool in self._tools.values()]

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        spec = self._tools.get(name)
        if spec is None:
            return f"ERROR: không có tool tên '{name}'. Tool hiện có: {', '.join(self.names())}"
        try:
            result = spec.func(**arguments)
        except TypeError as exc:
            return f"ERROR: sai tham số cho {name}: {exc}"
        except Exception as exc:
            return f"ERROR: {type(exc).__name__}: {exc}"
        return truncate(str(result), MAX_TOOL_OUTPUT)


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... (đã cắt bớt, tổng {len(text)} ký tự)"


def build_builtin_tools(workspace: Path, shell: str) -> list[ToolSpec]:
    root = workspace.resolve()

    def resolve(path_str: str) -> Path:
        path = Path(path_str)
        resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
        if resolved != root and root not in resolved.parents:
            raise PermissionError(f"đường dẫn ngoài workspace: {resolved}")
        return resolved

    def read_file(path: str, offset: int = 0, limit: int = 400) -> str:
        target = resolve(path)
        if not target.is_file():
            return f"ERROR: không phải file: {target}"
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(0, offset)
        end = start + max(1, limit)
        chunk = lines[start:end]
        numbered = "\n".join(f"{i + start + 1:>5}\t{line}" for i, line in enumerate(chunk))
        if end < len(lines):
            numbered += f"\n... (còn {len(lines) - end} dòng, dùng offset={end} để đọc tiếp)"
        return numbered or "(file rỗng)"

    def write_file(path: str, content: str) -> str:
        target = resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        target.write_text(content, encoding="utf-8", newline="\n")
        return f"{'Đã ghi đè' if existed else 'Đã tạo'} {target} ({len(content)} ký tự)"

    def list_dir(path: str = ".", recursive: bool = False, max_entries: int = 200) -> str:
        target = resolve(path)
        if not target.is_dir():
            return f"ERROR: không phải thư mục: {target}"
        ignored = {".git", "__pycache__", ".venv", "venv", "node_modules"}
        entries: list[str] = []
        iterator = target.rglob("*") if recursive else target.iterdir()
        for item in sorted(iterator):
            parts = item.relative_to(target).parts
            if any(part in ignored for part in parts):
                continue
            entries.append("/".join(parts) + ("/" if item.is_dir() else ""))
            if len(entries) >= max_entries:
                entries.append("... (còn nữa)")
                break
        return "\n".join(entries) or "(thư mục rỗng)"

    def run_shell(command: str, timeout: int = 60) -> str:
        try:
            if shell == "bash":
                argv: list[str] | str = ["bash", "-lc", command]
                proc = subprocess.run(
                    argv, cwd=root, capture_output=True, text=True,
                    errors="replace", timeout=timeout,
                )
            elif shell == "powershell":
                proc = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", command],
                    cwd=root, capture_output=True, text=True,
                    errors="replace", timeout=timeout,
                )
            else:
                proc = subprocess.run(
                    command, shell=True, cwd=root, capture_output=True, text=True,
                    errors="replace", timeout=timeout,
                )
        except subprocess.TimeoutExpired:
            return f"ERROR: lệnh quá {timeout}s và bị hủy."
        except FileNotFoundError as exc:
            return f"ERROR: không tìm thấy shell '{shell}': {exc}"

        parts = [f"exit_code={proc.returncode} (cwd={root})"]
        if proc.stdout.strip():
            parts.append("stdout:\n" + proc.stdout.rstrip())
        if proc.stderr.strip():
            parts.append("stderr:\n" + proc.stderr.rstrip())
        return "\n".join(parts)

    return [
        ToolSpec(
            name="read_file",
            description="Đọc file văn bản trong workspace, trả về nội dung kèm số dòng.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Đường dẫn tương đối với workspace"},
                    "offset": {"type": "integer", "description": "Dòng bắt đầu (0-based)"},
                    "limit": {"type": "integer", "description": "Số dòng tối đa (mặc định 400)"},
                },
                "required": ["path"],
            },
            func=read_file,
        ),
        ToolSpec(
            name="write_file",
            description="Tạo mới hoặc ghi đè file trong workspace. Luôn ghi toàn bộ nội dung file.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Đường dẫn tương đối với workspace"},
                    "content": {"type": "string", "description": "Toàn bộ nội dung file"},
                },
                "required": ["path", "content"],
            },
            func=write_file,
        ),
        ToolSpec(
            name="list_dir",
            description="Liệt kê file/thư mục trong workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Thư mục cần liệt kê (mặc định '.')"},
                    "recursive": {"type": "boolean", "description": "Liệt kê đệ quy"},
                    "max_entries": {"type": "integer", "description": "Số mục tối đa (mặc định 200)"},
                },
            },
            func=list_dir,
        ),
        ToolSpec(
            name="run_shell",
            description=f"Chạy lệnh shell ({shell}) trong workspace và trả về stdout/stderr/exit code.",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Lệnh cần chạy"},
                    "timeout": {"type": "integer", "description": "Timeout giây (mặc định 60)"},
                },
                "required": ["command"],
            },
            func=run_shell,
        ),
    ]
