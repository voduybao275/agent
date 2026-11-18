from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ProviderConfig:
    name: str
    base_url: str
    model: str
    api_key: str
    api_key_env: str


@dataclass
class Config:
    workspace: Path
    shell: str
    max_steps: int
    temperature: float
    timeout: float
    skills_dir: Path
    examples_in_context: int
    memory_db: Path
    provider: ProviderConfig

    @classmethod
    def load(
        cls,
        config_path: str | Path = "config.toml",
        provider: str | None = None,
        model: str | None = None,
    ) -> "Config":
        path = Path(config_path).resolve()
        if not path.is_file():
            raise SystemExit(f"Không tìm thấy config: {path}")
        _load_dotenv(path.parent / ".env")
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        agent_raw = raw.get("agent", {})
        providers = raw.get("providers", {})

        active = provider or os.environ.get("AGENT_PROVIDER") or raw.get("provider", {}).get("active")
        if not active:
            raise SystemExit("Chưa cấu hình provider: đặt [provider] active trong config.toml.")
        if active not in providers:
            raise SystemExit(f"Provider '{active}' không có trong config.toml (có: {', '.join(providers)}).")

        provider_raw = providers[active]
        key_env = provider_raw.get("api_key_env", "")
        api_key = os.environ.get(key_env, "").strip() if key_env else "ollama"
        model_name = model or os.environ.get("AGENT_MODEL") or provider_raw.get("model", "")
        if not model_name:
            raise SystemExit(f"Provider '{active}' chưa có model.")

        shell = agent_raw.get("shell", "auto")
        if shell == "auto":
            shell = "bash" if shutil.which("bash") else "cmd"

        workspace = Path(agent_raw.get("workspace", "."))
        if not workspace.is_absolute():
            workspace = path.parent / workspace

        skills_dir = Path(raw.get("skills", {}).get("dir", "skills"))
        if not skills_dir.is_absolute():
            skills_dir = path.parent / skills_dir

        memory_db = Path(raw.get("memory", {}).get("db", "memory.db"))
        if not memory_db.is_absolute():
            memory_db = path.parent / memory_db

        return cls(
            workspace=workspace.resolve(),
            shell=shell,
            max_steps=int(agent_raw.get("max_steps", 12)),
            temperature=float(agent_raw.get("temperature", 0.2)),
            timeout=float(agent_raw.get("timeout", 120)),
            skills_dir=skills_dir.resolve(),
            examples_in_context=int(raw.get("skills", {}).get("examples_in_context", 3)),
            memory_db=memory_db.resolve(),
            provider=ProviderConfig(
                name=active,
                base_url=provider_raw["base_url"],
                model=model_name,
                api_key=api_key,
                api_key_env=key_env,
            ),
        )


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        os.environ.setdefault(key, value.strip().strip("'\""))
