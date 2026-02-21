"""Tests for Shopify sitemap enumeration."""

import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from estimator_king.crawler.http_client import HTTPClient, HTTPClientError
from estimator_king.crawler.sitemap import (
    SitemapEnumerator,
    SitemapError,
    SitemapParseError,
)


@pytest.fixture
def fixtures_dir() -> Path:
    """Path to test fixtures directory."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def sitemap_index_xml(fixtures_dir: Path) -> bytes:
    """Load sitemap index fixture."""
    with open(fixtures_dir / "sitemap_index.xml", "rb") as f:
        return f.read()


@pytest.fixture
def sitemap_products_1_xml(fixtures_dir: Path) -> bytes:
    """Load products sitemap 1 fixture."""
    with open(fixtures_dir / "sitemap_products_1.xml", "rb") as f:
        return f.read()


@pytest.fixture
def sitemap_products_2_xml(fixtures_dir: Path) -> bytes:
    """Load products sitemap 2 fixture."""
    with open(fixtures_dir / "sitemap_products_2.xml", "rb") as f:
        return f.read()


class MockResponse:
    """Mock requests Response object."""

    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code
        self.text = content.decode("utf-8")

    def raise_for_status(self):
        """Simulate raise_for_status behavior."""
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class TestSitemapEnumeratorParsingFixtures:
    """Test parsing of fixture-based sitemaps."""

    def test_parse_sitemap_index_fixture(self, sitemap_index_xml: bytes):
        """Test parsing sitemapindex fixture."""
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
        """Test parsing products sitemap 1 fixture."""
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
        """Test parsing products sitemap 2 fixture."""
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
    """Integration tests with mocked HTTP client."""

    def test_enumerate_products_with_mocked_http(
        self,
        sitemap_index_xml: bytes,
        sitemap_products_1_xml: bytes,
        sitemap_products_2_xml: bytes,
    ):
        """Test full enumeration flow with mocked HTTP."""
        mock_client = MagicMock(spec=HTTPClient)

        def get_side_effect(url: str):
            if url.endswith("/sitemap.xml"):
                return MockResponse(sitemap_index_xml)
            elif "products_1" in url:
                return MockResponse(sitemap_products_1_xml)
            elif "products_2" in url:
                return MockResponse(sitemap_products_2_xml)
            else:
                raise Exception(f"Unexpected URL: {url}")

        mock_client.get.side_effect = get_side_effect

        enumerator = SitemapEnumerator(http_client=mock_client)
        urls = enumerator.enumerate_products("https://shop.example.com")

        assert isinstance(urls, list)
        assert len(urls) > 0
        assert all(isinstance(url, str) for url in urls)

    def test_enumerate_products_excludes_en_paths(
        self,
        sitemap_index_xml: bytes,
        sitemap_products_1_xml: bytes,
        sitemap_products_2_xml: bytes,
    ):
        """Test that /en/ paths are excluded from results."""
        mock_client = MagicMock(spec=HTTPClient)

        def get_side_effect(url: str):
            if url.endswith("/sitemap.xml"):
                return MockResponse(sitemap_index_xml)
            elif "products_1" in url:
                return MockResponse(sitemap_products_1_xml)
            elif "products_2" in url:
                return MockResponse(sitemap_products_2_xml)
            else:
                raise Exception(f"Unexpected URL: {url}")

        mock_client.get.side_effect = get_side_effect

        enumerator = SitemapEnumerator(http_client=mock_client)
        urls = enumerator.enumerate_products("https://shop.example.com")

        assert "/en/" not in "\n".join(urls)
        assert all("/en/" not in url for url in urls)

    def test_enumerate_products_returns_sorted(
        self,
        sitemap_index_xml: bytes,
        sitemap_products_1_xml: bytes,
        sitemap_products_2_xml: bytes,
    ):
        """Test that results are sorted for stable output."""
        mock_client = MagicMock(spec=HTTPClient)

        def get_side_effect(url: str):
            if url.endswith("/sitemap.xml"):
                return MockResponse(sitemap_index_xml)
            elif "products_1" in url:
                return MockResponse(sitemap_products_1_xml)
            elif "products_2" in url:
                return MockResponse(sitemap_products_2_xml)
            else:
                raise Exception(f"Unexpected URL: {url}")

        mock_client.get.side_effect = get_side_effect

        enumerator = SitemapEnumerator(http_client=mock_client)
        urls = enumerator.enumerate_products("https://shop.example.com")

        assert urls == sorted(urls)

    def test_enumerate_products_deduplicates(
        self,
        sitemap_index_xml: bytes,
        sitemap_products_1_xml: bytes,
        sitemap_products_2_xml: bytes,
    ):
        """Test that duplicate URLs are removed."""
        mock_client = MagicMock(spec=HTTPClient)

        def get_side_effect(url: str):
            if url.endswith("/sitemap.xml"):
                return MockResponse(sitemap_index_xml)
            elif "products_1" in url:
                return MockResponse(sitemap_products_1_xml)
            elif "products_2" in url:
                return MockResponse(sitemap_products_2_xml)
            else:
                raise Exception(f"Unexpected URL: {url}")

        mock_client.get.side_effect = get_side_effect

        enumerator = SitemapEnumerator(http_client=mock_client)
        urls = enumerator.enumerate_products("https://shop.example.com")

        assert len(urls) == len(set(urls))

    def test_enumerate_products_includes_query_params(
        self,
        sitemap_index_xml: bytes,
        sitemap_products_1_xml: bytes,
        sitemap_products_2_xml: bytes,
    ):
        """Test that URLs with query parameters are included."""
        mock_client = MagicMock(spec=HTTPClient)

        def get_side_effect(url: str):
            if url.endswith("/sitemap.xml"):
                return MockResponse(sitemap_index_xml)
            elif "products_1" in url:
                return MockResponse(sitemap_products_1_xml)
            elif "products_2" in url:
                return MockResponse(sitemap_products_2_xml)
            else:
                raise Exception(f"Unexpected URL: {url}")

        mock_client.get.side_effect = get_side_effect

        enumerator = SitemapEnumerator(http_client=mock_client)
        urls = enumerator.enumerate_products("https://shop.example.com")

        assert any("variant=" in url for url in urls)

    def test_enumerate_products_skips_non_products_sitemaps(
        self,
        sitemap_index_xml: bytes,
        sitemap_products_1_xml: bytes,
        sitemap_products_2_xml: bytes,
    ):
        """Test that pages/collections sitemaps are not fetched."""
        mock_client = MagicMock(spec=HTTPClient)
        call_urls = []

        def get_side_effect(url: str):
            call_urls.append(url)
            if url.endswith("/sitemap.xml"):
                return MockResponse(sitemap_index_xml)
            elif "products_1" in url:
                return MockResponse(sitemap_products_1_xml)
            elif "products_2" in url:
                return MockResponse(sitemap_products_2_xml)
            else:
                raise Exception(f"Unexpected URL: {url}")

        mock_client.get.side_effect = get_side_effect

        enumerator = SitemapEnumerator(http_client=mock_client)
        urls = enumerator.enumerate_products("https://shop.example.com")

        assert any("products" in url for url in call_urls)
        assert not any("pages_1" in url for url in call_urls)
        assert not any("collections_1" in url for url in call_urls)


class TestSitemapEnumeratorErrorHandling:
    """Test error handling in sitemap enumeration."""

    def test_parse_error_on_malformed_index(self):
        """Test error handling for malformed sitemapindex."""
        mock_client = MagicMock(spec=HTTPClient)
        mock_client.get.return_value = MockResponse(b"<not-valid-xml>")

        enumerator = SitemapEnumerator(http_client=mock_client)

        with pytest.raises(SitemapError):
            enumerator.enumerate_products("https://shop.example.com")

    def test_parse_error_on_malformed_products_sitemap(self, sitemap_index_xml: bytes):
        """Test error handling for malformed products sitemap."""
        mock_client = MagicMock(spec=HTTPClient)

        def get_side_effect(url: str):
            if url.endswith("/sitemap.xml"):
                return MockResponse(sitemap_index_xml)
            else:
                return MockResponse(b"<not-closed-xml>")

        mock_client.get.side_effect = get_side_effect

        enumerator = SitemapEnumerator(http_client=mock_client)

        with pytest.raises(SitemapParseError):
            enumerator.enumerate_products("https://shop.example.com")

    def test_http_error_on_fetch_failure(self):
        """Test error handling for HTTP fetch failures."""
        mock_client = MagicMock(spec=HTTPClient)
        mock_client.get.side_effect = HTTPClientError("Connection failed")

        enumerator = SitemapEnumerator(http_client=mock_client)

        with pytest.raises(SitemapError):
            enumerator.enumerate_products("https://shop.example.com")

    def test_empty_sitemapindex_returns_empty_list(self):
        """Test handling of empty sitemapindex."""
        empty_index = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
</sitemapindex>
"""
        mock_client = MagicMock(spec=HTTPClient)
        mock_client.get.return_value = MockResponse(empty_index)

        enumerator = SitemapEnumerator(http_client=mock_client)
        urls = enumerator.enumerate_products("https://shop.example.com")

        assert urls == []

    def test_sitemapindex_without_products_returns_empty_list(self):
        """Test handling of sitemapindex with no products sitemaps."""
        no_products_index = b"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap>
    <loc>https://shop.example.com/sitemap_pages_1.xml</loc>
  </sitemap>
</sitemapindex>
"""
        mock_client = MagicMock(spec=HTTPClient)
        mock_client.get.return_value = MockResponse(no_products_index)

        enumerator = SitemapEnumerator(http_client=mock_client)
        urls = enumerator.enumerate_products("https://shop.example.com")

        assert urls == []


class TestSitemapEnumeratorRealFixtures:
    """Tests using the actual fixture files from disk."""

    def test_enumerate_with_real_fixtures(
        self,
        sitemap_index_xml: bytes,
        sitemap_products_1_xml: bytes,
        sitemap_products_2_xml: bytes,
    ):
        """Full integration test with real fixtures."""
        mock_client = MagicMock(spec=HTTPClient)

        def get_side_effect(url: str):
            if url.endswith("/sitemap.xml"):
                return MockResponse(sitemap_index_xml)
            elif "products_1" in url:
                return MockResponse(sitemap_products_1_xml)
            elif "products_2" in url:
                return MockResponse(sitemap_products_2_xml)
            else:
                raise Exception(f"Unexpected URL: {url}")

        mock_client.get.side_effect = get_side_effect

        enumerator = SitemapEnumerator(http_client=mock_client)
        urls = enumerator.enumerate_products("https://shop.example.com")

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
