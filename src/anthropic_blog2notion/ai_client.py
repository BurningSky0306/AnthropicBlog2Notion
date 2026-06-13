from __future__ import annotations

import json
import re
import time
from dataclasses import asdict
from dataclasses import replace
from urllib.parse import urljoin

import requests

from .markdown_utils import split_markdown
from .models import Article, Classification, TranslatedArticle


class AIError(RuntimeError):
    pass


class AIClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        prompt: str,
        max_input_chars: int = 120000,
        max_retries: int = 3,
        retry_base_delay_seconds: float = 2,
        timeout_seconds: int = 180,
        json_mode: bool = True,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.prompt = prompt
        self.max_input_chars = max_input_chars
        self.max_retries = max(1, max_retries)
        self.retry_base_delay_seconds = retry_base_delay_seconds
        self.timeout_seconds = timeout_seconds
        self.json_mode = json_mode

    def translate(self, article: Article, classification: Classification) -> TranslatedArticle:
        protected_article, images = protect_images(article)
        image_placeholders = [placeholder for placeholder, _image in images]
        payload = {
            "article": asdict(protected_article),
            "classification": asdict(classification),
            "required_output_schema": {
                "Title": "Simplified Chinese title",
                "Selection Reason": "One or two Simplified Chinese sentences explaining why the article is worth reading",
                "Markdown Content": "Simplified Chinese Markdown body",
            },
        }
        raw = json.dumps(payload, ensure_ascii=False)
        if len(raw) <= self.max_input_chars:
            translated = self._translate_payload(payload)
            validate_translation_integrity(protected_article, translated, image_placeholders)
            return restore_translated_images(translated, images)
        translated = self._translate_chunked(protected_article, classification)
        validate_translation_integrity(protected_article, translated, image_placeholders)
        return restore_translated_images(translated, images)

    def _translate_chunked(self, article: Article, classification: Classification) -> TranslatedArticle:
        translated_chunks: list[str] = []
        for index, chunk in enumerate(split_markdown(article.markdown_content, self.max_input_chars), 1):
            chunk_payload = {
                "mode": "translate_markdown_chunk",
                "chunk_index": index,
                "title": article.title,
                "source_url": article.source_url,
                "markdown_content": chunk,
                "required_output_schema": {"Markdown Content": "Simplified Chinese Markdown body chunk"},
            }
            result = self._chat_json(chunk_payload)
            translated = result.get("Markdown Content")
            if not isinstance(translated, str) or not translated.strip():
                raise AIError(f"AI chunk {index} did not return Markdown Content.")
            translated_chunks.append(translated.strip())
        final_payload = {
            "mode": "finalize_chunked_translation_metadata",
            "article": {
                "title": article.title,
                "source_url": article.source_url,
                "source_category": article.source_category,
            },
            "classification": asdict(classification),
            "translated_markdown_content": "\n\n".join(translated_chunks),
            "required_output_schema": {
                "Title": "Simplified Chinese title",
                "Selection Reason": "One or two Simplified Chinese sentences explaining why the article is worth reading",
            },
        }
        result = self._chat_json(final_payload)
        result["Markdown Content"] = "\n\n".join(translated_chunks)
        return parse_translated_article(result)

    def _translate_payload(self, payload: dict) -> TranslatedArticle:
        return parse_translated_article(self._chat_json(payload))

    def _chat_json(self, payload: dict) -> dict:
        url = urljoin(self.base_url + "/", "chat/completions")
        request = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.prompt},
                {
                    "role": "user",
                    "content": "Return only valid JSON matching required_output_schema.\n\n"
                    "Preserve any image placeholders exactly, such as [[ANTHROPIC_IMAGE_0001]].\n\n"
                    + json.dumps(payload, ensure_ascii=False),
                },
            ],
            "temperature": 0.2,
        }
        if self.json_mode:
            request["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        for attempt in range(1, self.max_retries + 1):
            try:
                response = requests.post(url, headers=headers, json=request, timeout=self.timeout_seconds)
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt == self.max_retries:
                    raise AIError("AI request failed after retries.") from exc
                time.sleep(self.retry_delay(attempt))
                continue
            if response.status_code == 429 and attempt < self.max_retries:
                time.sleep(float(response.headers.get("Retry-After", str(self.retry_delay(attempt)))))
                continue
            if response.status_code in {500, 502, 503, 504} and attempt < self.max_retries:
                time.sleep(float(response.headers.get("Retry-After", str(self.retry_delay(attempt)))))
                continue
            if response.status_code >= 400:
                raise AIError(response_error_summary("AI API", response))
            choice = response.json()["choices"][0]
            if choice.get("finish_reason") == "length":
                raise AIError(
                    "AI response was truncated (finish_reason=length); "
                    "lower AI_MAX_INPUT_CHARS to translate in smaller chunks."
                )
            content = choice["message"]["content"]
            try:
                return json.loads(strip_json_fences(content))
            except json.JSONDecodeError as exc:
                if attempt == self.max_retries:
                    raise AIError(f"AI returned invalid JSON: {content[:1000]}") from exc
                time.sleep(self.retry_delay(attempt))
        raise AIError("AI request failed after retries.")

    def retry_delay(self, attempt: int) -> float:
        return min(60.0, self.retry_base_delay_seconds * (2 ** (attempt - 1)))


def response_error_summary(service: str, response: requests.Response) -> str:
    request_id = response.headers.get("x-request-id") or response.headers.get("x-ds-request-id") or "unknown"
    return f"{service} {response.status_code} (request_id={request_id})"


def strip_json_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:]
    return stripped.strip()


def parse_translated_article(data: dict) -> TranslatedArticle:
    required = ("Title", "Selection Reason", "Markdown Content")
    missing = [key for key in required if not isinstance(data.get(key), str) or not data[key].strip()]
    if missing:
        raise AIError("AI response missing required field(s): " + ", ".join(missing))
    return TranslatedArticle(
        title=data["Title"].strip(),
        selection_reason=data["Selection Reason"].strip(),
        markdown_content=data["Markdown Content"].strip(),
    )


def validate_translation_integrity(
    source_article: Article,
    translated: TranslatedArticle,
    required_placeholders: list[str] | None = None,
) -> None:
    source_markdown = source_article.markdown_content.strip()
    translated_markdown = translated.markdown_content.strip()
    if not translated.title.strip():
        raise AIError("AI response returned an empty translated title.")
    if not translated_markdown:
        raise AIError("AI response returned empty Markdown Content.")

    source_len = len(source_markdown)
    translated_len = len(translated_markdown)
    if source_len >= 2000 and translated_len < source_len * 0.2:
        raise AIError(
            f"AI response appears truncated: translated Markdown is {translated_len} chars for {source_len} source chars."
        )

    missing_placeholders = [placeholder for placeholder in required_placeholders or [] if placeholder not in translated_markdown]
    if missing_placeholders:
        raise AIError("AI response dropped image placeholder(s): " + ", ".join(missing_placeholders[:5]))

    source_fences = source_markdown.count("```")
    translated_fences = translated_markdown.count("```")
    if source_fences and source_fences != translated_fences:
        raise AIError(
            f"AI response changed fenced code block structure: source has {source_fences}, translated has {translated_fences}."
        )


IMAGE_RE = re.compile(r"!\[[^\]]*]\([^)]+\)")


def protect_images(article: Article) -> tuple[Article, list[tuple[str, str]]]:
    images: list[tuple[str, str]] = []

    def repl(match: re.Match[str]) -> str:
        placeholder = f"[[ANTHROPIC_IMAGE_{len(images) + 1:04d}]]"
        images.append((placeholder, match.group(0)))
        return placeholder

    protected_markdown = IMAGE_RE.sub(repl, article.markdown_content)
    return replace(article, markdown_content=protected_markdown), images


def restore_translated_images(
    translated: TranslatedArticle, images: list[tuple[str, str]]
) -> TranslatedArticle:
    markdown = translated.markdown_content
    missing: list[str] = []
    for placeholder, image_markdown in images:
        if placeholder in markdown:
            markdown = markdown.replace(placeholder, image_markdown)
        else:
            missing.append(image_markdown)
    if missing:
        markdown = markdown.rstrip() + "\n\n" + "\n\n".join(missing)
    return TranslatedArticle(
        title=translated.title,
        selection_reason=translated.selection_reason,
        markdown_content=markdown,
    )
