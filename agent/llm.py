from __future__ import annotations

import json
import urllib.error
import urllib.request


class LLMError(RuntimeError):
    pass


class LLM:
    """Client cho mọi endpoint OpenAI-compatible (OpenAI, Ollama, OpenRouter, DeepSeek...)."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str = "",
        temperature: float = 0.2,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:2000]
            raise LLMError(f"HTTP {exc.code} từ {self.base_url}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(
                f"Không kết nối được tới {self.base_url}: {exc.reason}. "
                "Kiểm tra base_url và server (Ollama đã chạy chưa?)."
            ) from exc
        except TimeoutError as exc:
            raise LLMError(f"Timeout sau {self.timeout}s khi gọi {self.base_url}.") from exc

        choices = body.get("choices") or []
        if not choices:
            raise LLMError(f"Phản hồi không có choices: {json.dumps(body, ensure_ascii=False)[:1000]}")

        message = choices[0].get("message") or {}
        return {
            "role": "assistant",
            "content": message.get("content"),
            "tool_calls": message.get("tool_calls") or [],
        }
