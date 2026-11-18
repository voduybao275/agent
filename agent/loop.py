from __future__ import annotations

import json
from typing import Callable

from .llm import LLM
from .tools import ToolRegistry

MAX_RESULT_CHARS = 12000


class Agent:
    def __init__(
        self,
        llm: LLM,
        registry: ToolRegistry,
        system_prompt: str,
        max_steps: int = 12,
        verbose: bool = True,
        on_event: Callable[[dict], None] | None = None,
    ) -> None:
        self.llm = llm
        self.registry = registry
        self.max_steps = max_steps
        self.verbose = verbose
        self.on_event = on_event
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

    def run(self, user_input: str) -> str:
        self.last_used_skills = []
        self.messages.append({"role": "user", "content": user_input})

        for step in range(1, self.max_steps + 1):
            message = self.llm.chat(self.messages, tools=self.registry.schemas() or None)
            self.messages.append(message)

            tool_calls = message.get("tool_calls") or []
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
