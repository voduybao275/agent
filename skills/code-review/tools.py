from __future__ import annotations

import os
import subprocess
from pathlib import Path

from agent.tools import ToolSpec


def _git_diff(path: str = ".", staged: bool = False) -> str:
    workspace = Path(os.environ.get("AGENT_WORKSPACE", "."))
    args = ["git", "diff"]
    if staged:
        args.append("--staged")
    args += ["--", path]
    try:
        proc = subprocess.run(
            args, cwd=workspace, capture_output=True, text=True,
            errors="replace", timeout=30,
        )
    except FileNotFoundError:
        return "ERROR: không tìm thấy git trong PATH."
    except subprocess.TimeoutExpired:
        return "ERROR: git diff quá 30s."
    if proc.returncode not in (0, 1):
        return f"ERROR: git diff trả về {proc.returncode}: {proc.stderr.strip()}"
    return proc.stdout.strip() or "(không có thay đổi)"


TOOLS = [
    ToolSpec(
        name="git_diff",
        description="Xem git diff trong workspace, dùng khi review thay đổi chưa commit.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Giới hạn diff theo đường dẫn (mặc định toàn workspace)"},
                "staged": {"type": "boolean", "description": "Xem diff của phần đã staged"},
            },
        },
        func=_git_diff,
    )
]
