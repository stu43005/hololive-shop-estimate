from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, cast
from urllib.parse import urlsplit, urlunsplit

from .html_extractor import extract_detail_sections as extract_html_details
from .snapshot import ProductSnapshot, ProductVariant, compute_content_hash

logger = logging.getLogger(__name__)

# Shopify Markets returns the JSON `.json` endpoint's prices in a geo/locale-detected
# currency. Pin every product fetch to JPY via the `currency` query param, which
# overrides cookie/geo detection. The system is JPY-only (ProductItem.price_jpy).
_FORCE_CURRENCY = "JPY"


class _AsyncGetter(Protocol):
    async def get(self, url: str) -> str: ...


def _clean_body_html(html: str) -> str:
    """Convert Shopify body_html to clean Markdown text, stripping HTML tags."""
    if not html or not html.strip():
        return ""
    from bs4 import BeautifulSoup
    import markdownify as md
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["img", "style", "script"]):
        tag.decompose()
    result = md.markdownify(str(soup), heading_style="ATX")
    return result.strip()


def _parse_published_at(product: dict[str, object]) -> int:
    """Epoch seconds from product.published_at, falling back to created_at, else 0."""
    for key in ("published_at", "created_at"):
        raw = product.get(key)
        if isinstance(raw, str) and raw.strip():
            try:
                return int(datetime.fromisoformat(raw).timestamp())
            except ValueError:
                continue
    return 0


class ShopifyProductError(Exception):
    pass


class ShopifyJSONError(ShopifyProductError):
    pass


def _parse_product_json(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ShopifyJSONError("shopify json root must be an object")
    payload_obj = cast(dict[str, object], payload)
    product_obj = payload_obj.get("product")
    if not isinstance(product_obj, dict):
        raise ShopifyJSONError("shopify json missing 'product' object")
    return cast(dict[str, object], product_obj)


@dataclass
class ProductSnapshotWithHash(ProductSnapshot):
    content_hash: str = ""  # always set explicitly at construction; default satisfies dataclass field-ordering rule


def _build_snapshot_from_product_json(
    product: dict[str, object], *, html_details: dict[str, str]
) -> ProductSnapshot:
    product_id = product.get("id")
    if not isinstance(product_id, int):
        raise ShopifyJSONError("shopify product.id missing or not int")

    title = product.get("title")
    if not isinstance(title, str):
        raise ShopifyJSONError("shopify product.title missing or not str")

    description = product.get("body_html")
    if description is None:
        description = ""
    if not isinstance(description, str):
        raise ShopifyJSONError("shopify product.body_html must be str")
    description = _clean_body_html(description)

    variants_obj = product.get("variants")
    variants_raw: list[object]
    if variants_obj is None:
        variants_raw = []
    elif isinstance(variants_obj, list):
        variants_raw = cast(list[object], variants_obj)
    else:
        raise ShopifyJSONError("shopify product.variants must be a list")

    variants: list[ProductVariant] = []
    for v in variants_raw:
        if not isinstance(v, dict):
            raise ShopifyJSONError("shopify variant must be an object")
        v_obj = cast(dict[str, object], v)
        variant_id = v_obj.get("id")
        v_title = v_obj.get("title")
        price = v_obj.get("price")
        sku = v_obj.get("sku")
        if not isinstance(variant_id, int):
            raise ShopifyJSONError("shopify variant.id missing or not int")
        if not isinstance(v_title, str):
            raise ShopifyJSONError("shopify variant.title missing or not str")
        if not isinstance(price, str):
            raise ShopifyJSONError("shopify variant.price missing or not str")
        price_currency = v_obj.get("price_currency")
        if price_currency != _FORCE_CURRENCY:
            raise ShopifyJSONError(
                f"shopify variant.price_currency expected {_FORCE_CURRENCY!r}, "
                f"got {price_currency!r}"
            )
        if sku is not None and not isinstance(sku, str):
            raise ShopifyJSONError("shopify variant.sku must be str or null")
        variants.append(
            ProductVariant(
                variant_id=variant_id,
                title=v_title,
                price=price,
                sku=sku,
            )
        )

    return ProductSnapshot(
        product_id=product_id,
        title=title,
        description=description,
        variants=variants,
        html_details=html_details,
        published_at=_parse_published_at(product),
    )


async def fetch_product(url: str, client: _AsyncGetter) -> ProductSnapshot:
    raw = url.strip()
    if not raw:
        raise ValueError("url must be a non-empty string")
    parts = urlsplit(raw)
    path = parts.path
    if path.endswith(".json"):
        path = path[: -len(".json")]
    path = path.rstrip("/")
    # Drop any existing query/fragment so we never build a malformed double-query URL.
    canonical_url = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    json_url = f"{canonical_url}.json?currency={_FORCE_CURRENCY}"

    json_text = await client.get(json_url)
    html_text = await client.get(canonical_url)
    return await asyncio.to_thread(_build_snapshot, json_text, html_text, canonical_url)


def _build_snapshot(json_text: str, html_text: str, canonical_url: str) -> ProductSnapshot:
    try:
        payload = cast(object, json.loads(json_text))
    except Exception as e:  # noqa: BLE001
        raise ShopifyJSONError(f"invalid shopify json: {e}") from e

    product = _parse_product_json(payload)

    html_details = extract_html_details(html_text)
    logger.debug(f"Extracted html_details for {canonical_url}: {html_details}")
    if html_details:
        for key, value in html_details.items():
            logger.debug(f"  {key}: {value[:50] if len(value) > 50 else value}")
    snapshot = _build_snapshot_from_product_json(product, html_details=html_details)
    content_hash = compute_content_hash(snapshot)
    logger.debug(f"Product {snapshot.product_id} hash: {content_hash[:8]}...")
    return ProductSnapshotWithHash(
        product_id=snapshot.product_id,
        title=snapshot.title,
        description=snapshot.description,
        variants=snapshot.variants,
        html_details=snapshot.html_details,
        published_at=snapshot.published_at,
        content_hash=content_hash,
    )
