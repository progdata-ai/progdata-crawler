"""Turning a page into markdown.

- `content=main` (default): the main content, found with trafilatura. If it finds too
  little, we fall back to converting the page's main region ourselves.
- `content=full`: the whole page body.
- `tags.exclude` removes elements first. `tags.include` keeps only the matching elements,
  converted in document order.
"""

import re
import warnings

from selectolax.lexbor import LexborHTMLParser, LexborNode

from progdata_crawler.core.extract.html_markdown import html_to_markdown
from progdata_crawler.models.options import ContentMode, SelectorFilter

with warnings.catch_warnings():
    # urllib3, pulled in by trafilatura, warns about optional SOCKS support on import.
    warnings.simplefilter("ignore")
    import trafilatura

MIN_MAIN_CHARS = 100
"""If main-content extraction returns less than this, use the fallback instead."""

MAIN_REGION_SELECTORS = ("main", "article", "[role=main]")
BOILERPLATE_SELECTORS = (
    "nav, header, footer, aside, [role=navigation], [role=banner], [role=contentinfo], "
    "[aria-hidden=true]"
)

_BLANK_LINES = re.compile(r"\n{3,}")


def _apply_tags(tree: LexborHTMLParser, tags: SelectorFilter) -> list[LexborNode]:
    """Remove excluded elements and return the roots to convert."""
    for selector in tags.exclude:
        for node in tree.css(selector):
            node.decompose()
    if tags.include:
        selected: list[LexborNode] = []
        for selector in tags.include:
            selected.extend(tree.css(selector))
        # Keep document order and drop elements nested inside another selected one.
        selected = [
            node
            for node in _in_document_order(tree, selected)
            if not any(other is not node and _contains(other, node) for other in selected)
        ]
        return selected
    return [tree.body] if tree.body is not None else []


def _in_document_order(tree: LexborHTMLParser, nodes: list[LexborNode]) -> list[LexborNode]:
    root = tree.root
    if root is None:
        return nodes
    order = {node.mem_id: index for index, node in enumerate(root.traverse())}
    unique = {node.mem_id: node for node in nodes}
    return sorted(unique.values(), key=lambda node: order.get(node.mem_id, 0))


def _contains(ancestor: LexborNode, node: LexborNode) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.mem_id == ancestor.mem_id:
            return True
        parent = parent.parent
    return False


def _semantic_region(tree: LexborHTMLParser) -> LexborNode | None:
    for selector in MAIN_REGION_SELECTORS:
        node = tree.css_first(selector)
        if node is not None:
            return node
    return None


def _stripped_body(tree: LexborHTMLParser) -> LexborNode | None:
    body = tree.body
    if body is not None:
        for node in body.css(BOILERPLATE_SELECTORS):
            node.decompose()
    return body


def _lost_headings(markdown: str, scope: LexborNode | None) -> bool:
    """True when the page has headings but the extracted markdown has none, a sign that
    extraction fell back to plain text."""
    if scope is None or not scope.css("h1, h2, h3"):
        return False
    return not any(line.startswith("#") for line in markdown.splitlines())


def _extract_main(html: str, base_url: str) -> str | None:
    result = trafilatura.extract(
        html,
        url=base_url,
        output_format="markdown",
        include_formatting=True,
        include_links=True,
        include_images=True,
        include_tables=True,
        include_comments=False,
        with_metadata=False,
        deduplicate=False,
    )
    return result if isinstance(result, str) else None


_BLOCK_START = re.compile(r"^(?:[-*+] |\d+\. |#|>|\||```|!\[)")


def _tidy(markdown: str) -> str:
    """Trim trailing spaces, collapse blank lines, and re-join paragraph lines that the
    source HTML indented (outside code blocks and lists)."""
    lines: list[str] = []
    in_fence = False
    for raw in markdown.strip().split("\n"):
        line = raw.rstrip()
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            lines.append(line)
            continue
        previous = lines[-1] if lines else ""
        is_continuation = (
            not in_fence
            and line[:1].isspace()
            and previous.strip()
            and not _BLOCK_START.match(previous.lstrip())
            and not previous[:1].isspace()
            and not _BLOCK_START.match(line.lstrip())
        )
        if is_continuation:
            lines[-1] = f"{previous} {line.strip()}"
        else:
            lines.append(line)
    return _BLANK_LINES.sub("\n\n", "\n".join(lines))


def to_markdown(
    html: str,
    base_url: str,
    *,
    content: ContentMode = ContentMode.MAIN,
    tags: SelectorFilter | None = None,
) -> str:
    """Convert a page to markdown. Relative links and images become absolute."""
    tags = tags or SelectorFilter()
    tree = LexborHTMLParser(html)
    for node in tree.css("noscript, template"):
        node.decompose()  # fallback content for browsers without JavaScript, not the page
    roots = _apply_tags(tree, tags)

    if tags.include or content is ContentMode.FULL:
        parts = [html_to_markdown(root, base_url) for root in roots]
        return _tidy("\n\n".join(part for part in parts if part))

    # When the page marks its main content, trust that and clean it up. Otherwise let
    # trafilatura find the main content in the whole page.
    region = _semantic_region(tree)
    source = f"<html><body>{region.html}</body></html>" if region is not None else tree.html
    main = _extract_main(source or "", base_url)
    scope = region if region is not None else tree.body
    if main and len(main) >= MIN_MAIN_CHARS and not _lost_headings(main, scope):
        return _tidy(main)
    if region is None:
        region = _stripped_body(tree)
    fallback = html_to_markdown(region, base_url) if region is not None else ""
    return _tidy(fallback or main or "")
