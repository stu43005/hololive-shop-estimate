"""Tests for Shopify sitemap enumeration."""

import asyncio
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from estimator_king.crawler.async_http_client import AsyncHTTPClientError, ClientError
from estimator_king.crawler.sitemap import (
    SitemapEnumerator,
    SitemapError,
    SitemapParseError,
)


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def sitemap_index_xml(fixtures_dir: Path) -> bytes:
    with open(fixtures_dir / "sitemap_index.xml", "rb") as f:
        return f.read()


@pytest.fixture
def sitemap_products_1_xml(fixtures_dir: Path) -> bytes:
    with open(fixtures_dir / "sitemap_products_1.xml", "rb") as f:
        return f.read()


@pytest.fixture
def sitemap_products_2_xml(fixtures_dir: Path) -> bytes:
    with open(fixtures_dir / "sitemap_products_2.xml", "rb") as f:
        return f.read()


class FakeAsyncClient:
    """Minimal async stand-in for AsyncHTTPClient.get used by SitemapEnumerator.

    `router(url)` returns the XML text (str) for that URL, or raises.
    """

    def __init__(self, router):
        self._router = router
        self.call_urls: list[str] = []

    async def get(self, url: str) -> str:
        self.call_urls.append(url)
        return self._router(url)


def _fixtures_router(index_xml: bytes, p1_xml: bytes, p2_xml: bytes):
    def router(url: str) -> str:
        if url.endswith("/sitemap.xml"):
            return index_xml.decode("utf-8")
        elif "products_1" in url:
            return p1_xml.decode("utf-8")
        elif "products_2" in url:
            return p2_xml.decode("utf-8")
        raise AssertionError(f"Unexpected URL: {url}")

    return router


class TestSitemapEnumeratorParsingFixtures:
    """Test parsing of fixture-based sitemaps (pure ET, no client)."""

    def test_parse_sitemap_index_fixture(self, sitemap_index_xml: bytes):
        root = ET.fromstring(sitemap_index_xml)
        ns = {"sitemap": "http://www.sitemaps.org/schemas/sitemap/0.9"}

        sitemaps = root.findall("sitemap:sitemap", ns)
        assert len(sitemaps) == 4

        locs = [
            elem.find("sitemap:loc", ns).text
            for elem in sitemaps
            if elem.find("sitemap:loc", ns) is not None
        ]
        assert len(locs) == 4
        assert any("products_1" in loc for loc in locs)
        assert any("products_2" in loc for loc in locs)
        assert any("pages_1" in loc for loc in locs)
        assert any("collections_1" in loc for loc in locs)

    def test_parse_sitemap_products_1_fixture(self, sitemap_products_1_xml: bytes):
        root = ET.fromstring(sitemap_products_1_xml)
        ns = {"sitemap": "http://www.sitemaps.org/schemas/sitemap/0.9"}

        urls = root.findall("sitemap:url", ns)
        assert len(urls) == 5

        locs = [
            elem.find("sitemap:loc", ns).text
            for elem in urls
            if elem.find("sitemap:loc", ns) is not None
        ]
        assert "https://shop.example.com/products/item-001" in locs
        assert "https://shop.example.com/products/item-002" in locs
        assert "https://shop.example.com/en/products/item-001-en" in locs

    def test_parse_sitemap_products_2_fixture(self, sitemap_products_2_xml: bytes):
        root = ET.fromstring(sitemap_products_2_xml)
        ns = {"sitemap": "http://www.sitemaps.org/schemas/sitemap/0.9"}

        urls = root.findall("sitemap:url", ns)
        assert len(urls) == 5

        locs = [
            elem.find("sitemap:loc", ns).text
            for elem in urls
            if elem.find("sitemap:loc", ns) is not None
        ]
        assert "https://shop.example.com/products/item-005" in locs
        assert "https://shop.example.com/products/item-006" in locs


class TestSitemapEnumeratorIntegration:
    """Integration tests with an async fake HTTP client."""

    def _enumerate(self, index_xml, p1_xml, p2_xml):
        client = FakeAsyncClient(_fixtures_router(index_xml, p1_xml, p2_xml))
        enumerator = SitemapEnumerator(http_client=client)
        urls = asyncio.run(enumerator.enumerate_products("https://shop.example.com"))
        return client, urls

    def test_enumerate_products_with_mocked_http(
        self, sitemap_index_xml, sitemap_products_1_xml, sitemap_products_2_xml
    ):
        _, urls = self._enumerate(sitemap_index_xml, sitemap_products_1_xml, sitemap_products_2_xml)
        assert isinstance(urls, list)
        assert len(urls) > 0
        assert all(isinstance(url, str) for url in urls)

    def test_enumerate_products_excludes_en_paths(
        self, sitemap_index_xml, sitemap_products_1_xml, sitemap_products_2_xml
    ):
        _, urls = self._enumerate(sitemap_index_xml, sitemap_products_1_xml, sitemap_products_2_xml)
        assert "/en/" not in "\n".join(urls)
        assert all("/en/" not in url for url in urls)

    def test_enumerate_products_returns_sorted(
        self, sitemap_index_xml, sitemap_products_1_xml, sitemap_products_2_xml
    ):
        _, urls = self._enumerate(sitemap_index_xml, sitemap_products_1_xml, sitemap_products_2_xml)
        assert urls == sorted(urls)

    def test_enumerate_products_deduplicates(
        self, sitemap_index_xml, sitemap_products_1_xml, sitemap_products_2_xml
    ):
        _, urls = self._enumerate(sitemap_index_xml, sitemap_products_1_xml, sitemap_products_2_xml)
        assert len(urls) == len(set(urls))

    def test_enumerate_products_includes_query_params(
        self, sitemap_index_xml, sitemap_products_1_xml, sitemap_products_2_xml
    ):
        _, urls = self._enumerate(sitemap_index_xml, sitemap_products_1_xml, sitemap_products_2_xml)
        assert any("variant=" in url for url in urls)

    def test_enumerate_products_skips_non_products_sitemaps(
        self, sitemap_index_xml, sitemap_products_1_xml, sitemap_products_2_xml
    ):
        client, _ = self._enumerate(sitemap_index_xml, sitemap_products_1_xml, sitemap_products_2_xml)
        assert any("products" in url for url in client.call_urls)
        assert not any("pages_1" in url for url in client.call_urls)
        assert not any("collections_1" in url for url in client.call_urls)


class TestSitemapEnumeratorErrorHandling:
    """Test error handling in sitemap enumeration."""

    def test_parse_error_on_malformed_index(self):
        client = FakeAsyncClient(lambda url: "<not-valid-xml>")
        enumerator = SitemapEnumerator(http_client=client)

        with pytest.raises(SitemapError):
            asyncio.run(enumerator.enumerate_products("https://shop.example.com"))

    def test_parse_error_on_malformed_products_sitemap(self, sitemap_index_xml):
        def router(url: str) -> str:
            if url.endswith("/sitemap.xml"):
                return sitemap_index_xml.decode("utf-8")
            return "<not-closed-xml>"

        client = FakeAsyncClient(router)
        enumerator = SitemapEnumerator(http_client=client)

        with pytest.raises(SitemapParseError):
            asyncio.run(enumerator.enumerate_products("https://shop.example.com"))

    def test_http_error_on_fetch_failure(self):
        def router(url: str) -> str:
            raise AsyncHTTPClientError("Connection failed")

        client = FakeAsyncClient(router)
        enumerator = SitemapEnumerator(http_client=client)

        with pytest.raises(SitemapError):
            asyncio.run(enumerator.enumerate_products("https://shop.example.com"))

    def test_client_error_4xx_wraps_to_sitemap_error(self):
        def router(url: str) -> str:
            raise ClientError(url, status_code=404)

        client = FakeAsyncClient(router)
        enumerator = SitemapEnumerator(http_client=client)

        with pytest.raises(SitemapError):
            asyncio.run(enumerator.enumerate_products("https://shop.example.com"))

    def test_empty_sitemapindex_returns_empty_list(self):
        empty_index = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
</sitemapindex>
"""
        client = FakeAsyncClient(lambda url: empty_index)
        enumerator = SitemapEnumerator(http_client=client)
        urls = asyncio.run(enumerator.enumerate_products("https://shop.example.com"))

        assert urls == []

    def test_sitemapindex_without_products_returns_empty_list(self):
        no_products_index = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap>
    <loc>https://shop.example.com/sitemap_pages_1.xml</loc>
  </sitemap>
</sitemapindex>
"""
        client = FakeAsyncClient(lambda url: no_products_index)
        enumerator = SitemapEnumerator(http_client=client)
        urls = asyncio.run(enumerator.enumerate_products("https://shop.example.com"))

        assert urls == []


class TestSitemapEnumeratorRealFixtures:
    """Tests using the actual fixture files from disk."""

    def test_enumerate_with_real_fixtures(
        self, sitemap_index_xml, sitemap_products_1_xml, sitemap_products_2_xml
    ):
        client = FakeAsyncClient(
            _fixtures_router(sitemap_index_xml, sitemap_products_1_xml, sitemap_products_2_xml)
        )
        enumerator = SitemapEnumerator(http_client=client)
        urls = asyncio.run(enumerator.enumerate_products("https://shop.example.com"))

        expected_urls = [
            "https://shop.example.com/products/item-001",
            "https://shop.example.com/products/item-002",
            "https://shop.example.com/products/item-003",
            "https://shop.example.com/products/item-004?variant=blue",
            "https://shop.example.com/products/item-004?variant=red",
            "https://shop.example.com/products/item-005",
            "https://shop.example.com/products/item-006",
            "https://shop.example.com/products/item-007",
        ]

        assert sorted(urls) == sorted(expected_urls)
        assert all("/en/" not in url for url in urls)
