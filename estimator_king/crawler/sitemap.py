"""Sitemap parsing for Shopify stores."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import List, Optional, Set
from urllib.parse import urljoin

from estimator_king.crawler.http_client import HTTPClient, HTTPClientError


# XML Namespace for sitemaps (standard)
SITEMAP_NS = {"sitemap": "http://www.sitemaps.org/schemas/sitemap/0.9"}


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

    def __init__(self, http_client: Optional[HTTPClient] = None):
        """Initialize enumerator with optional HTTP client."""
        self.http_client = http_client or HTTPClient()

    def enumerate_products(self, base_url: str) -> List[str]:
        """Enumerate all product URLs from a Shopify store.

        Args:
            base_url: Store base URL (e.g., "https://shop.example.com")

        Returns:
            Sorted, deduplicated list of product URLs (excluding /en/ paths)

        Raises:
            SitemapError: If sitemap parsing or fetching fails
            HttpClientError: If HTTP requests fail
        """
        sitemap_index_url = urljoin(base_url, "/sitemap.xml")

        try:
            # Fetch and parse sitemapindex
            products_sitemap_urls = self._extract_products_sitemaps(sitemap_index_url)

            # Collect all product URLs
            all_product_urls: Set[str] = set()
            for sitemap_url in products_sitemap_urls:
                urls = self._extract_product_urls(sitemap_url)
                all_product_urls.update(urls)

            # Filter out /en/ paths and return sorted
            filtered = [url for url in all_product_urls if "/products/" in url and "/en/" not in url]
            return sorted(filtered)

        except (ET.ParseError, HTTPClientError) as e:
            raise SitemapError(
                f"Failed to enumerate products from {base_url}: {e}"
            ) from e

    def _extract_products_sitemaps(self, sitemap_index_url: str) -> List[str]:
        """Extract all products sitemap URLs from sitemapindex.

        Args:
            sitemap_index_url: URL to /sitemap.xml (sitemapindex)

        Returns:
            List of products sitemap URLs (filtered to only "products" ones)

        Raises:
            SitemapParseError: If XML parsing fails
            HttpClientError: If HTTP request fails
        """
        try:
            resp = self.http_client.get(sitemap_index_url)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except ET.ParseError as e:
            raise SitemapParseError(f"Failed to parse sitemapindex: {e}") from e
        except HTTPClientError as e:
            raise SitemapParseError(f"Failed to fetch sitemapindex: {e}") from e

        products_urls: List[str] = []

        # Find all <sitemap> entries
        for sitemap_elem in root.findall("sitemap:sitemap", SITEMAP_NS):
            loc_elem = sitemap_elem.find("sitemap:loc", SITEMAP_NS)
            if loc_elem is not None and loc_elem.text:
                url = loc_elem.text.strip()
                # Only include sitemaps with "products" in the URL
                if "products" in url:
                    products_urls.append(url)

        return products_urls

    def _extract_product_urls(self, sitemap_url: str) -> List[str]:
        """Extract all product URLs from a products sitemap.

        Args:
            sitemap_url: URL to a sitemap_products_*.xml file

        Returns:
            List of product URLs from this sitemap

        Raises:
            SitemapParseError: If XML parsing fails
            HttpClientError: If HTTP request fails
        """
        try:
            resp = self.http_client.get(sitemap_url)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
        except ET.ParseError as e:
            raise SitemapParseError(
                f"Failed to parse sitemap {sitemap_url}: {e}"
            ) from e
        except HTTPClientError as e:
            raise SitemapParseError(
                f"Failed to fetch sitemap {sitemap_url}: {e}"
            ) from e

        product_urls: List[str] = []

        # Find all <url><loc> entries
        for url_elem in root.findall("sitemap:url", SITEMAP_NS):
            loc_elem = url_elem.find("sitemap:loc", SITEMAP_NS)
            if loc_elem is not None and loc_elem.text:
                url = loc_elem.text.strip()
                product_urls.append(url)

        return product_urls
