from __future__ import annotations

import re
import time
from urllib.parse import urlparse

import requests

from .models import Article, TranslatedArticle


NOTION_API_BASE = "https://api.notion.com/v1"
EXPECTED_SCHEMA = {
    "Title": "title",
    "Source URL": "url",
    "Tags": "multi_select",
    "Selection Reason": "rich_text",
    "Publish Date": "date",
}


class NotionError(RuntimeError):
    pass


class NotionClient:
    def __init__(
        self,
        api_key: str,
        database_id: str,
        version: str,
        rate_limit_per_second: float = 3,
        max_retries: int = 5,
    ):
        self.api_key = api_key
        self.database_id = database_id
        self.version = version
        self.max_retries = max(1, max_retries)
        self.min_request_interval_seconds = 1 / rate_limit_per_second if rate_limit_per_second > 0 else 0
        self.last_request_at = 0.0
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Notion-Version": version,
                "Content-Type": "application/json",
            }
        )
        self.data_source_id = ""
        self.source_url_prop_id = ""

    def request(self, method: str, path: str, **kwargs) -> dict:
        for attempt in range(1, self.max_retries + 1):
            self.wait_for_rate_limit()
            try:
                response = self.session.request(method, NOTION_API_BASE + path, timeout=60, **kwargs)
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt == self.max_retries:
                    raise NotionError(f"Notion request failed after retries: {redact_path(path)}") from exc
                time.sleep(self.retry_delay(attempt))
                continue
            if response.status_code in {429, 529} and attempt < self.max_retries:
                time.sleep(float(response.headers.get("Retry-After", str(self.retry_delay(attempt)))))
                continue
            if response.status_code in {500, 502, 503, 504} and attempt < self.max_retries:
                time.sleep(float(response.headers.get("Retry-After", str(self.retry_delay(attempt)))))
                continue
            if response.status_code >= 400:
                raise NotionError(response_error_summary("Notion API", response, path))
            return response.json()
        raise NotionError(f"Notion request failed after retries: {redact_path(path)}")

    def wait_for_rate_limit(self) -> None:
        if self.min_request_interval_seconds <= 0:
            return
        elapsed = time.monotonic() - self.last_request_at
        if elapsed < self.min_request_interval_seconds:
            time.sleep(self.min_request_interval_seconds - elapsed)
        self.last_request_at = time.monotonic()

    @staticmethod
    def retry_delay(attempt: int) -> float:
        return min(30.0, 2.0**attempt)

    def resolve_data_source(self) -> str:
        database = self.request("GET", f"/databases/{self.database_id}")
        data_sources = database.get("data_sources") or []
        if len(data_sources) != 1:
            raise NotionError(f"Expected exactly one data source under database, found {len(data_sources)}.")
        data_source_id = data_sources[0]["id"]
        self.validate_schema(data_source_id)
        self.data_source_id = data_source_id
        return data_source_id

    def validate_schema(self, data_source_id: str) -> None:
        data_source = self.request("GET", f"/data_sources/{data_source_id}")
        properties = data_source.get("properties", {})
        missing = [name for name in EXPECTED_SCHEMA if name not in properties]
        if missing:
            raise NotionError("Notion database is missing required field(s): " + ", ".join(missing))
        self.source_url_prop_id = properties.get("Source URL", {}).get("id", "")
        wrong = []
        for name, expected_type in EXPECTED_SCHEMA.items():
            actual = properties[name].get("type")
            if actual != expected_type:
                wrong.append(f"{name} expected {expected_type}, got {actual}")
        if wrong:
            raise NotionError("Notion database field type mismatch: " + "; ".join(wrong))

    def existing_source_urls(self) -> set[str]:
        if not self.data_source_id:
            self.resolve_data_source()
        urls: set[str] = set()
        cursor = None
        while True:
            payload: dict = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            # Only fetch the Source URL property to shrink the response payload.
            params = {"filter_properties": [self.source_url_prop_id]} if self.source_url_prop_id else None
            result = self.request(
                "POST", f"/data_sources/{self.data_source_id}/query", params=params, json=payload
            )
            for page in result.get("results", []):
                if page.get("archived") or page.get("in_trash"):
                    continue
                prop = page.get("properties", {}).get("Source URL", {})
                value = prop.get("url")
                if value:
                    urls.add(value)
            if not result.get("has_more"):
                break
            cursor = result.get("next_cursor")
        return urls

    def create_page(self, article: Article, tags: list[str], translated: TranslatedArticle) -> dict:
        if not self.data_source_id:
            self.resolve_data_source()
        properties = {
            "Title": {"title": [{"text": {"content": translated.title[:2000]}}]},
            "Source URL": {"url": article.source_url},
            "Tags": {"multi_select": [{"name": tag} for tag in tags]},
            "Selection Reason": {"rich_text": [{"text": {"content": translated.selection_reason[:2000]}}]},
        }
        if article.publish_date:
            properties["Publish Date"] = {"date": {"start": article.publish_date}}
        payload = {
            "parent": {"type": "data_source_id", "data_source_id": self.data_source_id},
            "properties": properties,
            "markdown": translated.markdown_content,
        }
        cover_url = first_markdown_image_url(translated.markdown_content)
        if cover_url:
            payload["cover"] = {"type": "external", "external": {"url": cover_url}}
        return self.request("POST", "/pages", json=payload)


IMAGE_URL_RE = re.compile(r"!\[[^\]]*]\(\s*(?:<([^>]+)>|([^\s)]+))")


def first_markdown_image_url(markdown: str) -> str:
    for match in IMAGE_URL_RE.finditer(markdown):
        url = (match.group(1) or match.group(2) or "").strip()
        if urlparse(url).scheme in {"http", "https"}:
            return url
    return ""


def response_error_summary(service: str, response: requests.Response, path: str) -> str:
    request_id = response.headers.get("x-request-id") or "unknown"
    return f"{service} {response.status_code} for {redact_path(path)} (request_id={request_id})"


def redact_path(path: str) -> str:
    return re.sub(r"/[0-9A-Za-z_-]{20,}", "/<id>", path)
