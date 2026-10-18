"""Settings from environment variables and an optional .env file (shell values win)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"

SEED_NOW = datetime(2026, 10, 12, 18, 0, tzinfo=UTC)
"""The seed snapshot. Duty clocks are counted up to here, so it is the default 'now'."""


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def parse_utc(value: str) -> datetime:
    """ISO 8601 with a ``Z`` or offset; naive values are taken as UTC."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class Settings:
    falkordb_host: str = "localhost"
    falkordb_port: int = 6379
    falkordb_username: str | None = None
    falkordb_password: str | None = None
    ops_graph: str = "crewops"
    memory_prefix: str = "crewops_memory_"
    now: datetime = SEED_NOW
    model: str = "claude-opus-5-5"
    anthropic_api_key: str | None = None
    llm_api_key: str | None = None
    """Key for an OpenAI-compatible API (Groq by default). Takes priority over Anthropic."""
    llm_base_url: str = "https://api.groq.com/openai/v1"
    llm_model: str = "qwen/qwen3.8-27b,openai/gpt-oss-120b,openai/gpt-oss-20b"
    """One model, or a comma-separated list to try in order when one hits its rate limit."""

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> Settings:
        file_values = _read_env_file(env_file or ROOT / ".env")

        def get(key: str, default: str | None = None) -> str | None:
            value = os.environ.get(key, file_values.get(key))
            return value if value not in (None, "") else default

        now = get("CREWOPS_NOW")
        return cls(
            falkordb_host=get("FALKORDB_HOST", "localhost") or "localhost",
            falkordb_port=int(get("FALKORDB_PORT", "6379") or 6379),
            falkordb_username=get("FALKORDB_USERNAME"),
            falkordb_password=get("FALKORDB_PASSWORD"),
            ops_graph=get("CREWOPS_OPS_GRAPH", "crewops") or "crewops",
            memory_prefix=get("CREWOPS_MEMORY_PREFIX", "crewops_memory_") or "crewops_memory_",
            now=parse_utc(now) if now else SEED_NOW,
            model=get("CREWOPS_MODEL", "claude-opus-5-5") or "claude-opus-5-5",
            anthropic_api_key=get("ANTHROPIC_API_KEY"),
            llm_api_key=get("LLM_API_KEY"),
            llm_base_url=get("LLM_BASE_URL", "https://api.groq.com/openai/v1") or "https://api.groq.com/openai/v1",
            llm_model=get("LLM_MODEL") or "qwen/qwen3.8-27b,openai/gpt-oss-120b,openai/gpt-oss-20b",
        )


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings.from_env()
