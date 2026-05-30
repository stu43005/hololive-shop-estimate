"""Sitemap parsing for Shopify stores."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlparse

from estimator_king.crawler.async_http_client import AsyncHTTPClient, AsyncHTTPClientError


# XML Namespace for sitemaps (standard)
SITEMAP_NS = {"sitemap": "http://www.sitemaps.org/schemas/sitemap/0.9"}

DEFAULT_LOCALE = "default"


def locale_of_url(url: str) -> str:
    """Return the locale segment of a Shopify store URL, or DEFAULT_LOCALE.

    Multi-locale Shopify stores prefix localized paths with a locale segment,
    e.g. ``/en/products/x`` or ``/ja-al/sitemap_products_1.xml``. Default-locale
    paths start directly with a structural segment (``products`` or
    ``sitemap_...``). The first path segment is therefore the locale unless it is
    one of those structural segments. The result is lowercased.
    """
    path = urlparse(url).path.lstrip("/")
    first = path.split("/", 1)[0].lower()
    if first == "products" or first.startswith("sitemap"):
        return DEFAULT_LOCALE
    return first


class SitemapError(Exception):
    """Base error for sitemap operations."""


class SitemapParseError(SitemapError):
    """Raised when XML parsing fails."""


class SitemapEnumerator:
    """Enumerates product URLs from Shopify sitemap hierarchy.

    Flow:
    1. Fetch /sitemap.xml (sitemapindex)
    2. Extract all <sitemap><loc> entries containing "products"
    3. For each products sitemap, fetch and extract <url><loc> entries
    4. Filter out /en/ locale paths
    5. Return stable-ordered (sorted), deduplicated list
    """

    def __init__(self, http_client: AsyncHTTPClient):
        """Initialize enumerator with an async HTTP client."""
        self.http_client = http_client

    async def enumerate_products(self, base_url: str) -> list[str]:
        """Enumerate all product URLs from a Shopify store.

        Args:
            base_url: Store base URL (e.g., "https://shop.example.com")

        Returns:
            Sorted, deduplicated list of product URLs (excluding /en/ paths)

        Raises:
            SitemapError: If sitemap parsing or fetching fails
        """
        sitemap_index_url = urljoin(base_url, "/sitemap.xml")

        try:
            products_sitemap_urls = await self._extract_products_sitemaps(sitemap_index_url)

            all_product_urls: set[str] = set()
            for sitemap_url in products_sitemap_urls:
                urls = await self._extract_product_urls(sitemap_url)
                all_product_urls.update(urls)

            filtered = [url for url in all_product_urls if "/products/" in url and "/en/" not in url]
            return sorted(filtered)

        except (ET.ParseError, AsyncHTTPClientError) as e:
            raise SitemapError(
                f"Failed to enumerate products from {base_url}: {e}"
            ) from e

    async def _extract_products_sitemaps(self, sitemap_index_url: str) -> list[str]:
        """Extract all products sitemap URLs from sitemapindex."""
        try:
            text = await self.http_client.get(sitemap_index_url)
            root = ET.fromstring(text)
        except ET.ParseError as e:
            raise SitemapParseError(f"Failed to parse sitemapindex: {e}") from e
        except AsyncHTTPClientError as e:
            raise SitemapParseError(f"Failed to fetch sitemapindex: {e}") from e

        products_urls: list[str] = []

        for sitemap_elem in root.findall("sitemap:sitemap", SITEMAP_NS):
            loc_elem = sitemap_elem.find("sitemap:loc", SITEMAP_NS)
            if loc_elem is not None and loc_elem.text:
                url = loc_elem.text.strip()
                if "products" in url:
                    products_urls.append(url)

        return products_urls

    async def _extract_product_urls(self, sitemap_url: str) -> list[str]:
        """Extract all product URLs from a products sitemap."""
        try:
            text = await self.http_client.get(sitemap_url)
            root = ET.fromstring(text)
        except ET.ParseError as e:
            raise SitemapParseError(
                f"Failed to parse sitemap {sitemap_url}: {e}"
            ) from e
        except AsyncHTTPClientError as e:
            raise SitemapParseError(
                f"Failed to fetch sitemap {sitemap_url}: {e}"
            ) from e

        product_urls: list[str] = []

        for url_elem in root.findall("sitemap:url", SITEMAP_NS):
            loc_elem = url_elem.find("sitemap:loc", SITEMAP_NS)
            if loc_elem is not None and loc_elem.text:
                url = loc_elem.text.strip()
                product_urls.append(url)

        return product_urls
