from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urldefrag, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from .markdown_utils import children_to_markdown, compact_block
from .models import Article


@dataclass(frozen=True)
class CrawlConfig:
    base_url: str = "https://www.anthropic.com"
    timeout_seconds: int = 30


ALLOWED_PREFIXES = ("/research/", "/engineering/", "/news/")
MAX_SITEMAP_DEPTH = 5
EXCLUDED_PATH_PATTERNS = (
    re.compile(r"^/research/?$"),
    re.compile(r"^/engineering/?$"),
    re.compile(r"^/news/?$"),
    re.compile(r"^/research/team(?:/|$)"),
)


def normalize_url(url: str) -> str:
    cleaned, _fragment = urldefrag(url)
    parsed = urlparse(cleaned)
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def is_article_url(url: str, base_url: str = "https://www.anthropic.com") -> bool:
    parsed = urlparse(normalize_url(url))
    base = urlparse(base_url)
    if parsed.netloc != base.netloc:
        return False
    path = parsed.path
    if any(pattern.match(path) for pattern in EXCLUDED_PATH_PATTERNS):
        return False
    return any(path.startswith(prefix) for prefix in ALLOWED_PREFIXES)


def fetch_text(url: str, timeout_seconds: int = 30) -> str:
    response = requests.get(url, timeout=timeout_seconds, headers={"User-Agent": "anthropic-blog2notion/0.1"})
    response.raise_for_status()
    return response.text


def discover_article_urls(config: CrawlConfig) -> list[str]:
    sitemap_url = urljoin(config.base_url + "/", "sitemap.xml")
    urls: set[str] = set()
    _collect_sitemap_urls(sitemap_url, config, urls, seen=set(), depth=0)
    return sorted(urls)


def _collect_sitemap_urls(
    sitemap_url: str,
    config: CrawlConfig,
    urls: set[str],
    seen: set[str],
    depth: int,
) -> None:
    if sitemap_url in seen or depth > MAX_SITEMAP_DEPTH:
        return
    seen.add(sitemap_url)
    root = ET.fromstring(fetch_text(sitemap_url, config.timeout_seconds))
    local_tag = root.tag.rsplit("}", 1)[-1]
    if local_tag == "sitemapindex":
        for loc in root.findall(".//{*}loc"):
            if loc.text and loc.text.strip():
                _collect_sitemap_urls(loc.text.strip(), config, urls, seen, depth + 1)
        return
    for loc in root.findall(".//{*}loc"):
        if loc.text and is_article_url(loc.text, config.base_url):
            urls.add(normalize_url(loc.text))


def fetch_article(url: str, config: CrawlConfig) -> Article:
    html = fetch_text(url, config.timeout_seconds)
    soup = BeautifulSoup(html, "html.parser")
    canonical = soup.find("link", rel="canonical")
    source_url = normalize_url(canonical.get("href") if canonical and canonical.get("href") else url)
    article = soup.find("article") or soup.find("main") or soup
    title = soup.find("h1").get_text(" ", strip=True) if soup.find("h1") else ""
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)
    text_lines = [line.strip() for line in article.get_text("\n", strip=True).splitlines() if line.strip()]
    source_category = infer_source_category(source_url, title, text_lines)
    publish_date = infer_publish_date(soup, html, text_lines)
    markdown = compact_block(children_to_markdown(article, config.base_url))
    if title and markdown.startswith(title):
        markdown = markdown[len(title) :].lstrip()
    markdown = append_embedded_video_urls(markdown, html, config.base_url)
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
