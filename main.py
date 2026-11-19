from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _init_streams() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit] + "…"


def main() -> int:
    _init_streams()

    from agent.config import Config
    from agent.llm import LLMError
    from agent.loop import Agent
    from agent.memory import Memory
    from agent.prompt import build_system_prompt
    from agent.skills import SkillManager
    from agent.tools import ToolRegistry, build_builtin_tools, build_image_tool
    from agent.web import build_web_tools
    from learning.distill import distill_skill
    from learning.export import export_dpo, export_sft
    from learning.feedback import apply_feedback

    parser = argparse.ArgumentParser(
        prog="agent",
        description="Agent Python: nạp skill theo nhu cầu, học từ feedback, chạy với API OpenAI-compatible hoặc Ollama.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        default="chat",
        choices=["chat", "ask", "skills", "tools", "learn", "stats", "distill", "export", "gui"],
    )
    parser.add_argument("prompt", nargs="*", help='Câu hỏi cho ask, tên skill cho distill')
    parser.add_argument("--provider", help="Ghi đè provider trong config.toml (vd: openai, ollama)")
    parser.add_argument("--model", help="Ghi đè model")
    parser.add_argument("--config", default="config.toml", help="Đường dẫn config.toml")
    parser.add_argument("-q", "--quiet", action="store_true", help="Không in chi tiết tool")
    parser.add_argument("--format", choices=["sft", "dpo"], default="sft", help="Định dạng dữ liệu export")
    parser.add_argument("--min-rating", type=int, default=4, help="Ngưỡng rating cho SFT (mặc định 4)")
    parser.add_argument("--out", help="File JSONL đầu ra (mặc định data/<format>.jsonl)")
    parser.add_argument(
        "--image",
        action="append",
        help="File ảnh cần đọc, có thể lặp lại (dùng cho lệnh ask)",
    )
    args = parser.parse_args()

    cfg = Config.load(args.config, provider=args.provider, model=args.model)
    os.environ.setdefault("AGENT_WORKSPACE", str(cfg.workspace))

    if args.command == "gui":
        from gui import run_gui

        return run_gui(cfg)

    if args.command == "skills":
        skills = SkillManager(cfg.skills_dir, cfg.examples_in_context)
        if not skills.skills:
            print(f"Không có skill nào trong {cfg.skills_dir}")
            return 0
        for skill in skills.skills.values():
            print(f"- {skill.name}: {skill.description}")
            if skill.when_to_use:
                print(f"  khi nào: {skill.when_to_use}")
            print(f"  thư mục: {skill.path}")
        return 0

    registry = ToolRegistry()
    for spec in build_builtin_tools(cfg.workspace, cfg.shell):
        registry.register(spec)
    for spec in build_web_tools(cfg.workspace):
        registry.register(spec)
    skills = SkillManager(cfg.skills_dir, cfg.examples_in_context)
    skills.register_load_tool(registry)
    vision = _build_vision(cfg)
    registry.register(build_image_tool(cfg.workspace, vision))
    memory = Memory(cfg.memory_db)

    if args.command == "tools":
        for name in registry.names():
            spec = registry.get(name)
            print(f"- {name}: {spec.description}")
        return 0

    if args.command == "stats":
        _print_stats(memory)
        return 0

    if args.command == "export":
        return _run_export(memory, skills, cfg, args, build_system_prompt, export_sft, export_dpo)

    if args.command == "learn":
        return _learn_interactive(memory, skills, apply_feedback)

    llm = _build_llm(cfg)
    if llm is None:
        return 2

    if args.command == "distill":
        only = args.prompt[0] if args.prompt else None
        return _run_distill(skills, llm, only, distill_skill, LLMError)

    agent = Agent(
        llm=llm,
        registry=registry,
        system_prompt=build_system_prompt(cfg.workspace, skills.catalog(), cfg.shell),
        max_steps=cfg.max_steps,
        verbose=not args.quiet,
        vision=vision,
    )

    if args.command == "ask":
        question = " ".join(args.prompt).strip()
        images = args.image or []
        if not question and not images:
            print(
                'Thiếu câu hỏi. Ví dụ: py main.py ask --image photo.png "ảnh này có gì?"',
                file=sys.stderr,
            )
            return 2
        record_question = question or "Hãy đọc và mô tả hình ảnh này."
        if images:
            names = ", ".join(Path(image).name for image in images)
            record_question = f"[ảnh: {names}] {record_question}"
        try:
            answer = agent.run(question or "Hãy đọc và mô tả hình ảnh này.", images=images or None)
        except (LLMError, OSError, ValueError, RuntimeError) as exc:
            print(f"Lỗi: {exc}", file=sys.stderr)
            return 1
        print(answer)
        memory.record_turn(record_question, answer, agent.last_used_skills)
        return 0

    return _chat(
        agent, skills, registry, memory, llm, cfg,
        LLMError, apply_feedback, distill_skill,
    )


def _build_llm(cfg):
    from agent.llm import LLM

    if cfg.provider.api_key_env and not cfg.provider.api_key:
        print(
            f"Thiếu API key cho provider '{cfg.provider.name}': "
            f"đặt biến môi trường {cfg.provider.api_key_env} (xem .env.example).",
            file=sys.stderr,
        )
        return None
    return LLM(
        base_url=cfg.provider.base_url,
        model=cfg.provider.model,
        api_key=cfg.provider.api_key,
        temperature=cfg.temperature,
        timeout=cfg.timeout,
    )


def _build_vision(cfg):
    from agent.llm import LLM
    from agent.vision import Vision

    return Vision(
        LLM(
            base_url=cfg.provider.base_url,
            model=cfg.provider.vision_model or cfg.provider.model,
            api_key=cfg.provider.api_key,
            temperature=0.0,
            timeout=cfg.timeout,
        )
    )


def _print_stats(memory) -> None:
    stats = memory.stats()
    print(f"Tổng lượt: {stats['total']} | đã đánh giá: {stats['rated']} | điểm TB: {stats['avg'] or '-'}")
    if stats["by_skill"]:
        print("Theo skill:")
        for skills_used, count in stats["by_skill"]:
            print(f"  {skills_used}: {count}")
    if stats["rated"] >= 3:
        print("Gợi ý: chạy `py main.py distill` để nén ví dụ thành bài học.")


def _run_export(memory, skills, cfg, args, build_system_prompt, export_sft, export_dpo) -> int:
    out = _resolve_out(args.out, cfg.workspace, cfg.workspace / "data" / f"{args.format}.jsonl")
    if args.format == "sft":
        turns = memory.turns_with_min_rating(args.min_rating)
        system_prompt = build_system_prompt(cfg.workspace, skills.catalog(), cfg.shell)
        count = export_sft(turns, system_prompt, out)
        hint = f"rating >= {args.min_rating}"
    else:
        turns = memory.turns_for_dpo()
        count = export_dpo(turns, out)
        hint = "rating <= 2 kèm bản sửa"

    print(f"Đã xuất {count} dòng {args.format.upper()} vào {out}")
    if count == 0:
        print(f"Chưa có lượt nào đạt điều kiện ({hint}). Chấm điểm bằng `py main.py learn` trước.")
    return 0


def _resolve_out(out: str | None, workspace: Path, default: Path) -> Path:
    if not out:
        return default
    path = Path(out)
    return path if path.is_absolute() else workspace / path


def _learn_interactive(memory, skills, apply_feedback_fn) -> int:
    turn = memory.last_turn()
    if turn is None:
        print("Chưa có lượt hội thoại nào. Hãy chat hoặc dùng ask trước.")
        return 1

    if turn["rating"] is not None:
        print(
            f"Lượt #{turn['id']} đã được đánh giá {turn['rating']}/5 "
            f"lúc {turn['rated_at']}. Chạy lại tác vụ nếu muốn đánh giá lượt mới."
        )
        return 0

    used = [name for name in (turn["skills"] or "").split(",") if name]
    print(f"Lượt #{turn['id']} · {turn['created_at']} · skill: {', '.join(used) or '(không)'}")
    print(f"Q: {_clip(turn['user_input'], 200)}")
    print(f"A: {_clip(turn['assistant_output'], 300)}\n")

    try:
        raw = input("Điểm 1-5 (Enter bỏ qua): ").strip()
        if not raw:
            print("Bỏ qua đánh giá.")
            return 0
        rating = int(raw)
        if rating not in range(1, 6):
            print("Điểm phải từ 1 đến 5.")
            return 2
        corrected = input("Bản trả lời đúng, 1 dòng (Enter nếu không có): ").strip() or None
        note = input("Ghi chú (Enter bỏ qua): ").strip() or None
    except (EOFError, KeyboardInterrupt):
        print()
        return 0
    except ValueError:
        print("Điểm không hợp lệ.")
        return 2

    for line in apply_feedback_fn(memory, skills, turn, rating, corrected, note):
        print(line)
    return 0


def _run_distill(skills, llm, only, distill_skill, llm_error) -> int:
    if only and only not in skills.skills:
        print(f"Không có skill '{only}'. Có: {', '.join(skills.names()) or '(không)'}")
        return 2

    targets = [skills.skills[only]] if only else list(skills.skills.values())
    updated = 0
    for skill in targets:
        print(f"[distill] {skill.name}...")
        try:
            ok, message = distill_skill(skill, llm)
        except llm_error as exc:
            print(f"Lỗi LLM: {exc}")
            return 1
        print(message)
        updated += 1 if ok else 0
    print(f"Hoàn tất: {updated}/{len(targets)} skill được cập nhật.")
    return 0


def _chat(
    agent,
    skills,
    registry,
    memory,
    llm,
    cfg,
    llm_error,
    apply_feedback_fn,
    distill_skill,
) -> int:
    print(f"provider={cfg.provider.name} model={cfg.provider.model}")
    print(f"workspace={cfg.workspace}")
    print(
        "Lệnh: /exit | /reset | /skills | /tools | /skill <tên> | /image <ảnh> [câu hỏi] "
        "| /learn | /stats | /distill [tên]\n"
    )

    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line in {"/exit", "/quit"}:
            return 0
        if line == "/reset":
            agent.reset()
            print("(đã xóa lịch sử hội thoại)")
            continue
        if line == "/skills":
            print(skills.catalog())
            continue
        if line == "/tools":
            print("\n".join(f"- {name}" for name in registry.names()))
            continue
        if line.startswith("/skill "):
            print(skills.load(line.split(maxsplit=1)[1].strip(), registry))
            continue
        if line == "/learn":
            _learn_interactive(memory, skills, apply_feedback_fn)
            continue
        if line == "/stats":
            _print_stats(memory)
            continue
        if line == "/distill" or line.startswith("/distill "):
            only = line.split(maxsplit=1)[1].strip() if " " in line else None
            _run_distill(skills, llm, only, distill_skill, llm_error)
            continue
        if line.startswith("/image "):
            rest = line[len("/image "):].strip().strip('"').strip("'")
            path, _, question = rest.partition(" ")
            path, question = path.strip(), question.strip()
            if not path:
                print("Dùng: /image <đường dẫn ảnh> [câu hỏi]")
                continue
            ask = question or "Hãy đọc và mô tả hình ảnh này."
            try:
                answer = agent.run(ask, images=[path])
            except (llm_error, OSError, ValueError, RuntimeError) as exc:
                print(f"Lỗi đọc ảnh: {exc}")
                continue
            memory.record_turn(f"[ảnh: {Path(path).name}] {ask}", answer, agent.last_used_skills)
            print(f"\nagent> {answer}\n")
            continue

        try:
            answer = agent.run(line)
        except llm_error as exc:
            print(f"Lỗi LLM: {exc}")
            continue
        except KeyboardInterrupt:
            print("\n(đã dừng)")
            continue

        memory.record_turn(line, answer, agent.last_used_skills)
        print(f"\nagent> {answer}\n")


if __name__ == "__main__":
    raise SystemExit(main())
