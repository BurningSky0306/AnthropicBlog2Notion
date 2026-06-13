from __future__ import annotations

import re
from urllib.parse import urljoin


def compact_inline(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def compact_block(text: str) -> str:
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = strip_standalone_copy_lines(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def strip_standalone_copy_lines(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if line.strip().lower() != "copy")


def node_to_markdown(node, base_url: str) -> str:
    name = getattr(node, "name", None)
    if name is None:
        return compact_inline(str(node))
    if name in {"script", "style", "noscript", "nav", "footer", "svg"}:
        return ""
    if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        return "\n\n" + "#" * int(name[1]) + " " + node.get_text(" ", strip=True) + "\n\n"
    if name == "p":
        return "\n\n" + inline_children(node, base_url) + "\n\n"
    if name == "br":
        return "\n"
    if name in {"strong", "b"}:
        return f"**{inline_children(node, base_url)}**"
    if name in {"em", "i"}:
        return f"*{inline_children(node, base_url)}*"
    if name == "code":
        return "`" + node.get_text("", strip=True) + "`"
    if name == "pre":
        return "\n\n```\n" + node.get_text("\n", strip=False).strip() + "\n```\n\n"
    if name == "a":
        label = inline_children(node, base_url) or node.get_text(" ", strip=True)
        href = node.get("href")
        return f"[{label}]({urljoin(base_url, href)})" if href else label
    if name == "img":
        src = node.get("src")
        alt = node.get("alt") or "Image"
        return f"\n\n![{alt}]({urljoin(base_url, src)})\n\n" if src else ""
    if name == "iframe":
        src = node.get("src")
        return f"\n\n{urljoin(base_url, src)}\n\n" if src else ""
    if name in {"video", "source"}:
        urls = media_source_urls(node, base_url)
        return "\n\n" + "\n\n".join(urls) + "\n\n" if urls else ""
    if name == "details":
        return details_to_markdown(node, base_url)
    if name == "summary":
        return "**" + inline_children(node, base_url) + "**"
    if name in {"ul", "ol"}:
        ordered = name == "ol"
        lines = []
        for index, li in enumerate(node.find_all("li", recursive=False), 1):
            bullet = f"{index}. " if ordered else "- "
            lines.append(bullet + compact_block(inline_children(li, base_url)))
        return "\n\n" + "\n".join(lines) + "\n\n"
    if name == "blockquote":
        quote = compact_block(children_to_markdown(node, base_url))
        return "\n\n" + "\n".join("> " + line for line in quote.splitlines()) + "\n\n"
    if name == "table":
        return "\n\n" + table_to_markdown(node) + "\n\n"
    if name == "figure":
        return "\n\n" + compact_block(children_to_markdown(node, base_url)) + "\n\n"
    return children_to_markdown(node, base_url)


def inline_children(node, base_url: str) -> str:
    return compact_inline("".join(node_to_markdown(child, base_url) for child in node.children))


def children_to_markdown(node, base_url: str) -> str:
    return "".join(node_to_markdown(child, base_url) for child in node.children)


def media_source_urls(node, base_url: str) -> list[str]:
    urls: list[str] = []
    for candidate in [node, *node.find_all("source")]:
        src = candidate.get("src")
        if src:
            url = urljoin(base_url, src)
            if url not in urls:
                urls.append(url)
    return urls


def details_to_markdown(node, base_url: str) -> str:
    summary = node.find("summary", recursive=False)
    summary_text = inline_children(summary, base_url) if summary else "Details"
    body = ""
    for child in node.children:
        if child is summary:
            continue
        body += node_to_markdown(child, base_url)
    body = compact_block(body)
    return f"\n\n<details>\n<summary>{summary_text}</summary>\n\n{body}\n\n</details>\n\n"


def table_to_markdown(table) -> str:
    rows = []
    for tr in table.find_all("tr"):
        cells = [compact_inline(cell.get_text(" ", strip=True)) for cell in tr.find_all(["th", "td"])]
        if cells:
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    lines = [
        "| " + " | ".join(rows[0]) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in rows[1:])
    return "\n".join(lines)


def split_markdown(markdown: str, max_chars: int) -> list[str]:
    if len(markdown) <= max_chars:
        return [markdown]
    parts = re.split(r"(?m)(?=^#{1,3}\s+)", markdown)
    chunks: list[str] = []
    current = ""
    for part in parts:
        if current and len(current) + len(part) > max_chars:
            chunks.append(current.strip())
            current = part
        else:
            current += part
    if current.strip():
        chunks.append(current.strip())
    oversized: list[str] = []
    for chunk in chunks:
        if len(chunk) <= max_chars:
            oversized.append(chunk)
            continue
        oversized.extend(_split_on_boundary(chunk, max_chars))
    return [chunk for chunk in oversized if chunk]


def _split_on_boundary(text: str, max_chars: int) -> list[str]:
    """Split overly long text, preferring paragraph > line > space boundaries.

    Avoids slicing through the middle of a fenced code block, which would break
    the ``` pairing that translation integrity checks depend on.
    """
    pieces: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        cut = window.rfind("\n\n")
        if cut <= 0:
            cut = window.rfind("\n")
        if cut <= 0:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = max_chars
        pieces.append(remaining[:cut].strip())
        remaining = remaining[cut:].lstrip()
    if remaining.strip():
        pieces.append(remaining.strip())
    return pieces
