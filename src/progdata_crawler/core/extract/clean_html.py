"""Cleaned HTML: the page's content without anything that runs or only styles it."""

from selectolax.lexbor import LexborHTMLParser, LexborNode

from progdata_crawler.core.extract.links import absolute_url
from progdata_crawler.core.extract.markdown import (
    BOILERPLATE_SELECTORS,
    MAIN_REGION_SELECTORS,
)
from progdata_crawler.models.options import ContentMode, SelectorFilter

REMOVED_ELEMENTS = (
    "script, style, noscript, template, iframe, object, embed, link, meta, base, svg, canvas"
)
URL_ATTRIBUTES = ("href", "src", "poster")
DROPPED_ATTRIBUTES = frozenset({"style", "srcset", "nonce", "integrity", "crossorigin"})


def _clean_node(root: LexborNode, base_url: str) -> None:
    for node in root.css(REMOVED_ELEMENTS):
        node.decompose()
    # Collect first: changing the tree while traversing it is not safe.
    nodes = list(root.traverse(include_text=True))
    comments = [node for node in nodes if (node.tag or "") in ("-comment", "_comment")]
    elements = [node for node in nodes if not (node.tag or "").startswith(("-", "_"))]
    for node in elements:
        attrs = node.attrs
        for name in list(attrs.keys()):
            lowered = name.lower()
            if lowered.startswith("on") or lowered in DROPPED_ATTRIBUTES:
                del attrs[name]
        for name in URL_ATTRIBUTES:
            value = attrs.get(name)
            if value is None:
                continue
            url = absolute_url(base_url, value)
            if url is None:
                del attrs[name]
            else:
                attrs[name] = url
    for node in comments:
        node.decompose()


def clean_html(
    html: str,
    base_url: str,
    *,
    content: ContentMode = ContentMode.MAIN,
    tags: SelectorFilter | None = None,
) -> str:
    """The cleaned HTML of the main content (or the whole body with `content=full`).

    Scripts, styles, frames, event handlers, inline styles and comments are removed, and
    links and images get absolute URLs. `tags` works as for markdown.
    """
    tags = tags or SelectorFilter()
    tree = LexborHTMLParser(html)
    for selector in tags.exclude:
        for node in tree.css(selector):
            node.decompose()

    if tags.include:
        roots: list[LexborNode] = []
        for selector in tags.include:
            roots.extend(tree.css(selector))
    elif content is ContentMode.FULL:
        roots = [tree.body] if tree.body is not None else []
    else:
        region = next(
            (node for s in MAIN_REGION_SELECTORS if (node := tree.css_first(s)) is not None),
            None,
        )
        if region is None and tree.body is not None:
            for node in tree.body.css(BOILERPLATE_SELECTORS):
                node.decompose()
            region = tree.body
        roots = [region] if region is not None else []

    parts: list[str] = []
    for root in roots:
        _clean_node(root, base_url)
        if root.tag == "body":
            parts.append("".join(child.html or "" for child in root.iter(include_text=True)))
        else:
            parts.append(root.html or "")
    return "\n".join(part.strip() for part in parts if part.strip())
