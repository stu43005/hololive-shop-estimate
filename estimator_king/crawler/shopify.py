from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol, cast

from .html_extractor import extract_detail_sections as extract_html_details
from .snapshot import ProductSnapshot, ProductVariant, compute_content_hash

logger = logging.getLogger(__name__)


class _HTTPResponse(Protocol):
    status_code: int
    text: str


class _HTTPGetter(Protocol):
    def get(self, url: str) -> _HTTPResponse: ...


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


class ShopifyProductError(Exception):
    pass


class ShopifyHTTPError(ShopifyProductError):
    url: str
    status_code: int

    def __init__(self, url: str, status_code: int):
        super().__init__(f"shopify http error: {status_code} {url}")
        self.url = url
        self.status_code = status_code


class ShopifyJSONError(ShopifyProductError):
    pass


def _raise_for_status(url: str, resp: _HTTPResponse) -> None:
    status = int(getattr(resp, "status_code", 0) or 0)
    if status < 200 or status >= 300:
        raise ShopifyHTTPError(url, status_code=status)


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
    content_hash: str


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
    )


def fetch_product(url: str, http_client: _HTTPGetter) -> ProductSnapshot:
    canonical_url = url.strip()
    if not canonical_url:
        raise ValueError("url must be a non-empty string")
    if canonical_url.endswith(".json"):
        canonical_url = canonical_url[: -len(".json")]
    canonical_url = canonical_url.rstrip("/")
    json_url = canonical_url + ".json"

    json_resp = http_client.get(json_url)
    _raise_for_status(json_url, json_resp)

    try:
        json_text = cast(str, getattr(json_resp, "text", ""))
        payload = cast(object, json.loads(json_text))
    except Exception as e:  # noqa: BLE001
        raise ShopifyJSONError(f"invalid shopify json: {e}") from e

    product = _parse_product_json(payload)

    html_resp = http_client.get(canonical_url)
    _raise_for_status(canonical_url, html_resp)
    html_text = cast(str, getattr(html_resp, "text", ""))

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
        content_hash=content_hash,
    )
