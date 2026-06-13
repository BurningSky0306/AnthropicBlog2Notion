from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Article:
    title: str
    source_url: str
    source_category: str
    publish_date: str
    markdown_content: str
    text_content: str


@dataclass(frozen=True)
class Classification:
    decision: str
    tags: list[str] = field(default_factory=list)
    selection_basis: str = ""

    @property
    def keep(self) -> bool:
        return self.decision == "keep"


@dataclass(frozen=True)
class TranslatedArticle:
    title: str
    selection_reason: str
    markdown_content: str
