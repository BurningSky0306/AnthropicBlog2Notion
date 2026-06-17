from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from .markdown_utils import children_to_markdown, compact_block
from .models import Article


DEFAULT_SOURCE_PAGE_PATHS = ("/engineering", "/research", "/economic-futures")
ARTICLE_COMPONENT_CLASS_HINTS = ("ArticleList", "PublicationList", "FeaturedGrid")


@dataclass(frozen=True)
class Source:
    page_url: str


def default_sources(anthropic_base_url: str = "https://www.anthropic.com") -> tuple[Source, ...]:
    base_url = anthropic_base_url.rstrip("/")
    return tuple(Source(base_url + path) for path in DEFAULT_SOURCE_PAGE_PATHS)


@dataclass(frozen=True)
class CrawlConfig:
    base_url: str = "https://www.anthropic.com"
    timeout_seconds: int = 30

    @property
    def sources(self) -> tuple[Source, ...]:
        return default_sources(self.base_url)


def normalize_url(url: str) -> str:
    cleaned, _fragment = urldefrag(url)
    parsed = urlparse(cleaned)
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def page_base_url(url: str) -> str:
    """Scheme+host of a page, used to resolve its relative links/images."""
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def fetch_text(url: str, timeout_seconds: int = 30) -> str:
    response = requests.get(url, timeout=timeout_seconds, headers={"User-Agent": "anthropic-blog2notion/0.1"})
    response.raise_for_status()
    return response.text


def discover_article_urls(config: CrawlConfig, rules: dict | None = None) -> list[str]:
    ordered: list[str] = []
    seen_urls: set[str] = set()
    source_page_urls = configured_source_page_urls(config, rules)
    source_page_paths = {urlparse(normalize_url(url)).path for url in source_page_urls}
    for source in source_page_urls:
        try:
            html = fetch_text(source, config.timeout_seconds)
        except Exception as exc:  # one source page outage must not block the others
            print(f"WARN source page discovery failed for {source}: {exc}", file=sys.stderr)
            continue
        for url in extract_article_urls_from_source_page(html, source, source_page_paths):
            if url not in seen_urls:
                seen_urls.add(url)
                ordered.append(url)
    return ordered


def configured_source_page_urls(config: CrawlConfig, rules: dict | None = None) -> tuple[str, ...]:
    configured = rules.get("source_pages") if rules else None
    if not configured:
        return tuple(source.page_url for source in config.sources)
    urls: list[str] = []
    for item in configured:
        url = item.get("url") if isinstance(item, dict) else item
        if isinstance(url, str) and url.strip():
            urls.append(normalize_url(url.strip()))
    return tuple(urls)


def extract_article_urls_from_source_page(html: str, page_url: str, source_page_paths: set[str]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    def append(url: str) -> None:
        normalized = normalize_url(url)
        if normalized in seen:
            return
        seen.add(normalized)
        urls.append(normalized)

    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.find_all("a", href=True):
        if not anchor_looks_like_article_link(anchor):
            continue
        candidate = urljoin(page_url, anchor["href"])
        if is_same_site_article_candidate(candidate, page_url, source_page_paths):
            append(candidate)

    for candidate in extract_embedded_post_urls(html, page_url):
        if is_same_site_article_candidate(candidate, page_url, source_page_paths):
            append(candidate)
    return urls


def anchor_looks_like_article_link(anchor) -> bool:
    current = anchor
    while current is not None:
        name = getattr(current, "name", None)
        if name in {"header", "footer", "nav"}:
            return False
        classes = " ".join(current.get("class", [])) if hasattr(current, "get") else ""
        if any(hint in classes for hint in ARTICLE_COMPONENT_CLASS_HINTS):
            return True
        current = getattr(current, "parent", None)
    return False


def is_same_site_article_candidate(url: str, page_url: str, source_page_paths: set[str]) -> bool:
    parsed = urlparse(normalize_url(url))
    page = urlparse(page_url)
    if parsed.scheme not in {"http", "https"} or parsed.netloc != page.netloc:
        return False
    if parsed.path in source_page_paths or parsed.path in {"/", "/news", "/policy", "/81k-interviews"}:
        return False
    if parsed.path.startswith(("/_next/", "/images/", "/research/team/", "/features/")):
        return False
    return True


def extract_embedded_post_urls(html: str, page_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    seen: set[str] = set()
    for script in soup.find_all("script"):
        text = script.string or script.get_text()
        if not text.startswith("self.__next_f.push("):
            continue
        for payload in next_data_payloads(text):
            for content in walk_content_objects(payload):
                candidate = content_object_url(content, page_url)
                if candidate and candidate not in seen:
                    seen.add(candidate)
                    urls.append(candidate)
    return urls


def next_data_payloads(script_text: str):
    import json

    try:
        pushed = json.loads(script_text.removeprefix("self.__next_f.push(").removesuffix(")"))
    except ValueError:
        return
    for item in pushed[1:]:
        if not isinstance(item, str) or ":" not in item:
            continue
        _slot, payload = item.split(":", 1)
        if not payload.startswith(("[", "{")):
            continue
        try:
            yield json.loads(payload)
        except ValueError:
            continue


def walk_content_objects(value):
    if isinstance(value, dict):
        if value.get("_type") in {"post", "engineeringArticle"} and isinstance(value.get("slug"), dict):
            yield value
        for child in value.values():
            yield from walk_content_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_content_objects(child)


def content_object_url(content: dict, page_url: str) -> str:
    slug = content.get("slug", {}).get("current", "")
    if not slug:
        return ""
    if content.get("_type") == "engineeringArticle":
        return urljoin(page_url, f"/engineering/{slug}")
    directories = [
        directory.get("value")
        for directory in content.get("directories", [])
        if isinstance(directory, dict) and directory.get("value")
    ]
    if not directories:
        return ""
    return urljoin(page_url, f"/{directories[0]}/{slug}")


def fetch_article(url: str, config: CrawlConfig) -> Article:
    html = fetch_text(url, config.timeout_seconds)
    soup = BeautifulSoup(html, "html.parser")
    canonical = soup.find("link", rel="canonical")
    requested_url = normalize_url(url)
    source_url = requested_url
    if canonical and canonical.get("href"):
        canonical_url = normalize_url(canonical["href"])
        if urlparse(canonical_url).netloc == urlparse(requested_url).netloc:
            source_url = canonical_url
    page_base = page_base_url(source_url)
    article = soup.find("article") or soup.find("main") or soup
    title = soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else ""
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)
    text_lines = [line.strip() for line in article.get_text("\n", strip=True).splitlines() if line.strip()]
    source_category = infer_source_category(source_url, title, text_lines)
    publish_date = infer_publish_date(soup, html, text_lines)
    markdown = compact_block(children_to_markdown(article, page_base))
    if title and markdown.startswith(title):
        markdown = markdown[len(title) :].lstrip()
    markdown = append_embedded_video_urls(markdown, html, page_base)
    return Article(
        title=title or source_url,
        source_url=source_url,
        source_category=source_category,
        publish_date=publish_date,
        markdown_content=markdown,
        text_content="\n".join(text_lines),
    )


def infer_source_category(source_url: str, title: str, text_lines: list[str]) -> str:
    path = urlparse(source_url).path
    if path.startswith("/engineering/"):
        return "Engineering"
    if path.startswith("/blog/"):
        return "Blog"
    if path.startswith("/news/"):
        return "News"
    if not text_lines:
        return "Research" if path.startswith("/research/") else ""
    if text_lines[0] != title:
        return text_lines[0]
    for index, line in enumerate(text_lines[:12]):
        if line == title and index > 0:
            return text_lines[index - 1]
    return "Research" if path.startswith("/research/") else ""


def infer_publish_date(soup: BeautifulSoup, html: str, text_lines: list[str]) -> str:
    selectors = [
        ("meta", {"property": "article:published_time"}),
        ("meta", {"name": "article:published_time"}),
        ("meta", {"property": "og:published_time"}),
        ("meta", {"name": "date"}),
    ]
    for name, attrs in selectors:
        tag = soup.find(name, attrs)
        if tag and tag.get("content"):
            parsed = parse_date(tag["content"])
            if parsed:
                return parsed
    for tag in soup.find_all("time"):
        parsed = parse_date(tag.get("datetime") or tag.get_text(" ", strip=True))
        if parsed:
            return parsed
    date_pattern = re.compile(
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},\s+\d{4}",
        re.IGNORECASE,
    )
    for line in text_lines[:25]:
        parsed = parse_date(line.replace("Published", "", 1).strip())
        if parsed:
            return parsed
    match = date_pattern.search(html)
    return parse_date(match.group(0)) if match else ""


def parse_date(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if "T" in value:
        value = value.split("T", 1)[0]
    for fmt in ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return ""


def append_embedded_video_urls(markdown: str, html: str, base_url: str = "https://www.anthropic.com") -> str:
    urls = extract_embedded_video_urls(html, base_url)
    missing = [url for url in urls if url not in markdown]
    if not missing:
        return markdown
    return markdown.rstrip() + "\n\n" + "\n\n".join(missing) + "\n"


def extract_embedded_video_urls(html: str, base_url: str = "https://www.anthropic.com") -> list[str]:
    found: list[str] = []
    patterns = [
        r'"embedUrl"\s*:\s*"([^"]+)"',
        r'\\"embedUrl\\"\s*:\s*\\"([^"\\]+)\\"',
        r'"(?:videoUrl|playbackUrl|streamingUrl)"\s*:\s*"([^"]+)"',
        r'\\"(?:videoUrl|playbackUrl|streamingUrl)\\"\s*:\s*\\"([^"\\]+)\\"',
    ]
    for pattern in patterns:
        for raw_url in re.findall(pattern, html):
            url = raw_url.replace("\\/", "/")
            if is_embedded_media_url(url) and url not in found:
                found.append(url)
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["iframe", "video", "source"]):
        src = tag.get("src")
        if src:
            url = urljoin(base_url, src)
            if is_embedded_media_url(url) and url not in found:
                found.append(url)
    return found


def is_embedded_media_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    lowered = url.lower()
    media_hosts = ("youtube.com", "youtu.be", "vimeo.com", "wistia", "mux.com", "stream.mux.com")
    media_extensions = (".mp4", ".webm", ".mov", ".m3u8")
    return any(host in lowered for host in media_hosts) or any(
        lowered.split("?", 1)[0].endswith(extension) for extension in media_extensions
    )
