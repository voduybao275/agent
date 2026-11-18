from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

MAX_WORKSPACES = 30
INVALID_NAME_CHARS = '<>:"/\\|?*'


class WorkspaceManager:
    """Danh sách workspace của GUI, lưu trong một file JSON."""

    def __init__(self, path: str | Path, default: str | Path) -> None:
        self.path = Path(path)
        self.default = Path(default).resolve()

    def ensure_active(self) -> Path:
        """Trả về workspace đang mở; tạo mục mặc định nếu danh sách trống hoặc workspace cũ đã mất."""
        data = self._read()
        active = self._find(data, data.get("active_id", ""))
        if active is not None and Path(active["path"]).is_dir():
            return Path(active["path"])
        workspace = next(
            (item for item in data["workspaces"] if Path(item["path"]) == self.default),
            None,
        )
        if workspace is None:
            workspace = self._add(data, self.default, self.default.name or "workspace")
        data["active_id"] = workspace["id"]
        self._write(data)
        return Path(workspace["path"])

    def list(self) -> list[dict]:
        data = self._read()
        active_id = data.get("active_id", "")
        return [
            {
                "id": workspace["id"],
                "name": workspace["name"],
                "path": workspace["path"],
                "created_at": workspace.get("created_at", ""),
                "active": workspace["id"] == active_id,
                "exists": Path(workspace["path"]).is_dir(),
            }
            for workspace in data["workspaces"]
        ]

    def get(self, workspace_id: str) -> dict | None:
        workspace = self._find(self._read(), workspace_id)
        if workspace is None:
            return None
        return {
            "id": workspace["id"],
            "name": workspace["name"],
            "path": workspace["path"],
            "exists": Path(workspace["path"]).is_dir(),
        }

    def set_active(self, workspace_id: str) -> bool:
        data = self._read()
        if self._find(data, workspace_id) is None:
            return False
        data["active_id"] = workspace_id
        self._write(data)
        return True

    def create(self, parent: str | Path, name: str) -> dict:
        clean = name.strip()
        _validate_name(clean)
        target = Path(parent) / clean
        if target.exists() and not target.is_dir():
            raise ValueError(f"Đã có file cùng tên: {target}")
        target.mkdir(parents=True, exist_ok=True)
        return self.add(target, clean)

    def add(self, path: str | Path, name: str | None = None, activate: bool = True) -> dict:
        resolved = Path(path).resolve()
        if not resolved.is_dir():
            raise ValueError(f"Không phải thư mục: {resolved}")
        data = self._read()
        for existing in data["workspaces"]:
            if Path(existing["path"]) == resolved:
                if activate:
                    data["active_id"] = existing["id"]
                    self._write(data)
                return dict(existing)
        workspace = self._add(data, resolved, (name or "").strip() or resolved.name or "workspace")
        if activate:
            data["active_id"] = workspace["id"]
        self._write(data)
        return dict(workspace)

    def remove(self, workspace_id: str) -> bool:
        data = self._read()
        remaining = [workspace for workspace in data["workspaces"] if workspace["id"] != workspace_id]
        if len(remaining) == len(data["workspaces"]):
            return False
        data["workspaces"] = remaining
        if data.get("active_id") == workspace_id:
            data["active_id"] = ""
        self._write(data)
        return True

    def _add(self, data: dict, path: Path, name: str) -> dict:
        workspace = {
            "id": _now_id(),
            "name": _unique_name(data, name),
            "path": str(path),
            "created_at": _now(),
        }
        data["workspaces"].append(workspace)
        if len(data["workspaces"]) > MAX_WORKSPACES:
            keep = [item for item in data["workspaces"] if item["id"] == data.get("active_id")]
            others = [
                item for item in data["workspaces"]
                if item["id"] != data.get("active_id")
            ][-MAX_WORKSPACES + len(keep):]
            data["workspaces"] = keep + others
        return workspace

    def _find(self, data: dict, workspace_id: str) -> dict | None:
        for workspace in data["workspaces"]:
            if workspace["id"] == workspace_id:
                return workspace
        return None

    def _read(self) -> dict:
        empty = {"active_id": "", "workspaces": []}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return empty
        if not isinstance(raw, dict) or not isinstance(raw.get("workspaces"), list):
            return empty
        workspaces = [
            workspace
            for workspace in (_clean(item) for item in raw["workspaces"])
            if workspace is not None
        ]
        active_id = raw.get("active_id") if isinstance(raw.get("active_id"), str) else ""
        return {"active_id": active_id, "workspaces": workspaces}

    def _write(self, data: dict) -> None:
        active_id = data.get("active_id") or ""
        if not any(workspace["id"] == active_id for workspace in data["workspaces"]):
            active_id = ""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = {"updated_at": _now(), "active_id": active_id, "workspaces": data["workspaces"]}
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)


def _validate_name(name: str) -> None:
    if not name:
        raise ValueError("Tên workspace không được để trống.")
    if name in (".", "..") or any(char in name for char in INVALID_NAME_CHARS):
        raise ValueError('Tên workspace không được chứa < > : " / \\ | ? *')
    if name != name.rstrip(" ."):
        raise ValueError("Tên workspace không được kết thúc bằng dấu chấm hoặc khoảng trắng.")
    if name.split(".")[0].upper() in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        raise ValueError(f"'{name}' là tên dành riêng của Windows.")


def _unique_name(data: dict, name: str) -> str:
    used = {workspace["name"].lower() for workspace in data["workspaces"]}
    if name.lower() not in used:
        return name
    index = 2
    while f"{name} ({index})".lower() in used:
        index += 1
    return f"{name} ({index})"


def _clean(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    path = value.get("path")
    if not isinstance(path, str) or not path.strip():
        return None
    name = value.get("name") if isinstance(value.get("name"), str) else ""
    workspace_id = value.get("id") if isinstance(value.get("id"), str) else ""
    created_at = value.get("created_at") if isinstance(value.get("created_at"), str) else ""
    return {
        "id": workspace_id or _now_id(),
        "name": name.strip() or Path(path).name or "workspace",
        "path": path,
        "created_at": created_at,
    }


def _now() -> str:
    return datetime.now().isoformat(timespec="microseconds")


def _now_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")
