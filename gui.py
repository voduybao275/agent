from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Callable

from agent.clipboard import grab_clipboard_image
from agent.config import Config
from agent.history import ChatHistory
from agent.llm import LLM
from agent.loop import Agent
from agent.memory import Memory
from agent.prompt import build_system_prompt
from agent.skills import SkillManager
from agent.tools import ToolRegistry, build_builtin_tools, build_image_tool
from agent.vision import Vision
from agent.web import build_web_tools
from agent.workspaces import WorkspaceManager
from learning.distill import distill_skill
from learning.export import export_dpo, export_sft
from learning.feedback import apply_feedback

BG = "#f4f5f7"
CARD = "#ffffff"
SOFT = "#f8f9fb"
BORDER = "#d9dde3"
ACCENT = "#2563eb"
ACCENT_DARK = "#1d4ed8"
FG = "#1f2328"
MUTED = "#68707c"
DANGER = "#b42318"
FONT = "Segoe UI"
SIDE_WIDTH = 320

PROJECT_DIR = Path(__file__).resolve().parent
OLLAMA_EXE = PROJECT_DIR / ".tools" / "ollama" / "ollama.exe"
OLLAMA_MODELS_DIR = PROJECT_DIR / ".ollama-models"
OLLAMA_LOG = PROJECT_DIR / "data" / "ollama-serve.log"


def _server_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    return base + "/api/version"


def _server_up(url: str, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


def _find_ollama_exe() -> Path | None:
    if OLLAMA_EXE.is_file():
        return OLLAMA_EXE
    found = shutil.which("ollama")
    return Path(found) if found else None


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


def _display_stamp(value: str) -> str:
    try:
        when = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return "trước đó"
    now = datetime.now()
    if when.date() == now.date():
        return when.strftime("%H:%M")
    if when.year == now.year:
        return when.strftime("%d/%m %H:%M")
    return when.strftime("%d/%m/%Y %H:%M")


HTML_BLOCK_RE = re.compile(r"```([a-zA-Z]*)[ \t]*\n(.*?)```", re.DOTALL)


def _extract_html(text: str) -> str | None:
    for language, code in HTML_BLOCK_RE.findall(text or ""):
        code = code.strip()
        lowered = code.lower()
        if language.lower() == "html" or any(
            tag in lowered for tag in ("<html", "<!doctype", "<body")
        ):
            return code
    lowered = (text or "").lower()
    if "<html" not in lowered:
        return None
    start = lowered.find("<!doctype")
    if start == -1:
        start = lowered.find("<html")
    end = lowered.rfind("</html>")
    return text[start:end + 7 if end != -1 else len(text)].strip()


def _init_streams() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _enable_dpi() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class AgentApp:
    def __init__(self, root: tk.Tk, cfg: Config) -> None:
        if cfg.provider.api_key_env and not cfg.provider.api_key:
            raise RuntimeError(
                f"Thiếu API key cho provider '{cfg.provider.name}': "
                f"đặt biến môi trường {cfg.provider.api_key_env} (xem .env.example)."
            )

        self.root = root
        self.cfg = cfg
        self.queue: queue.Queue = queue.Queue()
        self.busy = False
        self._started_at = 0.0
        self._pending_image: str | None = None
        self._last_html: str | None = None
        self._ws_items: list[dict] = []
        self.ollama_btn: ttk.Button | None = None
        self._ollama_starting = False
        self._ollama_proc: subprocess.Popen | None = None

        self.workspaces = WorkspaceManager(cfg.workspaces_file, cfg.workspace)
        default_workspace = cfg.workspace
        try:
            self.workspace = self.workspaces.ensure_active()
        except OSError:
            self.workspace = cfg.workspace
        self.cfg.workspace = self.workspace
        os.environ["AGENT_WORKSPACE"] = str(self.workspace)

        self.skills = SkillManager(cfg.skills_dir, cfg.examples_in_context)
        self.llm = LLM(
            base_url=cfg.provider.base_url,
            model=cfg.provider.model,
            api_key=cfg.provider.api_key,
            temperature=cfg.temperature,
            timeout=cfg.timeout,
        )
        self.vision = Vision(
            LLM(
                base_url=cfg.provider.base_url,
                model=cfg.provider.vision_model or cfg.provider.model,
                api_key=cfg.provider.api_key,
                temperature=0.0,
                timeout=cfg.timeout,
            )
        )
        self.registry = self._make_registry()
        self.memory = Memory(cfg.memory_db)
        self.history = ChatHistory(cfg.chat_history, cfg.history_turns, default_workspace)
        self.agent = Agent(
            llm=self.llm,
            registry=self.registry,
            system_prompt=build_system_prompt(self.workspace, self.skills.catalog(), cfg.shell),
            max_steps=cfg.max_steps,
            verbose=False,
            on_event=lambda event: self.queue.put(("event", event)),
            vision=self.vision,
        )

        self._build_ui()
        self._refresh_skills()
        self._refresh_workspaces()
        self._refresh_last_turn()
        self._restore_history()
        self._log("info", f"Sẵn sàng · {cfg.provider.name}/{cfg.provider.model}")
        self._log("info", f"Workspace: {self.workspace}")
        if cfg.provider.name == "ollama":
            threading.Thread(target=self._ollama_startup_check, daemon=True).start()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_id = self.root.after(100, self._poll)

    def _make_registry(self) -> ToolRegistry:
        registry = ToolRegistry()
        for spec in build_builtin_tools(self.workspace, self.cfg.shell):
            registry.register(spec)
        for spec in build_web_tools(self.workspace):
            registry.register(spec)
        self.skills.reset_tool_state()
        self.skills.register_load_tool(registry)
        registry.register(build_image_tool(self.workspace, self.vision))
        return registry

    # ---------- UI ----------

    def _build_ui(self) -> None:
        root = self.root
        root.title("Agent · skill + học từ feedback")
        root.configure(bg=BG)
        root.minsize(920, 600)
        width, height = 1120, 760
        x = max(0, (root.winfo_screenwidth() - width) // 2)
        y = max(0, (root.winfo_screenheight() - height) // 2 - 20)
        root.geometry(f"{width}x{height}+{x}+{y}")

        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", font=(FONT, 10), background=BG, foreground=FG)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("Section.TLabel", background=CARD, foreground=FG, font=(FONT, 10, "bold"))
        style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED)
        style.configure("Title.TLabel", background=BG, foreground=FG, font=(FONT, 15, "bold"))
        style.configure("TButton", padding=(10, 6))
        style.configure(
            "Accent.TButton", background=ACCENT, foreground="#ffffff", padding=(14, 6),
            lightcolor=ACCENT, darkcolor=ACCENT, bordercolor=ACCENT,
        )
        style.map(
            "Accent.TButton",
            background=[("active", ACCENT_DARK), ("disabled", "#9db9f3")],
            lightcolor=[("active", ACCENT_DARK), ("disabled", "#9db9f3")],
            darkcolor=[("active", ACCENT_DARK), ("disabled", "#9db9f3")],
            foreground=[("disabled", "#eef2ff")],
        )
        style.configure("TCombobox", padding=(6, 3))
        style.configure("TSpinbox", padding=(4, 3))

        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        header = ttk.Frame(root, padding=(16, 12, 16, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Agent", style="Title.TLabel").grid(row=0, column=0, sticky="w")

        workspace_row = ttk.Frame(header)
        workspace_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(3, 0))
        ttk.Label(workspace_row, text="Workspace:", style="Muted.TLabel").pack(side="left")
        self.workspace_box = ttk.Combobox(workspace_row, state="readonly", width=24)
        self.workspace_box.pack(side="left", padx=(6, 0))
        self.workspace_box.bind("<<ComboboxSelected>>", self._on_workspace_selected)
        ttk.Button(workspace_row, text="Tạo…", command=self._create_workspace).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(workspace_row, text="Quản lý", command=self._open_workspaces).pack(
            side="left", padx=(6, 0)
        )
        if self.cfg.provider.name == "ollama":
            self.ollama_btn = ttk.Button(
                workspace_row, text="Bật Ollama", command=self._start_ollama
            )
            self.ollama_btn.pack(side="left", padx=(6, 0))
        ttk.Label(
            workspace_row,
            text=f"{self.cfg.provider.name}/{self.cfg.provider.model} · shell {self.cfg.shell}",
            style="Muted.TLabel",
        ).pack(side="right")

        actions = ttk.Frame(header)
        actions.grid(row=0, column=1, sticky="e")
        self.new_btn = ttk.Button(actions, text="Cuộc mới", command=self._new_chat)
        self.new_btn.pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="Lịch sử", command=self._open_history).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="Thống kê", command=self._show_stats).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="Nén (distill)", command=self._open_distill).pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="Xuất dữ liệu", command=self._open_export).pack(side="left", padx=(6, 0))

        body = ttk.Frame(root, padding=(16, 0, 16, 0))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, minsize=SIDE_WIDTH)
        body.rowconfigure(0, weight=1)

        chat = ttk.Frame(body)
        chat.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        chat.columnconfigure(0, weight=1)
        chat.rowconfigure(0, weight=1)

        transcript_wrap = tk.Frame(chat, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        transcript_wrap.grid(row=0, column=0, sticky="nsew")
        transcript_wrap.columnconfigure(0, weight=1)
        transcript_wrap.rowconfigure(0, weight=1)
        self.transcript = tk.Text(
            transcript_wrap, wrap="word", state="disabled", bd=0, relief="flat",
            bg=CARD, fg=FG, font=(FONT, 10), padx=16, pady=12,
            spacing1=1, spacing3=4, cursor="arrow", highlightthickness=0,
        )
        self.transcript.grid(row=0, column=0, sticky="nsew")
        transcript_scroll = ttk.Scrollbar(transcript_wrap, orient="vertical", command=self.transcript.yview)
        transcript_scroll.grid(row=0, column=1, sticky="ns")
        self.transcript.configure(yscrollcommand=transcript_scroll.set)
        self.transcript.tag_configure("hint", foreground=MUTED, font=(FONT, 9, "italic"))
        self.transcript.tag_configure("user_head", foreground=ACCENT, font=(FONT, 9, "bold"), spacing1=10)
        self.transcript.tag_configure("user", foreground=FG)
        self.transcript.tag_configure("assistant_head", foreground=MUTED, font=(FONT, 9, "bold"), spacing1=10)
        self.transcript.tag_configure("assistant", foreground=FG)
        self.transcript.tag_configure("error_head", foreground=DANGER, font=(FONT, 9, "bold"), spacing1=10)
        self.transcript.tag_configure("error", foreground=DANGER)

        input_card = tk.Frame(chat, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        input_card.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        input_card.columnconfigure(0, weight=1)
        self.input_box = tk.Text(
            input_card, height=3, wrap="word", bd=0, relief="flat", bg=CARD, fg=FG,
            font=(FONT, 10), padx=12, pady=10, highlightthickness=0, insertbackground=FG,
        )
        self.input_box.grid(row=0, column=0, sticky="ew")
        input_side = ttk.Frame(input_card)
        input_side.grid(row=0, column=1, sticky="ns", padx=(0, 10), pady=8)
        self.attach_btn = ttk.Button(input_side, text="Ảnh…", command=self._attach_image)
        self.attach_btn.pack(side="top")
        self.send_btn = ttk.Button(input_side, text="Gửi", style="Accent.TButton", command=self._send)
        self.send_btn.pack(side="top", pady=(6, 0))
        self.html_btn = ttk.Button(
            input_side, text="Xem HTML", command=self._preview_html, state="disabled"
        )
        self.html_btn.pack(side="top", pady=(6, 0))
        ttk.Label(
            input_side,
            text="Enter gửi\nShift+Enter xuống dòng\nCtrl+V dán ảnh",
            style="CardMuted.TLabel",
            font=(FONT, 8),
            justify="left",
        ).pack(side="top", pady=(6, 0))
        self.input_box.bind("<Return>", self._on_return)
        self.input_box.bind("<Shift-Return>", self._on_shift_return)
        self.input_box.bind("<Control-v>", self._on_paste)

        self.attach_var = tk.StringVar(value="")
        self.attach_row = ttk.Frame(input_card)
        self.attach_row.grid(row=1, column=0, columnspan=2, sticky="w", padx=12, pady=(0, 8))
        ttk.Label(self.attach_row, textvariable=self.attach_var, style="CardMuted.TLabel").pack(side="left")
        ttk.Button(self.attach_row, text="Bỏ ảnh", command=self._clear_image).pack(side="left", padx=(8, 0))
        self.attach_row.grid_remove()

        side = tk.Frame(body, bg=CARD, width=SIDE_WIDTH, highlightthickness=1, highlightbackground=BORDER)
        side.grid(row=0, column=1, sticky="ns")
        side.grid_propagate(False)
        side.columnconfigure(0, weight=1)
        side.rowconfigure(9, weight=1)

        ttk.Label(side, text="Skill", style="Section.TLabel").grid(
            row=0, column=0, sticky="w", padx=14, pady=(14, 6)
        )
        skills_wrap = tk.Frame(side, bg=CARD)
        skills_wrap.grid(row=1, column=0, sticky="ew", padx=14)
        skills_wrap.columnconfigure(0, weight=1)
        self.skills_list = tk.Listbox(
            skills_wrap, height=4, bd=0, relief="flat", bg=SOFT, fg=FG,
            font=(FONT, 10), activestyle="none", highlightthickness=1,
            highlightbackground=BORDER, highlightcolor=ACCENT,
            selectbackground="#dbe7ff", selectforeground=FG, exportselection=False,
        )
        self.skills_list.grid(row=0, column=0, sticky="ew")
        self.skills_list.bind("<<ListboxSelect>>", self._on_skill_select)
        self.skills_list.bind("<Double-Button-1>", self._preview_selected_skill)
        self.skill_desc = ttk.Label(
            side, text="", style="CardMuted.TLabel", wraplength=SIDE_WIDTH - 40, justify="left"
        )
        self.skill_desc.grid(row=2, column=0, sticky="w", padx=14, pady=(4, 0))
        ttk.Separator(side, orient="horizontal").grid(row=3, column=0, sticky="ew", padx=14, pady=10)

        ttk.Label(side, text="Lượt gần nhất", style="Section.TLabel").grid(
            row=4, column=0, sticky="w", padx=14
        )
        self.last_turn_var = tk.StringVar(value="Chưa có lượt nào.")
        ttk.Label(
            side, textvariable=self.last_turn_var, style="CardMuted.TLabel",
            wraplength=SIDE_WIDTH - 40, justify="left",
        ).grid(row=5, column=0, sticky="w", padx=14, pady=(2, 6))
        self.learn_btn = ttk.Button(
            side, text="Chấm điểm lượt này", style="Accent.TButton", command=self._open_learn
        )
        self.learn_btn.grid(row=6, column=0, sticky="w", padx=14)
        ttk.Separator(side, orient="horizontal").grid(row=7, column=0, sticky="ew", padx=14, pady=10)

        activity_head = ttk.Label(side, text="Nhật ký hoạt động", style="Section.TLabel")
        activity_head.grid(row=8, column=0, sticky="nw", padx=14, pady=(2, 0))
        activity_wrap = tk.Frame(side, bg=CARD)
        activity_wrap.grid(row=9, column=0, sticky="nsew", padx=14, pady=(6, 14))
        activity_wrap.columnconfigure(0, weight=1)
        activity_wrap.rowconfigure(0, weight=1)
        self.activity = tk.Text(
            activity_wrap, height=6, wrap="word", state="disabled", bd=0, relief="flat",
            bg=SOFT, fg=FG, font=(FONT, 9), padx=8, pady=8,
            highlightthickness=1, highlightbackground=BORDER, cursor="arrow",
        )
        self.activity.grid(row=0, column=0, sticky="nsew")
        activity_scroll = ttk.Scrollbar(activity_wrap, orient="vertical", command=self.activity.yview)
        activity_scroll.grid(row=0, column=1, sticky="ns")
        self.activity.configure(yscrollcommand=activity_scroll.set)
        self.activity.tag_configure("dim", foreground=MUTED)
        self.activity.tag_configure("info", foreground=FG)
        self.activity.tag_configure("call", foreground=ACCENT)
        self.activity.tag_configure("result", foreground="#0f766e")
        self.activity.tag_configure("error", foreground=DANGER)

        status = ttk.Frame(root, padding=(16, 6, 16, 8))
        status.grid(row=2, column=0, sticky="ew")
        status.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="Sẵn sàng.")
        ttk.Label(status, textvariable=self.status_var, style="Muted.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.workspace_status = tk.StringVar(value=str(self.workspace))
        ttk.Label(status, textvariable=self.workspace_status, style="Muted.TLabel").grid(
            row=0, column=1, sticky="e"
        )

    def _restore_history(self) -> None:
        turns = self.history.load(str(self.workspace))
        if not turns:
            self._append_hint()
            return
        self._render_conversation(
            turns,
            f"Đã khôi phục {len(turns)} lượt của workspace {self.workspace.name} "
            f"từ lần trước ({self.cfg.chat_history.name}).\n",
        )
        self._log("info", f"Khôi phục {len(turns)} lượt của {self.workspace}")

    def _render_conversation(self, turns: list[dict], note: str) -> None:
        self.agent.reset()
        self._set_html(_extract_html(turns[-1].get("answer", "")) if turns else None)
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.configure(state="disabled")
        self._append_note(note)
        for turn in turns:
            stamp = _display_stamp(turn.get("created_at", ""))
            self._append_chat("user", turn["question"], stamp)
            self._append_chat("assistant", turn["answer"], stamp)
            self.agent.messages.append({"role": "user", "content": turn["question"]})
            self.agent.messages.append({"role": "assistant", "content": turn["answer"]})

    def _append_hint(self) -> None:
        self._append_note(
            "Sẵn sàng. Hỏi bất cứ điều gì — agent tự gọi tool/skill khi cần. "
            "Dùng nút Ảnh… hoặc Ctrl+V để đính kèm hình ảnh; có thể yêu cầu "
            "dựng HTML theo ảnh giao diện.\n"
        )

    def _append_note(self, text: str) -> None:
        self.transcript.configure(state="normal")
        self.transcript.insert("end", text, "hint")
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    # ---------- transcript & log ----------

    def _append_chat(self, kind: str, text: str, stamp: str | None = None) -> None:
        stamp = stamp or datetime.now().strftime("%H:%M")
        heads = {
            "user": (f"\nBạn · {stamp}\n", "user_head", "user"),
            "assistant": (f"\nAgent · {stamp}\n", "assistant_head", "assistant"),
            "error": (f"\nLỗi · {stamp}\n", "error_head", "error"),
        }
        head, head_tag, body_tag = heads[kind]
        self.transcript.configure(state="normal")
        self.transcript.insert("end", head, head_tag)
        self.transcript.insert("end", text.strip() + "\n", body_tag)
        self.transcript.configure(state="disabled")
        self.transcript.see("end")

    def _log(self, kind: str, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.activity.configure(state="normal")
        self.activity.insert("end", f"{stamp} ", "dim")
        self.activity.insert("end", text + "\n", kind)
        lines = int(self.activity.index("end-1c").split(".")[0])
        if lines > 300:
            self.activity.delete("1.0", "100.0")
        self.activity.configure(state="disabled")
        self.activity.see("end")

    # ---------- input ----------

    def _on_return(self, event: tk.Event) -> str:
        self._send()
        return "break"

    def _on_shift_return(self, event: tk.Event) -> str:
        self.input_box.insert("insert", "\n")
        return "break"

    def _send(self) -> None:
        if self.busy:
            return
        text = self.input_box.get("1.0", "end").strip()
        image = self._pending_image
        if not text and not image:
            return
        if not text:
            text = "Hãy đọc và mô tả hình ảnh này."
        display = f"[ảnh: {Path(image).name}] {text}" if image else text
        self.input_box.delete("1.0", "end")
        self._clear_image()
        self._append_chat("user", display)
        self._log("info", f"Bạn: {_clip(display, 120)}")
        self._started_at = time.monotonic()
        self._set_busy(True, "Đang đọc ảnh…" if image else "Đang gọi model…")
        threading.Thread(
            target=self._worker_ask,
            args=(text, [image] if image else None, display),
            daemon=True,
        ).start()

    def _attach_image(self) -> None:
        if self.busy:
            return
        chosen = filedialog.askopenfilename(
            parent=self.root,
            title="Chọn ảnh để agent đọc",
            filetypes=[
                ("Ảnh", "*.png *.jpg *.jpeg *.webp *.gif *.bmp"),
                ("Tất cả", "*.*"),
            ],
        )
        if not chosen:
            return
        self._pending_image = chosen
        self.attach_var.set(f"Ảnh đính kèm: {Path(chosen).name}")
        self.attach_row.grid()
        self._log("info", f"Đính kèm ảnh: {chosen}")

    def _clear_image(self) -> None:
        self._pending_image = None
        self.attach_var.set("")
        self.attach_row.grid_remove()

    def _on_paste(self, event: tk.Event | None = None) -> str | None:
        if self.busy:
            return None
        dest = Path(tempfile.gettempdir()) / "agent-paste.png"
        try:
            path = grab_clipboard_image(dest)
        except OSError as exc:
            self._log("error", f"Không lưu được ảnh dán từ clipboard: {exc}")
            return None
        if path is None:
            return None
        self._pending_image = str(path)
        self.attach_var.set(f"Ảnh dán từ clipboard: {path.name}")
        self.attach_row.grid()
        self._log("info", f"Dán ảnh từ clipboard → {path}")
        self.status_var.set("Đã dán ảnh từ clipboard. Nhấn Gửi để agent đọc.")
        return "break"

    def _set_html(self, code: str | None) -> None:
        self._last_html = code
        self.html_btn.configure(state="normal" if code else "disabled")

    def _preview_html(self) -> None:
        if not self._last_html:
            return
        target = self.workspace / "preview.html"
        try:
            target.write_text(self._last_html, encoding="utf-8", newline="\n")
        except OSError as exc:
            self._log("error", f"Không lưu được preview.html: {exc}")
            self.status_var.set(f"Không lưu được preview.html: {exc}")
            return
        webbrowser.open(target.as_uri())
        self._log("info", f"Đã lưu HTML → {target}")
        self.status_var.set(f"Đã mở {target.name} trong trình duyệt.")

    def _worker_ask(self, text: str, images: list[str] | None = None, display: str | None = None) -> None:
        try:
            answer = self.agent.run(text, images=images)
            skills = list(self.agent.last_used_skills)
        except Exception as exc:
            self.queue.put(("error", f"{type(exc).__name__}: {exc}"))
            return
        self.queue.put(("answer", {"question": display or text, "answer": answer, "skills": skills}))

    # ---------- ollama ----------

    def _ollama_startup_check(self) -> None:
        if _server_up(_server_url(self.cfg.provider.base_url)):
            self.queue.put(("ollama_state", "Ollama đã chạy sẵn."))
        else:
            self.queue.put(("log", "Ollama chưa chạy — bấm 'Bật Ollama' trên thanh workspace."))

    def _start_ollama(self) -> None:
        if self._ollama_starting:
            return
        exe = _find_ollama_exe()
        if exe is None:
            message = (
                f"Không tìm thấy ollama.exe. Đặt vào {OLLAMA_EXE} "
                "hoặc cài Ollama vào PATH."
            )
            self._log("error", message)
            self.status_var.set(message)
            return
        self._ollama_starting = True
        if self.ollama_btn is not None:
            self.ollama_btn.configure(text="Đang bật…", state="disabled")
        self._log("info", f"Kiểm tra Ollama server: {exe}")
        self.status_var.set("Đang kiểm tra Ollama…")
        threading.Thread(target=self._ollama_worker, args=(exe,), daemon=True).start()

    def _ollama_worker(self, exe: Path) -> None:
        url = _server_url(self.cfg.provider.base_url)
        if _server_up(url):
            self.queue.put(("ollama_done", {"ok": True, "message": "Ollama đã chạy sẵn."}))
            return

        env = os.environ.copy()
        if OLLAMA_MODELS_DIR.is_dir():
            env["OLLAMA_MODELS"] = str(OLLAMA_MODELS_DIR)

        log = None
        try:
            OLLAMA_LOG.parent.mkdir(parents=True, exist_ok=True)
            log = open(OLLAMA_LOG, "a", encoding="utf-8")
        except OSError:
            log = None

        try:
            proc = subprocess.Popen(
                [str(exe), "serve"],
                cwd=str(PROJECT_DIR),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log or subprocess.DEVNULL,
                stderr=subprocess.STDOUT if log else subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            if log:
                log.close()
            self.queue.put(("ollama_done", {"ok": False, "message": f"Không chạy được ollama.exe: {exc}"}))
            return
        self._ollama_proc = proc

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            time.sleep(0.5)
            code = proc.poll()
            if code is not None:
                self.queue.put((
                    "ollama_done",
                    {"ok": False, "message": f"Ollama thoát sớm (mã {code}). Xem log: {OLLAMA_LOG}"},
                ))
                break
            if _server_up(url):
                self.queue.put((
                    "ollama_done",
                    {"ok": True, "message": f"Ollama sẵn sàng (PID {proc.pid}) · log: {OLLAMA_LOG}"},
                ))
                break
        else:
            self.queue.put((
                "ollama_done",
                {"ok": False, "message": f"Ollama chưa phản hồi sau 30s (PID {proc.pid}). Xem log: {OLLAMA_LOG}"},
            ))
        if log:
            log.close()

    def _handle_ollama_done(self, payload: dict) -> None:
        self._ollama_starting = False
        ok = bool(payload.get("ok"))
        message = payload.get("message", "")
        if self.ollama_btn is not None:
            self.ollama_btn.configure(
                text="Ollama ✓" if ok else "Bật Ollama", state="normal"
            )
        self._log("result" if ok else "error", message)
        self.status_var.set("Ollama sẵn sàng." if ok else message)

    # ---------- queue ----------

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "event":
                    self._handle_event(payload)
                elif kind == "answer":
                    self._handle_answer(payload)
                elif kind == "error":
                    self._handle_error(payload)
                elif kind == "log":
                    self._log("info", payload)
                elif kind == "distill_done":
                    self._handle_distill_done(payload)
                elif kind == "ollama_done":
                    self._handle_ollama_done(payload)
                elif kind == "ollama_state":
                    if self.ollama_btn is not None:
                        self.ollama_btn.configure(text="Ollama ✓")
                    self._log("result", payload)
        except queue.Empty:
            pass
        self._poll_id = self.root.after(100, self._poll)

    def _handle_event(self, event: dict) -> None:
        name = event.get("name") or "?"
        if event.get("type") == "tool_start":
            args = json.dumps(event.get("arguments") or {}, ensure_ascii=False)
            self._log("call", f"→ {name} {_clip(args, 160)}")
            self.status_var.set(f"Đang chạy tool: {name}…")
        elif event.get("type") == "tool_result":
            self._log("result", f"← {name}: {_clip(event.get('result') or '', 200)}")
            self.status_var.set("Model đang xử lý…")
        elif event.get("type") == "vision_done":
            self._log("info", f"Đã đọc xong {event.get('count', 1)} ảnh")
            self.status_var.set(
                "Đã đọc ảnh. Model đang dựng HTML…"
                if event.get("ui")
                else "Đã đọc ảnh. Model đang xử lý…"
            )

    def _handle_answer(self, payload: dict) -> None:
        elapsed = time.monotonic() - self._started_at
        self._set_busy(False, f"Xong sau {elapsed:.0f}s.")
        self._append_chat("assistant", payload["answer"])
        html = _extract_html(payload["answer"])
        self._set_html(html)
        if html:
            self.status_var.set(f"Xong sau {elapsed:.0f}s. Bấm Xem HTML để mở bản dựng.")
        turn_id = self.memory.record_turn(payload["question"], payload["answer"], payload["skills"])
        try:
            self.history.append_turn(
                payload["question"], payload["answer"], payload["skills"],
                workspace=str(self.workspace),
            )
        except OSError as exc:
            self._log("error", f"Không lưu được hội thoại: {exc}")
        used = f" · skill: {', '.join(payload['skills'])}" if payload["skills"] else ""
        self._log("info", f"Đã lưu lượt #{turn_id}{used}")
        self._refresh_last_turn()

    def _handle_error(self, message: str) -> None:
        self._set_busy(False, "Lỗi khi gọi model.")
        self._append_chat("error", message)
        self._log("error", message)

    def _set_busy(self, busy: bool, status: str | None = None) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.send_btn.configure(state=state)
        self.attach_btn.configure(state=state)
        self.learn_btn.configure(state=state)
        self.new_btn.configure(state=state)
        if status:
            self.status_var.set(status)

    # ---------- skills panel ----------

    def _refresh_skills(self) -> None:
        self.skills_list.delete(0, "end")
        names = self.skills.names()
        for name in names:
            self.skills_list.insert("end", name)
        if names:
            self.skills_list.selection_set(0)
        self._on_skill_select()

    def _selected_skill(self) -> str | None:
        selection = self.skills_list.curselection()
        return self.skills_list.get(selection[0]) if selection else None

    def _on_skill_select(self, event: tk.Event | None = None) -> None:
        name = self._selected_skill()
        skill = self.skills.skills.get(name) if name else None
        if skill is None:
            self.skill_desc.configure(text="")
            return
        text = _clip(skill.description, 150) if skill.description else "(không có mô tả)"
        text += "\nNhấp đúp để xem SKILL.md"
        self.skill_desc.configure(text=text)

    def _preview_selected_skill(self, event: tk.Event | None = None) -> None:
        name = self._selected_skill()
        if not name or name not in self.skills.skills:
            return
        skill = self.skills.skills[name]
        win = self._dialog(f"Skill · {name}", "700x560")
        header = ttk.Frame(win, padding=(16, 14, 16, 4))
        header.pack(fill="x")
        ttk.Label(header, text=name, font=(FONT, 13, "bold")).pack(anchor="w")
        if skill.when_to_use:
            ttk.Label(
                header, text=f"Khi nào: {skill.when_to_use}", style="Muted.TLabel",
                wraplength=640, justify="left",
            ).pack(anchor="w", pady=(4, 0))
        wrap = tk.Frame(win, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        wrap.pack(fill="both", expand=True, padx=16, pady=(8, 16))
        text = tk.Text(wrap, wrap="word", bd=0, padx=12, pady=10, font=(FONT, 10), bg=CARD)
        text.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=text.yview)
        scroll.pack(side="right", fill="y")
        text.configure(yscrollcommand=scroll.set)
        text.insert("1.0", skill.read_body())
        text.configure(state="disabled")

    # ---------- workspaces ----------

    def _refresh_workspaces(self) -> None:
        self._ws_items = self.workspaces.list()
        self.workspace_box.configure(values=[item["name"] for item in self._ws_items])
        index = next(
            (i for i, item in enumerate(self._ws_items) if Path(item["path"]) == self.workspace),
            -1,
        )
        if index >= 0:
            self.workspace_box.current(index)
        else:
            self.workspace_box.set("")

    def _on_workspace_selected(self, event: tk.Event | None = None) -> None:
        index = self.workspace_box.current()
        if not (0 <= index < len(self._ws_items)):
            return
        item = self._ws_items[index]
        path = Path(item["path"])
        if path == self.workspace:
            return
        if self.busy:
            messagebox.showinfo(
                "Đang bận", "Đợi agent trả lời xong rồi chuyển workspace.", parent=self.root
            )
            self._refresh_workspaces()
            return
        if not path.is_dir():
            messagebox.showerror(
                "Không tìm thấy", f"Thư mục workspace không tồn tại:\n{path}", parent=self.root
            )
            self._refresh_workspaces()
            return
        self.workspaces.set_active(item["id"])
        self._switch_workspace(path)

    def _switch_workspace(self, workspace: Path) -> None:
        workspace = workspace.resolve()
        if workspace == self.workspace:
            return
        self.workspace = workspace
        self.cfg.workspace = workspace
        os.environ["AGENT_WORKSPACE"] = str(workspace)
        self.registry = self._make_registry()
        self.agent.registry = self.registry
        self.agent.update_system_prompt(
            build_system_prompt(workspace, self.skills.catalog(), self.cfg.shell)
        )
        self._clear_image()
        try:
            turns = self.history.load(str(workspace))
        except OSError as exc:
            turns = []
            self._log("error", f"Không đọc được lịch sử của workspace: {exc}")
        if turns:
            stamp = _display_stamp(turns[-1].get("created_at", ""))
            self._render_conversation(
                turns,
                f"Workspace: {workspace}\n"
                f"Đã khôi phục {len(turns)} lượt của workspace này (lượt cuối {stamp}).\n",
            )
        else:
            self._reset_chat_view(f"Workspace: {workspace}\n")
        self.workspace_status.set(str(workspace))
        self._refresh_workspaces()
        self._log("info", f"Chuyển workspace → {workspace}")
        self.status_var.set(f"Workspace: {workspace.name}")

    def _create_workspace(
        self, parent: tk.Misc | None = None, refresh: Callable[[], None] | None = None
    ) -> None:
        if self.busy:
            messagebox.showinfo("Đang bận", "Đợi agent trả lời xong đã.", parent=self.root)
            return
        parent_win = parent or self.root
        base = filedialog.askdirectory(
            parent=parent_win,
            title="Chọn thư mục cha cho workspace mới",
            initialdir=str(self.workspace.parent),
        )
        if not base:
            return
        name = simpledialog.askstring(
            "Workspace mới",
            "Tên workspace (cũng là tên thư mục):",
            parent=parent_win,
        )
        if not name or not name.strip():
            return
        try:
            item = self.workspaces.create(base, name)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Không tạo được workspace", str(exc), parent=parent_win)
            return
        self._switch_workspace(Path(item["path"]))
        self.status_var.set(f"Đã tạo workspace {item['name']} · {item['path']}")
        self._log("info", f"Tạo workspace {item['name']} tại {item['path']}")
        if refresh:
            refresh()

    def _add_existing_workspace(
        self, parent: tk.Misc | None = None, refresh: Callable[[], None] | None = None
    ) -> None:
        if self.busy:
            messagebox.showinfo("Đang bận", "Đợi agent trả lời xong đã.", parent=self.root)
            return
        parent_win = parent or self.root
        folder = filedialog.askdirectory(
            parent=parent_win,
            title="Chọn thư mục làm workspace",
            initialdir=str(self.workspace.parent),
        )
        if not folder:
            return
        try:
            item = self.workspaces.add(folder)
        except (OSError, ValueError) as exc:
            messagebox.showerror("Không thêm được workspace", str(exc), parent=parent_win)
            return
        self._switch_workspace(Path(item["path"]))
        self.status_var.set(f"Đang mở workspace {item['name']}.")
        if refresh:
            refresh()

    def _open_workspaces(self) -> None:
        if self.busy:
            messagebox.showinfo("Đang bận", "Đợi agent trả lời xong đã.", parent=self.root)
            return

        win = self._dialog("Workspaces", "720x440")
        body = ttk.Frame(win, padding=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        wrap = tk.Frame(body, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        wrap.grid(row=0, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        listing = tk.Listbox(
            wrap, bd=0, relief="flat", bg=SOFT, fg=FG, font=(FONT, 10),
            activestyle="none", exportselection=False, highlightthickness=0,
        )
        listing.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=listing.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        listing.configure(yscrollcommand=scroll.set)

        items: list[dict] = []

        def reload(select_id: str | None = None) -> None:
            nonlocal items
            items = self.workspaces.list()
            counts = self.history.counts()
            listing.delete(0, "end")
            for item in items:
                mark = " · đang mở" if item["active"] else ("" if item["exists"] else " · thiếu")
                try:
                    count = counts.get(str(Path(item["path"]).resolve()), 0)
                except OSError:
                    count = 0
                history = f" · {count} hội thoại" if count else ""
                listing.insert("end", f"{item['name']} — {item['path']}{history}{mark}")
            if not items:
                return
            index = 0
            if select_id:
                index = next((i for i, item in enumerate(items) if item["id"] == select_id), 0)
            else:
                index = next((i for i, item in enumerate(items) if item["active"]), 0)
            listing.selection_set(index)
            listing.see(index)

        def open_selected(event: tk.Event | None = None) -> None:
            selection = listing.curselection()
            if not selection:
                return
            item = items[selection[0]]
            if item["active"]:
                win.destroy()
                return
            if not item["exists"]:
                messagebox.showerror(
                    "Không tìm thấy",
                    f"Thư mục workspace không tồn tại:\n{item['path']}",
                    parent=win,
                )
                return
            self.workspaces.set_active(item["id"])
            win.destroy()
            self._switch_workspace(Path(item["path"]))

        def remove_selected() -> None:
            selection = listing.curselection()
            if not selection:
                messagebox.showinfo("Chưa chọn", "Chọn một workspace để xóa khỏi danh sách.", parent=win)
                return
            item = items[selection[0]]
            if item["active"]:
                messagebox.showinfo(
                    "Đang mở",
                    "Workspace này đang mở. Mở workspace khác rồi xóa khỏi danh sách.",
                    parent=win,
                )
                return
            if not messagebox.askyesno(
                "Xóa khỏi danh sách",
                f"Bỏ '{item['name']}' khỏi danh sách?\nThư mục trên đĩa không bị xóa.",
                parent=win,
            ):
                return
            try:
                self.workspaces.remove(item["id"])
            except OSError as exc:
                messagebox.showerror("Lỗi", str(exc), parent=win)
                return
            self._refresh_workspaces()
            reload(item["id"])
            self.status_var.set(f"Đã bỏ workspace {item['name']} khỏi danh sách.")

        reload()
        listing.bind("<Double-Button-1>", open_selected)
        listing.bind("<Return>", open_selected)
        listing.bind("<Delete>", lambda _event: remove_selected())

        buttons = ttk.Frame(body)
        buttons.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(
            buttons, text="Tạo mới…",
            command=lambda: self._create_workspace(win, lambda: reload()),
        ).pack(side="left")
        ttk.Button(
            buttons, text="Thêm thư mục…",
            command=lambda: self._add_existing_workspace(win, lambda: reload()),
        ).pack(side="left", padx=(6, 0))
        ttk.Button(buttons, text="Đóng", command=win.destroy).pack(side="right")
        ttk.Button(buttons, text="Mở", style="Accent.TButton", command=open_selected).pack(
            side="right", padx=(0, 8)
        )
        ttk.Button(buttons, text="Xóa khỏi DS", command=remove_selected).pack(
            side="right", padx=(0, 8)
        )

    # ---------- learning ----------

    def _refresh_last_turn(self) -> None:
        turn = self.memory.last_turn()
        if turn is None:
            self.last_turn_var.set("Chưa có lượt nào.")
            return
        rating = f"{turn['rating']}/5" if turn["rating"] is not None else "chưa chấm"
        skills = turn["skills"] or "không skill"
        self.last_turn_var.set(
            f"#{turn['id']} · {rating} · {skills}\nQ: {_clip(turn['user_input'], 45)}"
        )

    def _open_learn(self) -> None:
        if self.busy:
            return
        turn = self.memory.last_turn()
        if turn is None:
            messagebox.showinfo("Chưa có lượt", "Hãy hỏi agent ít nhất một câu trước.", parent=self.root)
            return
        LearnDialog(self, turn)

    def _show_stats(self) -> None:
        stats = self.memory.stats()
        lines = [
            f"Tổng lượt: {stats['total']}",
            f"Đã đánh giá: {stats['rated']}",
            f"Điểm trung bình: {stats['avg'] or '-'}",
        ]
        if stats["by_skill"]:
            lines.append("")
            lines.append("Theo skill:")
            for skills_used, count in stats["by_skill"]:
                lines.append(f"  · {skills_used}: {count}")
        if stats["rated"] >= 3:
            lines.append("")
            lines.append("Gợi ý: dùng 'Nén (distill)' để rút bài học từ các ví dụ.")
        messagebox.showinfo("Thống kê học", "\n".join(lines), parent=self.root)

    def _open_distill(self) -> None:
        if self.busy:
            messagebox.showinfo("Đang bận", "Đợi agent trả lời xong đã.", parent=self.root)
            return
        names = self.skills.names()
        if not names:
            messagebox.showinfo("Không có skill", "Chưa có skill nào trong thư mục skills.", parent=self.root)
            return
        win = self._dialog("Nén skill (distill)", "520x260")
        win.resizable(False, False)
        body = ttk.Frame(win, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text="Rút các ví dụ đã học thành tối đa 6 quy tắc và ghi vào SKILL.md.\n"
            "Skill cần có examples.md (tạo bằng cách chấm điểm rating >= 4).",
            justify="left",
        ).pack(anchor="w")
        ttk.Label(body, text="Skill:", font=(FONT, 10, "bold")).pack(anchor="w", pady=(14, 4))
        combo = ttk.Combobox(body, state="readonly", values=["(tất cả)"] + names)
        combo.current(0)
        combo.pack(fill="x")

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(16, 0))

        def run() -> None:
            choice = combo.get()
            targets = (
                list(self.skills.skills.values())
                if choice == "(tất cả)"
                else [self.skills.skills[choice]]
            )
            win.destroy()
            self._start_distill(targets)

        ttk.Button(buttons, text="Hủy", command=win.destroy).pack(side="right")
        ttk.Button(buttons, text="Chạy", style="Accent.TButton", command=run).pack(side="right", padx=(0, 8))

    def _start_distill(self, targets: list) -> None:
        self._set_busy(True, "Đang nén skill…")
        self._log("info", "Bắt đầu distill: " + ", ".join(skill.name for skill in targets))
        threading.Thread(target=self._worker_distill, args=(targets,), daemon=True).start()

    def _worker_distill(self, targets: list) -> None:
        results: list[tuple[str, bool, str]] = []
        for skill in targets:
            self.queue.put(("log", f"[distill] {skill.name}…"))
            try:
                ok, message = distill_skill(skill, self.llm)
            except Exception as exc:
                ok, message = False, f"Lỗi {type(exc).__name__}: {exc}"
            results.append((skill.name, ok, message))
        self.queue.put(("distill_done", results))

    def _handle_distill_done(self, results: list[tuple[str, bool, str]]) -> None:
        self._set_busy(False)
        updated = sum(1 for _, ok, _ in results if ok)
        for _, ok, message in results:
            self._log("result" if ok else "error", message)
        summary = f"Hoàn tất: {updated}/{len(results)} skill được cập nhật."
        self.status_var.set(summary)
        messagebox.showinfo(
            "Nén skill",
            summary + "\n\n" + "\n".join(message for _, _, message in results),
            parent=self.root,
        )

    # ---------- export ----------

    def _open_export(self) -> None:
        win = self._dialog("Xuất dữ liệu huấn luyện", "560x340")
        win.resizable(False, False)
        body = ttk.Frame(win, padding=16)
        body.pack(fill="both", expand=True)

        fmt = tk.StringVar(value="sft")
        min_rating = tk.IntVar(value=4)
        default_sft = self.workspace / "data" / "sft.jsonl"
        default_dpo = self.workspace / "data" / "dpo.jsonl"
        path_var = tk.StringVar(value=str(default_sft))
        custom = {"touched": False}

        ttk.Label(body, text="Định dạng:", font=(FONT, 10, "bold")).pack(anchor="w")
        row = ttk.Frame(body)
        row.pack(anchor="w", pady=(2, 10))
        ttk.Radiobutton(row, text="SFT (chat messages)", value="sft", variable=fmt).pack(side="left")
        ttk.Radiobutton(row, text="DPO (prompt/chosen/rejected)", value="dpo", variable=fmt).pack(
            side="left", padx=(16, 0)
        )

        rating_row = ttk.Frame(body)
        rating_row.pack(anchor="w", pady=(0, 10))
        ttk.Label(rating_row, text="Rating tối thiểu (chỉ SFT):").pack(side="left")
        ttk.Spinbox(rating_row, from_=1, to=5, width=4, textvariable=min_rating).pack(
            side="left", padx=(8, 0)
        )

        ttk.Label(body, text="File đầu ra:", font=(FONT, 10, "bold")).pack(anchor="w")
        path_row = ttk.Frame(body)
        path_row.pack(fill="x", pady=(4, 12))
        path_row.columnconfigure(0, weight=1)
        entry = ttk.Entry(path_row, textvariable=path_var)
        entry.grid(row=0, column=0, sticky="ew")

        def on_fmt(*_) -> None:
            if not custom["touched"]:
                path_var.set(str(default_dpo if fmt.get() == "dpo" else default_sft))

        def browse() -> None:
            chosen = filedialog.asksaveasfilename(
                parent=win, title="Lưu file JSONL", defaultextension=".jsonl",
                initialfile=Path(path_var.get()).name,
                filetypes=[("JSONL", "*.jsonl"), ("Tất cả", "*.*")],
            )
            if chosen:
                custom["touched"] = True
                path_var.set(chosen)

        ttk.Button(path_row, text="Chọn…", command=browse).grid(row=0, column=1, padx=(8, 0))
        entry.bind("<Key>", lambda _event: custom.update(touched=True))
        fmt.trace_add("write", on_fmt)

        ttk.Label(
            body,
            text="SFT: các lượt rating >= ngưỡng. DPO: các lượt rating <= 2 kèm bản sửa.",
            style="Muted.TLabel",
            wraplength=500,
            justify="left",
        ).pack(anchor="w")

        def run_export() -> None:
            out = Path(path_var.get().strip() or str(default_sft))
            if not out.is_absolute():
                out = self.workspace / out
            try:
                threshold = int(min_rating.get())
            except (tk.TclError, ValueError):
                threshold = 4
            try:
                if fmt.get() == "sft":
                    turns = self.memory.turns_with_min_rating(threshold)
                    system_prompt = build_system_prompt(self.workspace, self.skills.catalog(), self.cfg.shell)
                    count = export_sft(turns, system_prompt, out)
                    hint = f"rating >= {threshold}"
                else:
                    turns = self.memory.turns_for_dpo()
                    count = export_dpo(turns, out)
                    hint = "rating <= 2 kèm bản sửa"
            except OSError as exc:
                messagebox.showerror("Lỗi ghi file", str(exc), parent=win)
                return
            win.destroy()
            meta = f"Export {fmt.get().upper()}: {count} dòng → {out}"
            self._log("info", meta)
            text = f"Đã xuất {count} dòng {fmt.get().upper()} vào:\n{out}"
            if count == 0:
                text += f"\n\nChưa có lượt nào đạt điều kiện ({hint})."
            messagebox.showinfo("Xuất dữ liệu", text, parent=self.root)

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(16, 0))
        ttk.Button(buttons, text="Hủy", command=win.destroy).pack(side="right")
        ttk.Button(buttons, text="Xuất", style="Accent.TButton", command=run_export).pack(
            side="right", padx=(0, 8)
        )

    # ---------- history ----------

    def _open_history(self) -> None:
        if self.busy:
            messagebox.showinfo("Đang bận", "Đợi agent trả lời xong đã.", parent=self.root)
            return
        items = self.history.conversations(str(self.workspace))
        if not items:
            messagebox.showinfo(
                "Chưa có lịch sử",
                f"Chưa có cuộc trò chuyện nào trong workspace '{self.workspace.name}'.\n"
                "Hội thoại được lưu sau mỗi lượt trả lời.",
                parent=self.root,
            )
            return

        win = self._dialog(f"Lịch sử · {self.workspace.name}", "640x420")
        body = ttk.Frame(win, padding=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        wrap = tk.Frame(body, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        wrap.grid(row=0, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        listing = tk.Listbox(
            wrap, bd=0, relief="flat", bg=SOFT, fg=FG, font=(FONT, 10),
            activestyle="none", exportselection=False, highlightthickness=0,
        )
        listing.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=listing.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        listing.configure(yscrollcommand=scroll.set)

        for item in items:
            stamp = _display_stamp(item["updated_at"] or item["created_at"])
            mark = " · đang mở" if item["active"] else ""
            listing.insert(
                "end", f"{stamp} · {item['count']} lượt{mark} — {_clip(item['preview'], 60)}"
            )
        current = next((index for index, item in enumerate(items) if item["active"]), 0)
        listing.selection_set(current)
        listing.see(current)

        def reload(select_id: str | None = None) -> bool:
            nonlocal items
            items = self.history.conversations(str(self.workspace))
            listing.delete(0, "end")
            for item in items:
                stamp = _display_stamp(item["updated_at"] or item["created_at"])
                mark = " · đang mở" if item["active"] else ""
                listing.insert(
                    "end", f"{stamp} · {item['count']} lượt{mark} — {_clip(item['preview'], 60)}"
                )
            if not items:
                return False
            index = 0
            if select_id:
                index = next((i for i, item in enumerate(items) if item["id"] == select_id), 0)
            listing.selection_set(index)
            listing.see(index)
            return True

        def open_selected(event: tk.Event | None = None) -> None:
            selection = listing.curselection()
            if not selection:
                return
            item = items[selection[0]]
            win.destroy()
            if item["active"]:
                return
            self._open_conversation(item["id"])

        def delete_selected() -> None:
            selection = listing.curselection()
            if not selection:
                messagebox.showinfo("Chưa chọn", "Chọn một cuộc trò chuyện để xóa.", parent=win)
                return
            item = items[selection[0]]
            stamp = _display_stamp(item["updated_at"] or item["created_at"])
            if not messagebox.askyesno(
                "Xóa hội thoại",
                f"Xóa hội thoại {stamp} · {item['count']} lượt?\nKhông thể hoàn tác.",
                parent=win,
            ):
                return
            try:
                self.history.delete(item["id"])
            except OSError as exc:
                messagebox.showerror("Lỗi xóa", str(exc), parent=win)
                return
            was_active = item["active"]
            self._log("info", f"Đã xóa hội thoại {stamp} · {item['count']} lượt")
            if was_active:
                self._reset_chat_view(f"Workspace: {self.workspace}\n")
            if not reload():
                win.destroy()
                self.status_var.set(
                    f"Đã xóa hội thoại. Workspace '{self.workspace.name}' không còn lịch sử."
                )
                return
            self.status_var.set(f"Đã xóa hội thoại {stamp}.")

        def delete_all() -> None:
            if not messagebox.askyesno(
                "Xóa tất cả lịch sử",
                f"Xóa toàn bộ {len(items)} cuộc trò chuyện của workspace "
                f"'{self.workspace.name}'?\nKhông thể hoàn tác.",
                parent=win,
            ):
                return
            try:
                self.history.clear_all(str(self.workspace))
            except OSError as exc:
                messagebox.showerror("Lỗi xóa", str(exc), parent=win)
                return
            win.destroy()
            self._reset_chat_view(f"Workspace: {self.workspace}\n")
            self._log(
                "info",
                f"Đã xóa toàn bộ lịch sử của workspace {self.workspace.name} ({len(items)} cuộc).",
            )
            self.status_var.set(f"Đã xóa toàn bộ lịch sử của workspace '{self.workspace.name}'.")

        listing.bind("<Double-Button-1>", open_selected)
        listing.bind("<Return>", open_selected)
        listing.bind("<Delete>", lambda _event: delete_selected())

        buttons = ttk.Frame(body)
        buttons.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(buttons, text="Xóa tất cả", command=delete_all).pack(side="left")
        ttk.Button(buttons, text="Đóng", command=win.destroy).pack(side="right")
        ttk.Button(buttons, text="Mở", style="Accent.TButton", command=open_selected).pack(
            side="right", padx=(0, 8)
        )
        ttk.Button(buttons, text="Xóa", command=delete_selected).pack(side="right", padx=(0, 8))

    def _open_conversation(self, conversation_id: str) -> None:
        turns = self.history.open(conversation_id)
        if not turns:
            messagebox.showinfo(
                "Không có dữ liệu", "Hội thoại này không còn lượt nào.", parent=self.root
            )
            return
        stamp = _display_stamp(turns[-1].get("created_at", ""))
        self._render_conversation(
            turns,
            f"Workspace: {self.workspace}\n"
            f"Đang mở hội thoại đã lưu lúc {stamp} · {len(turns)} lượt.\n",
        )
        self._log("info", f"Mở hội thoại cũ · {len(turns)} lượt")
        self.status_var.set(f"Đang mở hội thoại cũ · {len(turns)} lượt.")

    # ---------- misc ----------

    def _dialog(self, title: str, geometry: str) -> tk.Toplevel:
        win = tk.Toplevel(self.root)
        win.title(title)
        win.geometry(geometry)
        win.configure(bg=BG)
        win.transient(self.root)
        win.grab_set()
        win.bind("<Escape>", lambda _event: win.destroy())
        return win

    def _reset_chat_view(self, note: str = "") -> None:
        self.agent.reset()
        self._set_html(None)
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.configure(state="disabled")
        if note:
            self._append_note(note)
        self._append_hint()

    def _new_chat(self) -> None:
        if self.busy:
            return
        try:
            archived = self.history.start_new(str(self.workspace))
        except OSError as exc:
            archived = False
            self._log("error", f"Không lưu được cuộc trò chuyện trước: {exc}")
        self._reset_chat_view(f"Workspace: {self.workspace}\n")
        if archived:
            self._log("info", "Đã lưu cuộc trò chuyện trước; bắt đầu cuộc mới.")
            self.status_var.set("Đã lưu cuộc trò chuyện trước và bắt đầu cuộc mới.")
        else:
            self._log("info", "Bắt đầu cuộc hội thoại mới.")
            self.status_var.set("Đã bắt đầu cuộc hội thoại mới.")

    def _on_close(self) -> None:
        if self.busy and not messagebox.askyesno(
            "Đang xử lý", "Agent đang trả lời. Vẫn thoát?", parent=self.root
        ):
            return
        try:
            self.root.after_cancel(self._poll_id)
        except (tk.TclError, ValueError):
            pass
        self.root.destroy()


class LearnDialog(tk.Toplevel):
    def __init__(self, app: AgentApp, turn: dict) -> None:
        super().__init__(app.root)
        self.app = app
        self.turn = turn
        self.title(f"Chấm điểm lượt #{turn['id']}")
        self.geometry("660x640")
        self.configure(bg=BG)
        self.transient(app.root)
        self.grab_set()
        self.bind("<Escape>", lambda _event: self.destroy())

        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)
        body.rowconfigure(4, weight=1)

        used = turn["skills"] or "không skill"
        ttk.Label(
            body,
            text=f"Lượt #{turn['id']} · {turn['created_at'].replace('T', ' ')} · {used}",
            font=(FONT, 10, "bold"),
        ).grid(row=0, column=0, sticky="w")

        if turn["rating"] is not None:
            ttk.Label(
                body,
                text=f"Lượt này đã được đánh giá {turn['rating']}/5 lúc "
                f"{(turn['rated_at'] or '').replace('T', ' ')}. Không ghi lặp ví dụ.",
                style="Muted.TLabel",
                wraplength=600,
                justify="left",
            ).grid(row=1, column=0, sticky="w", pady=(6, 0))
            ttk.Button(body, text="Đóng", command=self.destroy).grid(row=9, column=0, sticky="e", pady=(14, 0))
            return

        ttk.Label(body, text="Câu hỏi", font=(FONT, 10, "bold")).grid(
            row=1, column=0, sticky="w", pady=(10, 2)
        )
        question_wrap, question = self._text_area(body, height=3)
        question_wrap.grid(row=2, column=0, sticky="nsew")
        question.insert("1.0", turn["user_input"])
        question.configure(state="disabled", cursor="arrow")

        ttk.Label(body, text="Agent trả lời", font=(FONT, 10, "bold")).grid(
            row=3, column=0, sticky="w", pady=(10, 2)
        )
        answer_wrap, answer = self._text_area(body, height=6)
        answer_wrap.grid(row=4, column=0, sticky="nsew")
        answer.insert("1.0", turn["assistant_output"][:3000])
        answer.configure(state="disabled", cursor="arrow")

        self.rating = tk.IntVar(value=4)
        rating_row = ttk.Frame(body)
        rating_row.grid(row=5, column=0, sticky="w", pady=(12, 4))
        ttk.Label(rating_row, text="Điểm:", font=(FONT, 10, "bold")).pack(side="left")
        for value in range(1, 6):
            ttk.Radiobutton(rating_row, text=str(value), value=value, variable=self.rating).pack(
                side="left", padx=(8, 0)
            )

        ttk.Label(
            body,
            text="Bản trả lời đúng (bắt buộc khi điểm ≤ 2 để ghi ví dụ sửa):",
            font=(FONT, 10, "bold"),
        ).grid(row=6, column=0, sticky="w", pady=(8, 2))
        corrected_wrap, self.corrected = self._text_area(body, height=4)
        corrected_wrap.grid(row=7, column=0, sticky="nsew")

        note_row = ttk.Frame(body)
        note_row.grid(row=8, column=0, sticky="ew", pady=(10, 0))
        note_row.columnconfigure(1, weight=1)
        ttk.Label(note_row, text="Ghi chú:").grid(row=0, column=0, sticky="w")
        self.note = ttk.Entry(note_row)
        self.note.grid(row=0, column=1, sticky="ew", padx=(8, 0))

        buttons = ttk.Frame(body)
        buttons.grid(row=9, column=0, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="Hủy", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Lưu đánh giá", style="Accent.TButton", command=self._save).pack(
            side="right", padx=(0, 8)
        )

    def _text_area(self, parent: tk.Widget, height: int) -> tuple[tk.Frame, tk.Text]:
        wrap = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=BORDER)
        text = tk.Text(
            wrap, height=height, wrap="word", bd=0, padx=10, pady=8,
            font=(FONT, 10), bg=CARD, fg=FG, highlightthickness=0, insertbackground=FG,
        )
        text.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(wrap, orient="vertical", command=text.yview)
        scroll.pack(side="right", fill="y")
        text.configure(yscrollcommand=scroll.set)
        return wrap, text

    def _save(self) -> None:
        rating = self.rating.get()
        corrected = self.corrected.get("1.0", "end").strip() or None
        note = self.note.get().strip() or None
        if rating <= 2 and not corrected:
            if not messagebox.askyesno(
                "Thiếu bản sửa",
                "Điểm ≤ 2 nhưng chưa có bản trả lời đúng — sẽ không ghi examples.md.\nVẫn lưu điểm?",
                parent=self,
            ):
                return
        messages = apply_feedback(
            self.app.memory, self.app.skills, self.turn, rating, corrected, note
        )
        self.destroy()
        for line in messages:
            self.app._log("info", line)
        self.app._refresh_last_turn()
        messagebox.showinfo("Đã lưu", "\n".join(messages), parent=self.app.root)


def run_gui(cfg: Config | None = None) -> int:
    _init_streams()
    _enable_dpi()
    cfg = cfg or Config.load()
    os.environ.setdefault("AGENT_WORKSPACE", str(cfg.workspace))

    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", round(root.winfo_fpixels("1i") / 72.0, 2))
    except tk.TclError:
        pass
    root.withdraw()
    try:
        AgentApp(root, cfg)
    except Exception as exc:
        messagebox.showerror("Không khởi động được", str(exc))
        root.destroy()
        return 1
    root.deiconify()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(run_gui())
