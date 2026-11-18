from __future__ import annotations

import base64
import mimetypes
import re
import unicodedata
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

UI_PROMPT = (
    "Mô tả ảnh giao diện để dựng lại bằng HTML/CSS. Trả lời tối đa 12 dòng, không viết code:\n"
    "- CHỮ: toàn bộ chữ nhìn thấy, nguyên văn, cách nhau bằng dấu phẩy.\n"
    "- BỐ CỤC: các khối chính, tỉ lệ, khoảng cách ước lượng.\n"
    "- MÀU: mã hex gần đúng cho nền, chữ, nút, viền.\n"
    "- FONT & BO GÓC: sans/serif, đậm/nhạt, bo góc (px).\n"
    "Chỉ nêu điều thấy trong ảnh; không lặp lại giá trị; chỗ không chắc ghi 'ước lượng'. "
    "Trả lời trực tiếp, không nhắc lại yêu cầu."
)

UI_BUILD_INSTRUCTION = (
    "Ảnh ở trên là mẫu giao diện cần dựng lại. Yêu cầu:\n"
    "- Trả lời bằng MỘT file HTML duy nhất, CSS nằm trong <style>, không tải tài nguyên ngoài "
    "(font hệ thống; icon bằng emoji/CSS/SVG đơn giản; ảnh thay bằng khối màu hoặc gradient).\n"
    "- Giữ nguyên toàn bộ chữ nhìn thấy, đúng thứ tự và phân cấp; bám sát bố cục, màu sắc, "
    "bo góc và khoảng cách theo đặc tả ở trên.\n"
    "- Dùng thẻ semantic (header/nav/main/section/footer) và responsive cơ bản.\n"
    "- Viết code gọn (khoảng 120 dòng), không lặp thuộc tính, không giải thích trong code.\n"
    "- Nếu người dùng yêu cầu lưu file, dùng write_file để ghi file trong workspace; nếu không, "
    "trả code trong một khối ```html duy nhất.\n"
    "- Kết thúc bằng 2-3 gạch đầu dòng nêu phần đã ước lượng (màu, font, khoảng cách)."
)

_UI_PATTERN = re.compile(
    r"\b(html|css|website|web|landing|dashboard|giao dien|trang|form|template|mockup|"
    r"ui|ux|component|layout|dung lai|tai tao)\b"
)


def _fold(text: str) -> str:
    lowered = text.lower().replace("đ", "d")
    return "".join(
        char for char in unicodedata.normalize("NFD", lowered)
        if unicodedata.category(char) != "Mn"
    )


def is_ui_request(text: str) -> bool:
    """True khi yêu cầu có khả năng cần dựng lại giao diện từ ảnh."""
    return bool(_UI_PATTERN.search(_fold(text or "")))


class Vision:
    """Đọc ảnh (mô tả + OCR) bằng model vision qua endpoint OpenAI-compatible."""

    def __init__(self, llm: LLM) -> None:
        self.llm = llm

    def describe(
        self, image_path: str | Path, question: str = "", max_tokens: int | None = None
    ) -> str:
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
            ],
            max_tokens=max_tokens,
        )
        text = (reply.get("content") or "").strip()
        if not text:
            raise LLMError(f"model vision '{self.llm.model}' không trả về nội dung cho {path.name}")
        return text
