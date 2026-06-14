from __future__ import annotations

import json
import re
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
    required_top_level = {"engineering", "research", "news"}
    missing = sorted(required_top_level - set(rules))
    if missing:
        raise ValueError(f"{rules_path} is missing top-level classifier section(s): {', '.join(missing)}")
    if not isinstance(rules["engineering"].get("default_tags"), list):
        raise ValueError(f"{rules_path} engineering.default_tags must be a list")
    if not isinstance(rules["engineering"].get("postmortem_signals"), list):
        raise ValueError(f"{rules_path} engineering.postmortem_signals must be a list")
    if not isinstance(rules["research"].get("retained_categories"), dict):
        raise ValueError(f"{rules_path} research.retained_categories must be an object")
    if not isinstance(rules["news"].get("signal_tags"), dict):
        raise ValueError(f"{rules_path} news.signal_tags must be an object")
    if not isinstance(rules["news"].get("filtered_types"), list):
        raise ValueError(f"{rules_path} news.filtered_types must be a list")
    if "blog" in rules:
        if not isinstance(rules["blog"].get("signal_tags"), dict):
            raise ValueError(f"{rules_path} blog.signal_tags must be an object")
        if not isinstance(rules["blog"].get("filtered_types"), list):
            raise ValueError(f"{rules_path} blog.filtered_types must be a list")


def signal_matches(signal: str, text: str) -> bool:
    """Match a signal at a word boundary so 'science' does not hit 'conscience'.

    Anchors only the start of the word (not the end), so 'chemist' still matches
    'chemistry'/'chemists' while 'election' no longer matches 'selection'.
    """
    signal = signal.strip().lower()
    if not signal:
        return False
    return re.search(r"\b" + re.escape(signal), text) is not None


def classify_article(article: Article, rules: dict | None = None) -> Classification:
    rules = rules or load_classifier_rules()
    path = urlparse(article.source_url).path
    haystack = f"{article.source_url}\n{article.title}\n{article.source_category}\n{article.text_content[:6000]}".lower()
    category = article.source_category.lower()

    if path.startswith("/engineering/"):
        tags = list(rules["engineering"]["default_tags"])
        if any(signal_matches(signal, haystack) for signal in rules["engineering"]["postmortem_signals"]):
            tags.append("postmortem")
        return Classification("keep", tags, "Engineering article is always retained by rule.")

    if path.startswith("/research/"):
        for tag, config in rules["research"]["retained_categories"].items():
            category_signal = config.get("category_signal", "")
            text_signals = config.get("text_signals", [])
            if (category_signal and signal_matches(category_signal, category)) or any(
                signal_matches(signal, haystack) for signal in text_signals
            ):
                return Classification("keep", [tag], f"Research article matches {tag}.")
        return Classification(
            "exclude",
            [],
            "Research article does not match an explicit retained research category.",
        )

    if path.startswith("/news/"):
        news_signal_tags = rules["news"]["signal_tags"]
        matched_signals = [signal for signal in news_signal_tags if signal_matches(signal, haystack)]
        if not matched_signals:
            filtered_type = next(
                (signal for signal in rules["news"]["filtered_types"] if signal_matches(signal, haystack)),
                "",
            )
            reason = f"News article is filtered as {filtered_type}." if filtered_type else "News article has no strong long-term signal."
            return Classification("exclude", [], reason)
        tags = []
        for signal in matched_signals:
            for tag in news_signal_tags[signal]:
                if tag not in tags:
                    tags.append(tag)
        return Classification("keep", tags, "News article matches strong signal(s): " + ", ".join(matched_signals))

    if path.startswith("/blog/"):
        blog_rules = rules.get("blog")
        if not blog_rules:
            return Classification("exclude", [], "Blog posts are not configured for retention.")
        # Match on URL + title only: the product blog's body reuses durable words
        # ("security", "best practices") in marketing copy, but the slug/title
        # reliably signal whether a post is durable.
        blog_haystack = f"{article.source_url}\n{article.title}".lower()
        blog_signal_tags = blog_rules["signal_tags"]
        matched_signals = [signal for signal in blog_signal_tags if signal_matches(signal, blog_haystack)]
        if not matched_signals:
            filtered_type = next(
                (signal for signal in blog_rules.get("filtered_types", []) if signal_matches(signal, blog_haystack)),
                "",
            )
            reason = f"Blog post is filtered as {filtered_type}." if filtered_type else "Blog post has no strong long-term signal."
            return Classification("exclude", [], reason)
        tags = []
        for signal in matched_signals:
            for tag in blog_signal_tags[signal]:
                if tag not in tags:
                    tags.append(tag)
        return Classification("keep", tags, "Blog post matches strong signal(s): " + ", ".join(matched_signals))

    return Classification("exclude", [], "URL is outside supported Anthropic article paths.")
