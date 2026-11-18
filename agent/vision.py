from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

from .llm import LLM, LLMError

SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
MAX_IMAGE_BYTES = 20 * 1024 * 1024

DEFAULT_PROMPT = (
    "Đọc hình ảnh theo 2 bước. "
    "Bước 1: ghi lại nguyên văn toàn bộ chữ trong ảnh, từng dòng, đúng ngôn ngữ gốc. "
    "Bước 2: mô tả bố cục, thành phần, màu sắc và các đối tượng; nếu là giao diện "
    "hoặc sơ đồ thì mô tả cấu trúc và nhãn. "
    "Trả lời trực tiếp, không nhắc lại yêu cầu."
)


class Vision:
    """Đọc ảnh (mô tả + OCR) bằng model vision qua endpoint OpenAI-compatible."""

    def __init__(self, llm: LLM) -> None:
        self.llm = llm

    def describe(self, image_path: str | Path, question: str = "") -> str:
        path = Path(image_path)
        if not path.is_file():
            raise FileNotFoundError(f"không tìm thấy file ảnh: {path}")
        extension = path.suffix.lower()
        if extension not in SUPPORTED_EXTENSIONS:
            raise ValueError(
                f"định dạng ảnh không hỗ trợ: {extension or '(không có đuôi)'} "
                f"(hỗ trợ: {', '.join(sorted(SUPPORTED_EXTENSIONS))})"
            )
        size = path.stat().st_size
        if size > MAX_IMAGE_BYTES:
            raise ValueError(f"ảnh quá lớn: {size / 1024 / 1024:.1f} MB (tối đa 20 MB)")

        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        data_url = f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"

        prompt = (question or "").strip()
        if prompt:
            prompt = f"{prompt}\nNếu ảnh có chữ, ghi lại nguyên văn. Trả lời trực tiếp, không nhắc lại yêu cầu."
        else:
            prompt = DEFAULT_PROMPT

        reply = self.llm.chat(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ]
        )
        text = (reply.get("content") or "").strip()
        if not text:
            raise LLMError(f"model vision '{self.llm.model}' không trả về nội dung cho {path.name}")
        return text
