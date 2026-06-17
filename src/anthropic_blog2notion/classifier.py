from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

from .models import Article, Classification


DEFAULT_RULES_FILE = Path("rules/classifier_rules.json")


def resolve_rules_path(path: str | Path | None = None) -> Path:
    rules_path = Path(path) if path else DEFAULT_RULES_FILE
    if rules_path.is_absolute():
        return rules_path
    cwd_candidate = Path.cwd() / rules_path
    if cwd_candidate.exists():
        return cwd_candidate
    repo_candidate = Path(__file__).resolve().parents[2] / rules_path
    return repo_candidate


def load_classifier_rules(path: str | Path | None = None) -> dict:
    rules_path = resolve_rules_path(path)
    with rules_path.open("r", encoding="utf-8") as handle:
        rules = json.load(handle)
    validate_classifier_rules(rules, rules_path)
    return rules


def validate_classifier_rules(rules: dict, rules_path: Path) -> None:
    required_top_level = {"source_pages", "default_tags", "path_tags", "category_tags"}
    missing = sorted(required_top_level - set(rules))
    if missing:
        raise ValueError(f"{rules_path} is missing top-level classifier section(s): {', '.join(missing)}")
    if not isinstance(rules["source_pages"], list) or not all(
        isinstance(item, str) or isinstance(item, dict) and isinstance(item.get("url"), str)
        for item in rules["source_pages"]
    ):
        raise ValueError(f"{rules_path} source_pages must be a list of URLs")
    if not isinstance(rules["default_tags"], list):
        raise ValueError(f"{rules_path} default_tags must be a list")
    if not isinstance(rules["path_tags"], dict):
        raise ValueError(f"{rules_path} path_tags must be an object")
    if not isinstance(rules["category_tags"], dict):
        raise ValueError(f"{rules_path} category_tags must be an object")


def append_unique(target: list[str], values: list[str]) -> None:
    for value in values:
        if value not in target:
            target.append(value)


def tags_for_article(article: Article, rules: dict) -> list[str]:
    path = urlparse(article.source_url).path
    tags: list[str] = []
    for path_prefix, prefix_tags in rules["path_tags"].items():
        if path == path_prefix or path.startswith(path_prefix):
            append_unique(tags, prefix_tags)
            break
    category_key = article.source_category.strip().lower()
    if category_key in rules["category_tags"]:
        append_unique(tags, rules["category_tags"][category_key])
    if not tags:
        append_unique(tags, rules["default_tags"])
    return tags


def classify_article(article: Article, rules: dict | None = None) -> Classification:
    rules = rules or load_classifier_rules()
    tags = tags_for_article(article, rules)
    return Classification(
        "keep",
        tags,
        "Article was discovered from a configured source page; keyword filtering is disabled.",
    )
