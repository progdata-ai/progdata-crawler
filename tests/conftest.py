from collections.abc import Iterator

import pytest

from tests.support.site_server import SiteServer, run_site


@pytest.fixture(scope="session")
def _site_server() -> Iterator[SiteServer]:
    with run_site() as site:
        yield site


@pytest.fixture
def test_site(_site_server: SiteServer) -> Iterator[SiteServer]:
    """The local test site. Routes added with `test_site.add` are removed after each test."""
    yield _site_server
    _site_server.reset()
