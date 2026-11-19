from __future__ import annotations

import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from .tools import ToolSpec

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
FETCH_TIMEOUT = 25
MAX_BYTES = 3 * 1024 * 1024
DEFAULT_MAX_CHARS = 6000
MAX_CHARS_LIMIT = 8000

_SKIP_TAGS = {
    "script", "style", "noscript", "template", "svg", "iframe", "object", "canvas",
    "nav", "aside", "footer",
}
_BLOCK_TAGS = {
    "p", "div", "br", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6",
    "section", "article", "header", "footer", "table", "ul", "ol", "blockquote",
    "pre", "hr", "title", "nav", "aside", "main", "form", "figure", "figcaption",
    "dt", "dd",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    lines = []
    for raw_line in "".join(parser.parts).splitlines():
        line = " ".join(raw_line.split())
        if line:
            lines.append(line)
    return "\n".join(lines)


def _main_content(html: str) -> str:
    for tag in ("main", "article"):
        match = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", html, re.IGNORECASE | re.DOTALL)
        if match and len(match.group(1)) > 500:
            return match.group(1)
    return html


def _normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise ValueError("URL rỗng")
    if "://" not in url:
        url = "https://" + url
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise ValueError(f"chỉ hỗ trợ http/https, không hỗ trợ '{parts.scheme}'")
    if not parts.netloc:
        raise ValueError(f"URL không hợp lệ: {url}")
    return url


def _fetch(url: str) -> tuple[bytes, str, str, str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "vi,en;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
        data = response.read(MAX_BYTES + 1)
        content_type = response.headers.get_content_type() or ""
        charset = response.headers.get_content_charset() or ""
        final_url = response.geturl()
    return data, content_type, final_url, charset


def _decode(data: bytes, charset: str = "") -> str:
    candidates = [charset] if charset else []
    probe = data[:4096].decode("ascii", errors="ignore")
    match = re.search(r"charset=[\"']?([\w-]+)", probe, re.IGNORECASE)
    if match:
        candidates.append(match.group(1))
    candidates.append("utf-8")
    for name in candidates:
        try:
            return data.decode(name, errors="replace")
        except LookupError:
            continue
    return data.decode("utf-8", errors="replace")


def build_web_tools(workspace: Path) -> list[ToolSpec]:
    root = workspace.resolve()

    def resolve(path_str: str) -> Path:
        path = Path(path_str)
        resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
        if resolved != root and root not in resolved.parents:
            raise PermissionError(f"đường dẫn ngoài workspace: {resolved}")
        return resolved

    def fetch_url(url: str, save_to: str = "", max_chars: int = DEFAULT_MAX_CHARS) -> str:
        target_url = _normalize_url(url)
        data, content_type, final_url, charset = _fetch(target_url)
        size = len(data)
        truncated_bytes = size > MAX_BYTES
        if truncated_bytes:
            data = data[:MAX_BYTES]

        if save_to:
            destination = resolve(save_to)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            note = " (file bị cắt ở 3MB)" if truncated_bytes else ""
            return (
                f"Đã lưu {size} bytes từ {final_url} → {destination}"
                f" (content-type: {content_type or 'không rõ'}){note}"
            )

        text = _decode(data, charset)
        if "html" in content_type or text.lstrip().lower().startswith(("<!doctype", "<html")):
            text = html_to_text(_main_content(text))
        text = text.strip()
        limit = max(500, min(int(max_chars or DEFAULT_MAX_CHARS), MAX_CHARS_LIMIT))
        if len(text) > limit:
            text = text[:limit] + f"\n... (đã cắt bớt, tổng {len(text)} ký tự; tăng max_chars nếu cần)"
        if truncated_bytes:
            text += "\n... (trang lớn hơn 3MB, chỉ đọc phần đầu)"
        header = f"URL: {final_url}\ncontent-type: {content_type or 'không rõ'}"
        return f"{header}\n\n{text or '(trang không có nội dung văn bản)'}"

    return [
        ToolSpec(
            name="fetch_url",
            description=(
                "Truy cập internet: tải một trang web (http/https) và trả về nội dung dạng văn bản "
                "đã lược bỏ HTML, hoặc lưu nguyên file vào workspace nếu đặt save_to."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Địa chỉ trang cần tải"},
                    "save_to": {
                        "type": "string",
                        "description": "Đường dẫn tương đối trong workspace để lưu file (bỏ trống: chỉ đọc nội dung)",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": f"Số ký tự tối đa trả về khi đọc (mặc định {DEFAULT_MAX_CHARS})",
                    },
                },
                "required": ["url"],
            },
            func=fetch_url,
        ),
    ]
