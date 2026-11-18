"""What we return for a page: one field per requested output."""

from pydantic import BaseModel, ConfigDict, Field

from progdata_crawler.models.steps import Capture


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True)


class Link(_Model):
    url: str
    """Absolute URL without the #fragment."""
    text: str
    """The link text, whitespace collapsed. Empty for links without text."""
    internal: bool
    """True when the link points to the same host as the page (ignoring `www.`)."""
    rel: tuple[str, ...] = ()
    """Values of the rel attribute, such as `nofollow`."""


class Image(_Model):
    url: str
    alt: str = ""
    width: int | None = None
    height: int | None = None


class PageMetadata(_Model):
    title: str | None = None
    description: str | None = None
    language: str | None = None
    canonical_url: str | None = None
    author: str | None = None
    keywords: tuple[str, ...] = ()
    published_time: str | None = None
    """As the page states it, usually ISO 8601."""
    open_graph: dict[str, str] = Field(default_factory=dict[str, str])
    """`og:*` properties without the prefix, e.g. `{"title": ..., "image": ...}`."""
    status: int | None = None
    """HTTP status of the fetched page."""
    content_type: str | None = None


class Page(_Model):
    """The result of fetching one page. Only requested outputs are set."""

    url: str
    """The final URL, after redirects."""
    markdown: str | None = None
    html: str | None = None
    """Cleaned HTML: no scripts, styles, event handlers or comments; absolute URLs."""
    raw_html: str | None = None
    links: tuple[Link, ...] | None = None
    images: tuple[Image, ...] | None = None
    metadata: PageMetadata | None = None
    screenshot: str | None = None
    """PNG, base64-encoded."""
    captures: tuple[Capture, ...] | None = None
    """The page at each `capture` step, when the request had steps."""
