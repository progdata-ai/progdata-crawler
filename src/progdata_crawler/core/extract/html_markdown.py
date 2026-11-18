"""A small HTML-to-markdown converter.

Used for whole pages (`content=full`), for elements picked with `tags.include`, and as
the fallback when main-content extraction finds nothing. It handles the elements that
carry meaning in text: headings, paragraphs, lists, links, images, emphasis, code,
quotes and tables. Everything else is reduced to its text.
"""

import re
from urllib.parse import urljoin

from selectolax.lexbor import LexborNode

SKIP_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "svg",
        "head",
        "iframe",
        "object",
        "embed",
        "canvas",
        "form",
        "button",
        "input",
        "select",
        "textarea",
        "-comment",
        "_comment",
    }
)

CONTAINER_TAGS = frozenset(
    {
        "html",
        "body",
        "div",
        "section",
        "article",
        "main",
        "header",
        "footer",
        "nav",
        "aside",
        "figure",
        "figcaption",
        "details",
        "summary",
        "dl",
        "dt",
        "dd",
        "address",
        "center",
    }
)

HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
BLOCK_TAGS = (
    CONTAINER_TAGS | HEADINGS.keys() | {"p", "ul", "ol", "li", "pre", "blockquote", "table", "hr"}
)

_SPACES = re.compile(r"[ \t\r\f\v\n]+")


def _collapse(text: str) -> str:
    return _SPACES.sub(" ", text)


class _Converter:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url

    def url(self, href: str) -> str:
        return urljoin(self.base_url, href.strip())

    # Blocks

    def blocks(self, node: LexborNode) -> list[str]:
        blocks: list[str] = []
        inline: list[str] = []

        def flush() -> None:
            text = _finish_inline("".join(inline))
            if text:
                blocks.append(text)
            inline.clear()

        for child in node.iter(include_text=True):
            tag = child.tag or ""
            if tag == "-text":
                inline.append(_collapse(child.text_content or ""))
            elif tag in SKIP_TAGS:
                continue
            elif tag in BLOCK_TAGS:
                flush()
                blocks.extend(self.block(child, tag))
            else:
                inline.append(self.inline(child))
        flush()
        return blocks

    def block(self, node: LexborNode, tag: str) -> list[str]:
        if tag in HEADINGS:
            text = _finish_inline(self.inline_children(node))
            return [f"{'#' * HEADINGS[tag]} {text}"] if text else []
        if tag == "p":
            text = _finish_inline(self.inline_children(node))
            return [text] if text else []
        if tag in ("ul", "ol"):
            return self.list_block(node, ordered=tag == "ol")
        if tag == "li":
            return self.list_block(node, ordered=False, items=[node])
        if tag == "pre":
            code = (node.text(deep=True, separator="", strip=False) or "").strip("\n")
            return [f"```\n{code}\n```"] if code.strip() else []
        if tag == "blockquote":
            inner = "\n\n".join(self.blocks(node))
            return ["\n".join(f"> {line}" if line else ">" for line in inner.split("\n"))]
        if tag == "table":
            return self.table(node)
        if tag == "hr":
            return ["---"]
        return self.blocks(node)

    def list_block(
        self, node: LexborNode, *, ordered: bool, items: list[LexborNode] | None = None
    ) -> list[str]:
        items = items if items is not None else [c for c in node.iter() if c.tag == "li"]
        lines: list[str] = []
        for number, item in enumerate(items, start=1):
            marker = f"{number}. " if ordered else "- "
            indent = " " * len(marker)
            parts = self.blocks(item) or [""]
            first, *rest = "\n\n".join(parts).split("\n")
            lines.append(marker + first)
            lines.extend(indent + line if line else "" for line in rest)
        return ["\n".join(lines)] if lines else []

    def table(self, node: LexborNode) -> list[str]:
        rows: list[list[str]] = []
        for row in node.css("tr"):
            cells = [
                _finish_inline(self.inline_children(cell)).replace("|", "\\|")
                for cell in row.iter()
                if cell.tag in ("th", "td")
            ]
            if cells:
                rows.append(cells)
        if not rows:
            return []
        width = max(len(row) for row in rows)
        rows = [row + [""] * (width - len(row)) for row in rows]
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
        lines.extend("| " + " | ".join(row) + " |" for row in rows[1:])
        return ["\n".join(lines)]

    # Inline

    def inline_children(self, node: LexborNode) -> str:
        parts: list[str] = []
        for child in node.iter(include_text=True):
            if child.tag == "-text":
                parts.append(_collapse(child.text_content or ""))
            elif (child.tag or "") not in SKIP_TAGS:
                parts.append(self.inline(child))
        return "".join(parts)

    def inline(self, node: LexborNode) -> str:
        tag = node.tag or ""
        attrs = node.attributes
        if tag == "br":
            return "\n"
        if tag == "img":
            src = attrs.get("src")
            if not src:
                return ""
            alt = _collapse(attrs.get("alt") or "").strip()
            return f"![{alt}]({self.url(src)})"
        text = self.inline_children(node)
        if tag == "a":
            href = (attrs.get("href") or "").strip()
            label = text.strip()
            if not href or href.startswith(("javascript:", "#")) or not label:
                return text
            return f"[{label}]({self.url(href)})"
        if tag in ("strong", "b"):
            return _wrap(text, "**")
        if tag in ("em", "i"):
            return _wrap(text, "*")
        if tag == "code":
            return _wrap(text, "`")
        if tag in BLOCK_TAGS:
            return f" {text} "
        return text


def _wrap(text: str, mark: str) -> str:
    stripped = text.strip()
    if not stripped:
        return text
    lead = " " if text[:1].isspace() else ""
    trail = " " if text[-1:].isspace() else ""
    return f"{lead}{mark}{stripped}{mark}{trail}"


def _finish_inline(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def html_to_markdown(node: LexborNode, base_url: str) -> str:
    """Convert an element and its children to markdown. Relative URLs use `base_url`."""
    converter = _Converter(base_url)
    tag = node.tag or ""
    if tag in BLOCK_TAGS and tag not in CONTAINER_TAGS:
        blocks = converter.block(node, tag)
    else:
        blocks = converter.blocks(node)
    return "\n\n".join(blocks).strip()
