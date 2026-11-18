from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Callable

from .llm import LLM
from .tools import ToolRegistry
from .vision import UI_BUILD_INSTRUCTION, UI_PROMPT, Vision, is_ui_request

MAX_RESULT_CHARS = 12000
UI_ANSWER_TOKENS = 1200
_SAVE_PATTERN = re.compile(r"\b(luu|save|ghi file|xuat file)\b|\.html\b")


def _save_requested(text: str) -> bool:
    folded = text.lower().replace("đ", "d")
    folded = "".join(
        char for char in unicodedata.normalize("NFD", folded)
        if unicodedata.category(char) != "Mn"
    )
    return bool(_SAVE_PATTERN.search(folded))


class Agent:
    def __init__(
        self,
        llm: LLM,
        registry: ToolRegistry,
        system_prompt: str,
        max_steps: int = 12,
        verbose: bool = True,
        on_event: Callable[[dict], None] | None = None,
        vision: Vision | None = None,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.max_steps = max_steps
        self.verbose = verbose
        self.on_event = on_event
        self.vision = vision
        self._system_prompt = system_prompt
        self.messages: list[dict] = [{"role": "system", "content": system_prompt}]
        self.last_used_skills: list[str] = []

    def _emit(self, event: dict) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(event)
        except Exception:
            pass

    def reset(self) -> None:
        self.messages = [{"role": "system", "content": self._system_prompt}]

    def update_system_prompt(self, system_prompt: str) -> None:
        self._system_prompt = system_prompt
        self.reset()

    def run(self, user_input: str, images: list[str] | None = None) -> str:
        self.last_used_skills = []
        rebuild_only = False
        if images:
            if self.vision is None:
                raise RuntimeError(
                    "Chưa cấu hình chức năng đọc ảnh: đặt vision_model trong [providers.*] của config.toml."
                )
            rebuild_ui = is_ui_request(user_input)
            rebuild_only = rebuild_ui and not _save_requested(user_input)
            self.messages.append(
                {"role": "user", "content": user_input.strip() or "Hãy đọc và mô tả hình ảnh này."}
            )
            for index, image in enumerate(images, 1):
                note = {
                    "call_id": f"image_{index}",
                    "name": Path(image).name,
                    "description": self.vision.describe(
                        image,
                        question=UI_PROMPT if rebuild_ui else "",
                        max_tokens=350 if rebuild_ui else None,
                    ),
                }
                self.messages.append(
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": note["call_id"],
                                "type": "function",
                                "function": {
                                    "name": "read_image",
                                    "arguments": json.dumps({"path": note["name"]}, ensure_ascii=False),
                                },
                            }
                        ],
                    }
                )
                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": note["call_id"],
                        "content": f"Đã đọc ảnh {note['name']}:\n{note['description']}",
                    }
                )
            if rebuild_ui:
                self.messages.append({"role": "user", "content": UI_BUILD_INSTRUCTION})
            self._emit({"type": "vision_done", "count": len(images), "ui": rebuild_ui})
        else:
            self.messages.append({"role": "user", "content": user_input})

        for step in range(1, self.max_steps + 1):
            schemas = None if rebuild_only else (self.registry.schemas() or None)
            message = self.llm.chat(
                self.messages,
                tools=schemas,
                max_tokens=UI_ANSWER_TOKENS if rebuild_only and step == 1 else None,
            )
            tool_calls = message.get("tool_calls") or []
            # qwen2.5:3b đôi khi bịa tool call rỗng khi có tools, Ollama trả content rỗng.
            if schemas and not tool_calls and not (message.get("content") or "").strip():
                if self.verbose:
                    print("  [retry] phản hồi rỗng khi có tools → gọi lại không tools")
                retry = self.llm.chat(
                    self.messages,
                    tools=None,
                    max_tokens=UI_ANSWER_TOKENS if rebuild_only else None,
                )
                if not (retry.get("tool_calls") or []) and (retry.get("content") or "").strip():
                    message = retry
                    tool_calls = []
            self.messages.append(message)

            if not tool_calls:
                content = (message.get("content") or "").strip()
                return content or "(model không trả về nội dung)"

            for index, call in enumerate(tool_calls):
                function = call.get("function") or {}
                name = function.get("name") or ""
                raw_args = function.get("arguments") or "{}"
                try:
                    arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    if not isinstance(arguments, dict):
                        arguments = {"value": arguments}
                except json.JSONDecodeError as exc:
                    result = f"ERROR: arguments không phải JSON hợp lệ ({exc})"
                    arguments = {}
                else:
                    if self.verbose:
                        print(f"  [tool] {name}({_clip(json.dumps(arguments, ensure_ascii=False), 160)})")
                    self._emit({"type": "tool_start", "name": name, "arguments": arguments})
                    result = self.registry.execute(name, arguments)
                    if (
                        name == "load_skill"
                        and isinstance(arguments.get("name"), str)
                        and arguments["name"] not in self.last_used_skills
                    ):
                        self.last_used_skills.append(arguments["name"])
                    if self.verbose:
                        print(f"  [result] {_clip(result, 200)}")
                    self._emit({"type": "tool_result", "name": name, "result": result})

                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id") or f"call_{step}_{index}",
                        "content": result[:MAX_RESULT_CHARS],
                    }
                )

        return f"Đã đạt giới hạn {self.max_steps} bước. Dừng để tránh lặp vô hạn."


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"
