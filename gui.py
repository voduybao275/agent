from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from agent.config import Config
from agent.llm import LLM
from agent.loop import Agent
from agent.memory import Memory
from agent.prompt import build_system_prompt
from agent.skills import SkillManager
from agent.tools import ToolRegistry, build_builtin_tools
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


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


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

        self.registry = ToolRegistry()
        for spec in build_builtin_tools(cfg.workspace, cfg.shell):
            self.registry.register(spec)
        self.skills = SkillManager(cfg.skills_dir, cfg.examples_in_context)
        self.skills.register_load_tool(self.registry)
        self.memory = Memory(cfg.memory_db)
        self.llm = LLM(
            base_url=cfg.provider.base_url,
            model=cfg.provider.model,
            api_key=cfg.provider.api_key,
            temperature=cfg.temperature,
            timeout=cfg.timeout,
        )
        self.agent = Agent(
            llm=self.llm,
            registry=self.registry,
            system_prompt=build_system_prompt(cfg.workspace, self.skills.catalog(), cfg.shell),
            max_steps=cfg.max_steps,
            verbose=False,
            on_event=lambda event: self.queue.put(("event", event)),
        )

        self._build_ui()
        self._refresh_skills()
        self._refresh_last_turn()
        self._log("info", f"Sẵn sàng · {cfg.provider.name}/{cfg.provider.model}")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll)

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
        ttk.Label(
            header,
            text=f"{self.cfg.provider.name} · {self.cfg.provider.model} · shell {self.cfg.shell}",
            style="Muted.TLabel",
        ).grid(row=1, column=0, sticky="w")

        actions = ttk.Frame(header)
        actions.grid(row=0, column=1, rowspan=2, sticky="e")
        self.new_btn = ttk.Button(actions, text="Cuộc mới", command=self._new_chat)
        self.new_btn.pack(side="left", padx=(6, 0))
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
        self.send_btn = ttk.Button(input_side, text="Gửi", style="Accent.TButton", command=self._send)
        self.send_btn.pack(side="top")
        ttk.Label(
            input_side,
            text="Enter gửi\nShift+Enter xuống dòng",
            style="CardMuted.TLabel",
            font=(FONT, 8),
            justify="left",
        ).pack(side="top", pady=(6, 0))
        self.input_box.bind("<Return>", self._on_return)
        self.input_box.bind("<Shift-Return>", self._on_shift_return)

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
        ttk.Label(status, text=str(self.cfg.workspace), style="Muted.TLabel").grid(
            row=0, column=1, sticky="e"
        )

        self._append_hint()

    def _append_hint(self) -> None:
        self.transcript.configure(state="normal")
        self.transcript.insert(
            "end",
            "Sẵn sàng. Hỏi bất cứ điều gì — agent tự gọi tool/skill khi cần.\n",
            "hint",
        )
        self.transcript.configure(state="disabled")

    # ---------- transcript & log ----------

    def _append_chat(self, kind: str, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M")
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
        if not text:
            return
        self.input_box.delete("1.0", "end")
        self._append_chat("user", text)
        self._log("info", f"Bạn: {_clip(text, 120)}")
        self._started_at = time.monotonic()
        self._set_busy(True, "Đang gọi model…")
        threading.Thread(target=self._worker_ask, args=(text,), daemon=True).start()

    def _worker_ask(self, text: str) -> None:
        try:
            answer = self.agent.run(text)
            skills = list(self.agent.last_used_skills)
        except Exception as exc:
            self.queue.put(("error", f"{type(exc).__name__}: {exc}"))
            return
        self.queue.put(("answer", {"question": text, "answer": answer, "skills": skills}))

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
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _handle_event(self, event: dict) -> None:
        name = event.get("name") or "?"
        if event.get("type") == "tool_start":
            args = json.dumps(event.get("arguments") or {}, ensure_ascii=False)
            self._log("call", f"→ {name} {_clip(args, 160)}")
            self.status_var.set(f"Đang chạy tool: {name}…")
        elif event.get("type") == "tool_result":
            self._log("result", f"← {name}: {_clip(event.get('result') or '', 200)}")
            self.status_var.set("Model đang xử lý…")

    def _handle_answer(self, payload: dict) -> None:
        elapsed = time.monotonic() - self._started_at
        self._set_busy(False, f"Xong sau {elapsed:.0f}s.")
        self._append_chat("assistant", payload["answer"])
        turn_id = self.memory.record_turn(payload["question"], payload["answer"], payload["skills"])
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
        default_sft = self.cfg.workspace / "data" / "sft.jsonl"
        default_dpo = self.cfg.workspace / "data" / "dpo.jsonl"
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
                out = self.cfg.workspace / out
            try:
                threshold = int(min_rating.get())
            except (tk.TclError, ValueError):
                threshold = 4
            try:
                if fmt.get() == "sft":
                    turns = self.memory.turns_with_min_rating(threshold)
                    system_prompt = build_system_prompt(self.cfg.workspace, self.skills.catalog(), self.cfg.shell)
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

    def _new_chat(self) -> None:
        if self.busy:
            return
        self.agent.reset()
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", "end")
        self.transcript.configure(state="disabled")
        self._append_hint()
        self._log("info", "Bắt đầu cuộc hội thoại mới.")
        self.status_var.set("Đã xoá lịch sử hội thoại.")

    def _on_close(self) -> None:
        if self.busy and not messagebox.askyesno(
            "Đang xử lý", "Agent đang trả lời. Vẫn thoát?", parent=self.root
        ):
            return
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
