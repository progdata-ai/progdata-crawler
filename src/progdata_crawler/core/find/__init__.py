"""Web search through pluggable search providers."""

from progdata_crawler.core.find.provider import SearchProvider
from progdata_crawler.core.find.search import create_provider, find, search

__all__ = ["SearchProvider", "create_provider", "find", "search"]
