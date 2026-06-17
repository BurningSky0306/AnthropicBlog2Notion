from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .ai_client import AIClient
from .classifier import classify_article, load_classifier_rules
from .config import AppConfig, merged_env
from .crawler import CrawlConfig, discover_article_urls, fetch_article, normalize_url
from .notion_client import NotionClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync filtered Anthropic blog articles to Notion.")
    parser.add_argument("command", choices=["run"], nargs="?", default="run")
    parser.add_argument("--dry-run", action="store_true", help="Scan and classify without calling AI or writing Notion.")
    parser.add_argument("--limit", type=int, default=0, help="Override MAX_POSTS_PER_RUN for this run; 0 uses the env default.")
    parser.add_argument("--env-file", type=Path, default=Path(".anthropic-blog2notion.env"))
    parser.add_argument("--skip-notion", action="store_true", help="Do not query Notion for existing Source URL values.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = AppConfig.from_env(merged_env(args.env_file))
    classifier_rules = load_classifier_rules(config.classifier_rules_file)

    if not args.dry_run:
        missing = config.missing_for_write()
        if missing:
            print("Missing required configuration for write mode: " + ", ".join(missing), file=sys.stderr)
            return 2

    crawl_config = CrawlConfig(base_url=config.anthropic_base_url)
    urls = discover_article_urls(crawl_config, classifier_rules)
    print(f"Discovered article URLs in scope: {len(urls)}")
    if not urls:
        print(
            "No article URLs discovered; the configured source pages may have changed or the site is unreachable.",
            file=sys.stderr,
        )
        return 3

    notion: NotionClient | None = None
    existing_urls: set[str] = set()
    if not args.skip_notion and config.notion_api_key and config.notion_database_id:
        notion = NotionClient(
            config.notion_api_key,
            config.notion_database_id,
            config.notion_version,
            config.notion_rate_limit_per_second,
            config.notion_max_retries,
        )
        notion.resolve_data_source()
        existing_urls = existing_urls | {normalize_url(url) for url in notion.existing_source_urls()}
        print(f"Existing Notion Source URLs: {len(existing_urls)}")
    elif not args.dry_run:
        print("Notion configuration is required unless --dry-run is used.", file=sys.stderr)
        return 2

    ai_client: AIClient | None = None
    if not args.dry_run:
        ai_client = AIClient(
            config.ai_base_url,
            config.ai_api_key,
            config.ai_model,
            config.translation_prompt,
            config.ai_max_input_chars,
            config.ai_max_retries,
            config.ai_retry_base_delay_seconds,
            config.ai_timeout_seconds,
            config.ai_json_mode,
        )

    counts = {"skipped_existing": 0, "excluded": 0, "kept": 0, "created": 0, "retry_attempts": 0, "failed": 0}
    failure_messages: dict[str, str] = {}
    failed_urls: list[str] = []
    counted_kept_urls: set[str] = set()
    counted_excluded_urls: set[str] = set()
    excluded_articles: list[tuple[str, str]] = []
    effective_limit = args.limit if args.limit > 0 else config.max_posts_per_run
    if effective_limit > 0:
        if args.dry_run:
            print(f"Max kept new articles this dry run: {effective_limit}")
        else:
            print(f"Max new Notion pages to create this run: {effective_limit}")

    def limit_reached() -> bool:
        if effective_limit <= 0:
            return False
        return counts["kept"] >= effective_limit if args.dry_run else counts["created"] >= effective_limit

    def import_article(normalized_url: str, retry: bool = False) -> None:
        article = fetch_article(normalized_url, crawl_config)
        classification = classify_article(article, classifier_rules)
        if not classification.keep:
            if normalized_url not in counted_excluded_urls:
                counted_excluded_urls.add(normalized_url)
                counts["excluded"] += 1
                excluded_articles.append((article.source_url, classification.selection_basis))
            return
        if normalized_url not in counted_kept_urls:
            counted_kept_urls.add(normalized_url)
            counts["kept"] += 1
        prefix = "RETRY KEEP" if retry else "KEEP"
        print(f"{prefix} {article.source_url} tags={','.join(classification.tags)}")
        if args.dry_run:
            return
        assert ai_client is not None and notion is not None
        translated = ai_client.translate(article, classification)
        notion.create_page(article, classification.tags, translated)
        existing_urls.add(normalized_url)
        counts["created"] += 1
        if config.request_delay_seconds > 0:
            time.sleep(config.request_delay_seconds)

    for url in urls:
        if limit_reached():
            break
        normalized_url = normalize_url(url)
        if normalized_url in existing_urls:
            counts["skipped_existing"] += 1
            continue
        try:
            import_article(normalized_url)
        except Exception as exc:  # keep one bad article from ending the whole scheduled run
            failure_messages[normalized_url] = str(exc)
            failed_urls.append(normalized_url)
            print(f"FAILED {normalized_url}: {exc}", file=sys.stderr)

    retry_passes = 0 if args.dry_run else max(0, config.failed_article_retry_passes)
    for retry_pass in range(1, retry_passes + 1):
        if not failed_urls or limit_reached():
            break
        pending = failed_urls
        failed_urls = []
        for normalized_url in pending:
            if limit_reached():
                failed_urls.append(normalized_url)
                continue
            if normalized_url in existing_urls:
                failure_messages.pop(normalized_url, None)
                continue
            counts["retry_attempts"] += 1
            try:
                print(f"RETRY {normalized_url} pass={retry_pass}")
                import_article(normalized_url, retry=True)
                failure_messages.pop(normalized_url, None)
            except Exception as exc:
                failure_messages[normalized_url] = str(exc)
                failed_urls.append(normalized_url)
                print(f"FAILED RETRY {normalized_url}: {exc}", file=sys.stderr)

    counts["failed"] = len(failure_messages)

    print("\nRun summary")
    for key, value in counts.items():
        print(f"- {key}: {value}")
    if excluded_articles:
        print("\nExcluded (for rule auditing)")
        for url, reason in excluded_articles:
            print(f"- {url}: {reason}")
    if failure_messages:
        print("\nFailures")
        for url, message in failure_messages.items():
            print(f"- {url}: {message}")
    return 1 if failure_messages and not args.dry_run else 0


if __name__ == "__main__":
    raise SystemExit(main())
