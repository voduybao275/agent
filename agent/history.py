from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

MAX_CONVERSATIONS = 50


class ChatHistory:
    """Lưu nhiều cuộc hội thoại GUI vào một file JSON, tách theo workspace."""

    def __init__(self, path: str | Path, max_turns: int = 20, legacy_workspace: str = "") -> None:
        self.path = Path(path)
        self.max_turns = int(max_turns)
        self.legacy_workspace = _key(legacy_workspace)

    def load(self, workspace: str) -> list[dict]:
        if self.max_turns <= 0:
            return []
        conversation = self._active(self._read(), workspace)
        if conversation is None:
            return []
        return conversation["turns"][-self.max_turns:]

    def append_turn(
        self,
        question: str,
        answer: str,
        skills: list[str] | None = None,
        created_at: str | None = None,
        workspace: str = "",
    ) -> None:
        if self.max_turns <= 0:
            return
        data = self._read()
        conversation = self._active(data, workspace, create=True)
        conversation["turns"].append(
            {
                "question": question,
                "answer": answer,
                "skills": [str(name) for name in (skills or [])],
                "created_at": created_at or _now(),
            }
        )
        conversation["turns"] = conversation["turns"][-self.max_turns:]
        conversation["updated_at"] = _now()
        self._write(data)

    def start_new(self, workspace: str) -> bool:
        """Bỏ chọn cuộc đang mở của workspace để cuộc đó được lưu; trả về True nếu có cuộc vừa lưu."""
        if self.max_turns <= 0:
            return False
        data = self._read()
        key = _key(workspace)
        active = self._active(data, key)
        archived = active is not None and bool(active["turns"])
        if archived:
            data["active_by_workspace"].pop(key, None)
        self._write(data)
        return archived

    def conversations(self, workspace: str) -> list[dict]:
        if self.max_turns <= 0:
            return []
        data = self._read()
        key = _key(workspace)
        active_id = data["active_by_workspace"].get(key, "")
        items = sorted(
            (
                conversation
                for conversation in data["conversations"]
                if conversation["workspace"] == key and conversation["turns"]
            ),
            key=lambda conversation: conversation.get("updated_at") or conversation.get("created_at") or "",
            reverse=True,
        )
        return [
            {
                "id": conversation["id"],
                "updated_at": conversation.get("updated_at", ""),
                "created_at": conversation.get("created_at", ""),
                "count": len(conversation["turns"]),
                "preview": conversation["turns"][0]["question"],
                "active": conversation["id"] == active_id,
            }
            for conversation in items
        ]

    def counts(self) -> dict[str, int]:
        """Số cuộc hội thoại theo từng workspace (khóa là đường dẫn đã resolve)."""
        counts: dict[str, int] = {}
        for conversation in self._read()["conversations"]:
            counts[conversation["workspace"]] = counts.get(conversation["workspace"], 0) + 1
        return counts

    def open(self, conversation_id: str) -> list[dict]:
        if self.max_turns <= 0:
            return []
        data = self._read()
        for conversation in data["conversations"]:
            if conversation["id"] == conversation_id and conversation["turns"]:
                data["active_by_workspace"][conversation["workspace"]] = conversation_id
                self._write(data)
                return conversation["turns"][-self.max_turns:]
        return []

    def delete(self, conversation_id: str) -> bool:
        if self.max_turns <= 0:
            return False
        data = self._read()
        remaining = [
            conversation
            for conversation in data["conversations"]
            if conversation["id"] != conversation_id
        ]
        if len(remaining) == len(data["conversations"]):
            return False
        data["conversations"] = remaining
        data["active_by_workspace"] = {
            key: value
            for key, value in data["active_by_workspace"].items()
            if value != conversation_id
        }
        self._write(data)
        return True

    def clear_all(self, workspace: str | None = None) -> None:
        """Xóa lịch sử của một workspace; workspace=None xóa toàn bộ."""
        if self.max_turns <= 0:
            return
        data = self._read()
        if workspace is None:
            data = {"active_by_workspace": {}, "conversations": []}
        else:
            key = _key(workspace)
            data["conversations"] = [
                conversation
                for conversation in data["conversations"]
                if conversation["workspace"] != key
            ]
            data["active_by_workspace"].pop(key, None)
        self._write(data)

    def _active(self, data: dict, workspace: str, create: bool = False) -> dict | None:
        key = _key(workspace)
        active_id = data["active_by_workspace"].get(key, "")
        for conversation in data["conversations"]:
            if conversation["id"] == active_id and conversation["workspace"] == key:
                return conversation
        if not create:
            return None
        conversation = {
            "id": _now_id(),
            "workspace": key,
            "created_at": _now(),
            "updated_at": _now(),
            "turns": [],
        }
        data["conversations"].append(conversation)
        data["active_by_workspace"][key] = conversation["id"]
        return conversation

    def _read(self) -> dict:
        empty = {"active_by_workspace": {}, "conversations": []}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return empty
        if not isinstance(raw, dict):
            return empty

        raw_conversations = raw.get("conversations")
        if isinstance(raw_conversations, list):
            conversations = [
                conversation
                for conversation in (_clean(item) for item in raw_conversations)
                if conversation and conversation["turns"]
            ]
            for conversation in conversations:
                if not conversation["workspace"]:
                    conversation["workspace"] = self.legacy_workspace
            mapping = _clean_mapping(raw.get("active_by_workspace"))
            if not mapping:
                legacy_active = raw.get("active_id")
                if isinstance(legacy_active, str) and legacy_active:
                    mapping = {
                        conversation["workspace"]: legacy_active
                        for conversation in conversations
                        if conversation["id"] == legacy_active
                    }
            valid_ids = {conversation["id"] for conversation in conversations}
            mapping = {key: value for key, value in mapping.items() if value in valid_ids}
            return {"active_by_workspace": mapping, "conversations": conversations}

        legacy_turns = raw.get("turns")
        if isinstance(legacy_turns, list):
            turns = [turn for turn in legacy_turns if _valid(turn)]
            if turns:
                stamp = raw.get("updated_at") if isinstance(raw.get("updated_at"), str) else ""
                conversation = _clean(
                    {
                        "id": stamp or _now_id(),
                        "workspace": self.legacy_workspace,
                        "created_at": turns[0].get("created_at") or stamp,
                        "updated_at": stamp or turns[-1].get("created_at"),
                        "turns": turns,
                    }
                )
                return {
                    "active_by_workspace": {conversation["workspace"]: conversation["id"]},
                    "conversations": [conversation],
                }
        return empty

    def _write(self, data: dict) -> None:
        conversations = [conversation for conversation in data["conversations"] if conversation["turns"]]
        conversations.sort(
            key=lambda conversation: conversation.get("updated_at") or conversation.get("created_at") or ""
        )
        if len(conversations) > MAX_CONVERSATIONS:
            conversations = conversations[-MAX_CONVERSATIONS:]

        valid_ids = {conversation["id"] for conversation in conversations}
        active_map = {
            key: value
            for key, value in data.get("active_by_workspace", {}).items()
            if isinstance(key, str) and isinstance(value, str) and value in valid_ids
        }

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = {
            "updated_at": _now(),
            "active_by_workspace": active_map,
            "conversations": conversations,
        }
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(tmp, self.path)


def _key(workspace: str) -> str:
    if not workspace:
        return ""
    try:
        return str(Path(workspace).resolve())
    except OSError:
        return str(Path(workspace))


def _clean_mapping(value) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        _key(key): item
        for key, item in value.items()
        if isinstance(key, str) and key and isinstance(item, str) and item
    }


def _clean(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    turns = value.get("turns")
    if not isinstance(turns, list):
        return None
    valid = [turn for turn in turns if _valid(turn)]
    conversation_id = value.get("id")
    if not isinstance(conversation_id, str) or not conversation_id:
        conversation_id = _now_id()
    workspace = value.get("workspace")
    if not isinstance(workspace, str):
        workspace = ""
    created_at = value.get("created_at") if isinstance(value.get("created_at"), str) else ""
    updated_at = value.get("updated_at") if isinstance(value.get("updated_at"), str) else ""
    return {
        "id": conversation_id,
        "workspace": workspace,
        "created_at": created_at or (valid[0].get("created_at", "") if valid else ""),
        "updated_at": updated_at or (valid[-1].get("created_at", "") if valid else ""),
        "turns": valid,
    }


def _valid(turn) -> bool:
    return (
        isinstance(turn, dict)
        and isinstance(turn.get("question"), str)
        and turn["question"].strip() != ""
        and isinstance(turn.get("answer"), str)
        and turn["answer"].strip() != ""
    )


def _now() -> str:
    return datetime.now().isoformat(timespec="microseconds")


def _now_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")
