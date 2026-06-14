from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from .markdown_utils import children_to_markdown, compact_block
from .models import Article


MAX_SITEMAP_DEPTH = 5


@dataclass(frozen=True)
class Source:
    base_url: str
    allowed_prefixes: tuple[str, ...]
    excluded_patterns: tuple[re.Pattern[str], ...]


_ANTHROPIC_EXCLUDES = (
    re.compile(r"^/research/?$"),
    re.compile(r"^/engineering/?$"),
    re.compile(r"^/news/?$"),
    re.compile(r"^/research/team(?:/|$)"),
)
# claude.com/blog mixes durable engineering/security writeups with heavy product
# marketing. Admit only English single-slug posts here; the classifier's strict
# blog whitelist decides what to keep. Localized copies (/ja/blog, /de/blog, ...)
# fail the "/blog/" prefix, and category/pagination index pages are excluded.
_CLAUDE_BLOG_EXCLUDES = (
    re.compile(r"^/blog/?$"),
    re.compile(r"^/blog/category(?:/|$)"),
)


def default_sources(anthropic_base_url: str = "https://www.anthropic.com") -> tuple[Source, ...]:
    return (
        Source(
            anthropic_base_url.rstrip("/"),
            ("/research/", "/engineering/", "/news/"),
            _ANTHROPIC_EXCLUDES,
        ),
        Source("https://claude.com", ("/blog/",), _CLAUDE_BLOG_EXCLUDES),
    )


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
    """Scheme+host of a page, used to resolve its relative links/images.

    Derived from the page URL (not a global base) so claude.com assets resolve
    against claude.com and anthropic.com assets against anthropic.com.
    """
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def url_matches_source(url: str, source: Source) -> bool:
    parsed = urlparse(normalize_url(url))
    if parsed.netloc != urlparse(source.base_url).netloc:
        return False
    path = parsed.path
    if any(pattern.match(path) for pattern in source.excluded_patterns):
        return False
    return any(path.startswith(prefix) for prefix in source.allowed_prefixes)


def fetch_text(url: str, timeout_seconds: int = 30) -> str:
    response = requests.get(url, timeout=timeout_seconds, headers={"User-Agent": "anthropic-blog2notion/0.1"})
    response.raise_for_status()
    return response.text


def discover_article_urls(config: CrawlConfig) -> list[str]:
    ordered: list[str] = []
    seen_urls: set[str] = set()
    for source in config.sources:
        found: set[str] = set()
        sitemap_url = urljoin(source.base_url + "/", "sitemap.xml")
        try:
            _collect_sitemap_urls(sitemap_url, source, config.timeout_seconds, found, seen=set(), depth=0)
        except Exception as exc:  # one source's sitemap outage must not block the others
            print(f"WARN sitemap discovery failed for {source.base_url}: {exc}", file=sys.stderr)
        for url in sorted(found):
            if url not in seen_urls:
                seen_urls.add(url)
                ordered.append(url)
    return ordered


def _collect_sitemap_urls(
    sitemap_url: str,
    source: Source,
    timeout_seconds: int,
    urls: set[str],
    seen: set[str],
    depth: int,
) -> None:
    if sitemap_url in seen or depth > MAX_SITEMAP_DEPTH:
        return
    seen.add(sitemap_url)
    root = ET.fromstring(fetch_text(sitemap_url, timeout_seconds))
    local_tag = root.tag.rsplit("}", 1)[-1]
    if local_tag == "sitemapindex":
        for loc in root.findall(".//{*}loc"):
            if loc.text and loc.text.strip():
                _collect_sitemap_urls(loc.text.strip(), source, timeout_seconds, urls, seen, depth + 1)
        return
    for loc in root.findall(".//{*}loc"):
        if loc.text and url_matches_source(loc.text, source):
            urls.add(normalize_url(loc.text))


def fetch_article(url: str, config: CrawlConfig) -> Article:
    html = fetch_text(url, config.timeout_seconds)
    soup = BeautifulSoup(html, "html.parser")
    canonical = soup.find("link", rel="canonical")
    source_url = normalize_url(canonical.get("href") if canonical and canonical.get("href") else url)
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
