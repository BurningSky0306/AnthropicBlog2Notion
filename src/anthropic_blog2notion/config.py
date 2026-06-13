from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_NOTION_VERSION = "2026-03-11"
DEFAULT_BLOG_BASE_URL = "https://www.anthropic.com"


def load_env_file(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    values: dict[str, str] = {}
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    index = 0
    while index < len(lines):
        raw_line = lines[index]
        index += 1
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "<<" in line and "=" not in line:
            key, marker = line.split("<<", 1)
            key = key.strip()
            marker = marker.strip()
            collected: list[str] = []
            while index < len(lines) and lines[index].strip() != marker:
                collected.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            values[key] = "\n".join(collected)
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'").replace("\\n", "\n")
    return values


def merged_env(env_file: Path | None = None) -> dict[str, str]:
    values = load_env_file(env_file)
    values.update({key: value for key, value in os.environ.items() if value is not None})
    return values


def _env_bool(env: dict[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class AppConfig:
    notion_api_key: str
    notion_database_id: str
    notion_version: str
    ai_base_url: str
    ai_api_key: str
    ai_model: str
    translation_prompt: str
    ai_max_input_chars: int
    ai_max_retries: int
    ai_retry_base_delay_seconds: float
    ai_timeout_seconds: int
    max_posts_per_run: int
    notion_rate_limit_per_second: float
    notion_max_retries: int
    request_delay_seconds: float
    anthropic_base_url: str
    classifier_rules_file: str
    failed_article_retry_passes: int
    ai_json_mode: bool

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "AppConfig":
        return cls(
            notion_api_key=env.get("NOTION_API_KEY", ""),
            notion_database_id=env.get("NOTION_DATABASE_ID", ""),
            notion_version=env.get("NOTION_VERSION", DEFAULT_NOTION_VERSION),
            ai_base_url=env.get("AI_BASE_URL", "").rstrip("/"),
            ai_api_key=env.get("AI_API_KEY", ""),
            ai_model=env.get("AI_MODEL", ""),
            translation_prompt=load_translation_prompt(env),
            ai_max_input_chars=int(env.get("AI_MAX_INPUT_CHARS", "120000")),
            ai_max_retries=int(env.get("AI_MAX_RETRIES", "3")),
            ai_retry_base_delay_seconds=float(env.get("AI_RETRY_BASE_DELAY_SECONDS", "2")),
            ai_timeout_seconds=int(env.get("AI_TIMEOUT_SECONDS", "180")),
            max_posts_per_run=int(env.get("MAX_POSTS_PER_RUN", "20")),
            notion_rate_limit_per_second=float(env.get("NOTION_RATE_LIMIT_PER_SECOND", "3")),
            notion_max_retries=int(env.get("NOTION_MAX_RETRIES", "5")),
            request_delay_seconds=float(env.get("REQUEST_DELAY_SECONDS", "0.4")),
            # NOTE: use BLOG_BASE_URL, not ANTHROPIC_BASE_URL — the latter is the
            # Anthropic SDK's standard variable and is often set to api.anthropic.com,
            # which would hijack the crawler away from the public website.
            anthropic_base_url=env.get("BLOG_BASE_URL", DEFAULT_BLOG_BASE_URL).rstrip("/"),
            classifier_rules_file=env.get("CLASSIFIER_RULES_FILE", "rules/classifier_rules.json"),
            failed_article_retry_passes=int(env.get("FAILED_ARTICLE_RETRY_PASSES", "1")),
            ai_json_mode=_env_bool(env, "AI_JSON_MODE", True),
        )

    def missing_for_write(self) -> list[str]:
        required = {
            "NOTION_API_KEY": self.notion_api_key,
            "NOTION_DATABASE_ID": self.notion_database_id,
            "AI_BASE_URL": self.ai_base_url,
            "AI_API_KEY": self.ai_api_key,
            "AI_MODEL": self.ai_model,
            "TRANSLATION_PROMPT": self.translation_prompt,
        }
        return [key for key, value in required.items() if not value]


def load_translation_prompt(env: dict[str, str]) -> str:
    inline_prompt = env.get("TRANSLATION_PROMPT", "")
    if inline_prompt:
        return inline_prompt
    prompt_file = env.get("TRANSLATION_PROMPT_FILE", "")
    if prompt_file:
        path = Path(prompt_file)
        if path.exists():
            return path.read_text(encoding="utf-8")
    return ""
