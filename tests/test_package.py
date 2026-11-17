from importlib.metadata import version

import progdata_crawler


def test_version_matches_distribution_metadata() -> None:
    assert progdata_crawler.__version__ == version("progdata-crawler")
