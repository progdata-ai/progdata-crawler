"""A generated documentation site of 100 pages under /manual/, for end-to-end crawls.

    /manual/                                 the start page          (depth 0)
    /manual/<section>/                       3 sections              (depth 1)
    /manual/<section>/<topic>/               4 topics per section    (depth 2)
    /manual/<section>/<topic>/page-<n>.html  7 pages per topic       (depth 3)

1 + 3 + 12 + 84 = 100 pages. Every page links to its parent; topic pages link to their
neighbours and to a page in another section. Some links leave the manual: a blog, a
changelog and a page that robots.txt disallows. `/manual/sitemap.xml` lists every page.
"""

import html

SECTIONS = ("install", "configure", "operate")
TOPICS = ("basics", "advanced", "reference", "troubleshooting")
PAGES_PER_TOPIC = 7

ROBOTS = "User-agent: *\nDisallow: /manual/internal/\n\nSitemap: {sitemap}\n"
"""robots.txt for tests that use the manual. Fill in the sitemap URL."""


def _page_path(section: str, topic: str, n: int) -> str:
    return f"/manual/{section}/{topic}/page-{n}.html"


def manual_paths() -> list[str]:
    paths = ["/manual/"]
    for section in SECTIONS:
        paths.append(f"/manual/{section}/")
        for topic in TOPICS:
            paths.append(f"/manual/{section}/{topic}/")
            paths += [_page_path(section, topic, n) for n in range(1, PAGES_PER_TOPIC + 1)]
    return paths


MANUAL_PATHS = tuple(manual_paths())
"""Every page of the manual, in breadth-first order."""


def paths_up_to_depth(depth: int) -> set[str]:
    return {path for path in MANUAL_PATHS if path.count("/") - 2 + path.endswith(".html") <= depth}


def _document(title: str, body: str) -> bytes:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title></head><body>"
        "<nav><a href='/manual/'>Manual</a> <a href='/blog/'>Blog</a> "
        "<a href='/changelog.html'>Changelog</a></nav>"
        f"<main><h1>{html.escape(title)}</h1>{body}</main></body></html>"
    ).encode()


def _links(items: list[tuple[str, str]]) -> str:
    return (
        "<ul>" + "".join(f"<li><a href='{href}'>{text}</a></li>" for href, text in items) + "</ul>"
    )


def manual_page(path: str) -> bytes | None:
    """The page at `path`, or None when the manual has no such page."""
    if path == "/manual/":
        sections = [(f"/manual/{s}/", s.title()) for s in SECTIONS]
        return _document("Manual", "<p>How to run the example service.</p>" + _links(sections))
    parts = path.removeprefix("/manual/").rstrip("/").split("/")
    if len(parts) == 1 and parts[0] in SECTIONS:
        section = parts[0]
        topics = [(f"/manual/{section}/{t}/", t.title()) for t in TOPICS]
        return _document(f"{section.title()}", f"<p>All about {section}.</p>" + _links(topics))
    if len(parts) == 2 and parts[0] in SECTIONS and parts[1] in TOPICS:
        section, topic = parts
        pages = [
            (_page_path(section, topic, n), f"{topic.title()} {n}")
            for n in range(1, PAGES_PER_TOPIC + 1)
        ]
        body = f"<p>{topic.title()} of {section}.</p>" + _links(
            [(f"/manual/{section}/", "Up"), *pages]
        )
        return _document(f"{section.title()}: {topic.title()}", body)
    if len(parts) == 3 and parts[0] in SECTIONS and parts[1] in TOPICS:
        section, topic, name = parts
        number = name.removeprefix("page-").removesuffix(".html")
        if not number.isdigit() or not 1 <= int(number) <= PAGES_PER_TOPIC:
            return None
        n = int(number)
        other = SECTIONS[(SECTIONS.index(section) + 1) % len(SECTIONS)]
        links = [
            (f"/manual/{section}/{topic}/", "Up"),
            (_page_path(section, topic, n % PAGES_PER_TOPIC + 1), "Next"),
            (_page_path(other, topic, n), f"See also: {other}"),
            (f"/blog/{section}-{topic}-{n}.html", "Blog post"),
        ]
        if n == 1:
            links.append(("/manual/internal/notes.html", "Internal notes"))
        title = f"{section.title()}: {topic.title()} {n}"
        text = f"<p>Step {n} of {topic} for {section}: unique text {section}{topic}{n}.</p>"
        return _document(title, text + _links(links))
    return None


def outside_page(path: str) -> bytes | None:
    """Pages the manual links to that are not part of it."""
    if (
        path.startswith("/blog/")
        or path == "/changelog.html"
        or path == "/manual/internal/notes.html"
    ):
        return _document("Elsewhere", "<p>Not part of the manual.</p>")
    return None


def manual_sitemap(base_url: str) -> bytes:
    entries = "".join(f"<url><loc>{base_url}{path}</loc></url>" for path in MANUAL_PATHS)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</urlset>'
    ).encode()
