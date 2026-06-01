# pyright: reportMissingImports=false
# pyright: reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false
# pyright: reportUnknownArgumentType=false
# pyright: reportUnknownParameterType=false
# pyright: reportMissingParameterType=false

import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from estimator_king.crawler.async_http_client import ClientError, ServerError
from estimator_king.crawler.shopify import (
    ShopifyJSONError,
    _build_snapshot,
    ProductSnapshotWithHash,
    fetch_product,
)
from estimator_king.crawler.snapshot import compute_content_hash


class _FakeAsyncClient:
    def __init__(self, *, json_text, html_text, json_exc=None, html_exc=None):
        self._json_text = json_text
        self._html_text = html_text
        self._json_exc = json_exc
        self._html_exc = html_exc

    async def get(self, url: str) -> str:
        if urlsplit(url).path.endswith(".json"):
            if self._json_exc is not None:
                raise self._json_exc
            return self._json_text
        if self._html_exc is not None:
            raise self._html_exc
        return self._html_text


def _read_fixture(name: str) -> str:
    return (Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8")


def _mk_client(*, json_text, html_text, json_exc=None, html_exc=None):
    return _FakeAsyncClient(
        json_text=json_text, html_text=html_text, json_exc=json_exc, html_exc=html_exc
    )


def test_fetch_product_success_hololive_extracts_details_and_hash():
    json_text = _read_fixture("product_json_hololive.json")
    html_text = _read_fixture("product_html_hololive_basic.html")
    client = _mk_client(json_text=json_text, html_text=html_text)

    url = "https://shop.hololivepro.com/products/sample"
    snapshot = asyncio.run(fetch_product(url, client))

    assert snapshot.product_id == 1000000001
    assert snapshot.title == "Hololive Sample Product"
    assert "これは説明" in snapshot.description
    assert "<p>" not in snapshot.description, f"HTML tag found: {snapshot.description!r}"
    assert "<br>" not in snapshot.description, f"HTML tag found: {snapshot.description!r}"
    assert len(snapshot.variants) == 2
    assert "セット詳細" in snapshot.html_details
    assert "グッズ詳細" in snapshot.html_details

    expected_hash = compute_content_hash(snapshot)
    assert getattr(snapshot, "content_hash") == expected_hash
    assert len(expected_hash) == 64


def test_fetch_product_success_vspo_extracts_english_details_and_hash():
    json_text = _read_fixture("product_json_vspo.json")
    html_text = _read_fixture("product_html_vspo_basic.html")
    client = _mk_client(json_text=json_text, html_text=html_text)

    url = "https://store.vspo.jp/products/sample"
    snapshot = asyncio.run(fetch_product(url, client))

    assert snapshot.product_id == 1000000002
    assert snapshot.title == "VSPO Sample Product"
    assert len(snapshot.variants) == 1
    assert "Set Details" in snapshot.html_details
    assert "Merch details" in snapshot.html_details
    assert getattr(snapshot, "content_hash") == compute_content_hash(snapshot)


def test_fetch_product_no_detail_sections_returns_empty_dict():
    json_text = _read_fixture("product_json_hololive.json")
    html_text = _read_fixture("product_html_none.html")
    client = _mk_client(json_text=json_text, html_text=html_text)

    snapshot = asyncio.run(fetch_product("https://shop.hololivepro.com/products/x", client))
    assert snapshot.html_details == {}


@pytest.mark.parametrize("status,exc_type", [(404, ClientError), (500, ServerError)])
def test_fetch_product_http_error_propagates(status, exc_type):
    json_text = _read_fixture("product_json_hololive.json")
    html_text = _read_fixture("product_html_hololive_basic.html")
    url = "https://shop.hololivepro.com/products/x"
    client = _mk_client(
        json_text=json_text, html_text=html_text,
        json_exc=exc_type(url + ".json", status_code=status),
    )

    with pytest.raises(exc_type):
        _ = asyncio.run(fetch_product(url, client))


def test_fetch_product_html_http_error_propagates():
    json_text = _read_fixture("product_json_hololive.json")
    html_text = _read_fixture("product_html_hololive_basic.html")
    url = "https://shop.hololivepro.com/products/x"
    client = _mk_client(
        json_text=json_text, html_text=html_text,
        html_exc=ServerError(url, status_code=500),
    )

    with pytest.raises(ServerError):
        _ = asyncio.run(fetch_product(url, client))


def test_fetch_product_malformed_json_raises_shopify_json_error():
    html_text = _read_fixture("product_html_hololive_basic.html")
    client = _mk_client(json_text="{not json", html_text=html_text)

    with pytest.raises(ShopifyJSONError):
        _ = asyncio.run(fetch_product("https://shop.hololivepro.com/products/x", client))


def test_fetch_product_missing_product_object_raises_shopify_json_error():
    html_text = _read_fixture("product_html_hololive_basic.html")
    client = _mk_client(json_text=json.dumps({"nope": {}}), html_text=html_text)

    with pytest.raises(ShopifyJSONError):
        _ = asyncio.run(fetch_product("https://shop.hololivepro.com/products/x", client))


def test_fetch_product_accepts_url_with_json_suffix():
    json_text = _read_fixture("product_json_hololive.json")
    html_text = _read_fixture("product_html_hololive_basic.html")
    client = _mk_client(json_text=json_text, html_text=html_text)

    snapshot = asyncio.run(
        fetch_product("https://shop.hololivepro.com/products/x.json", client)
    )
    assert snapshot.product_id == 1000000001


def test_fetch_product_empty_url_raises_value_error():
    client = _mk_client(json_text="{}", html_text="")
    with pytest.raises(ValueError):
        _ = asyncio.run(fetch_product("   ", client))


def test_fetch_product_json_root_not_object_raises_shopify_json_error():
    html_text = _read_fixture("product_html_hololive_basic.html")
    client = _mk_client(json_text=json.dumps([1, 2, 3]), html_text=html_text)
    with pytest.raises(ShopifyJSONError):
        _ = asyncio.run(fetch_product("https://shop.hololivepro.com/products/x", client))


@pytest.mark.parametrize(
    "product_patch",
    [
        {"id": "not-int"},
        {"id": 123, "title": None},
        {"id": 123, "title": "X", "body_html": 42},
        {"id": 123, "title": "X", "variants": {"not": "a list"}},
        {"id": 123, "title": "X", "variants": ["nope"]},
        {
            "id": 123,
            "title": "X",
            "variants": [{"id": "bad", "title": "T", "price": "1"}],
        },
        {"id": 123, "title": "X", "variants": [{"id": 1, "title": None, "price": "1"}]},
        {"id": 123, "title": "X", "variants": [{"id": 1, "title": "T", "price": 1}]},
        {
            "id": 123,
            "title": "X",
            "variants": [{"id": 1, "title": "T", "price": "1", "sku": 9}],
        },
    ],
)
def test_fetch_product_json_validation_errors_raise_shopify_json_error(
    product_patch: dict[str, object],
):
    html_text = _read_fixture("product_html_hololive_basic.html")
    base: dict[str, object] = {"id": 123, "title": "X", "body_html": "", "variants": []}
    merged = dict(base)
    merged.update(product_patch)
    client = _mk_client(
        json_text=json.dumps({"product": merged}),
        html_text=html_text,
    )
    with pytest.raises(ShopifyJSONError):
        _ = asyncio.run(fetch_product("https://shop.hololivepro.com/products/x", client))


def test_fetch_product_allows_null_body_html_and_variants():
    html_text = _read_fixture("product_html_none.html")
    product = {"id": 123, "title": "X", "body_html": None, "variants": None}
    client = _mk_client(json_text=json.dumps({"product": product}), html_text=html_text)

    snapshot = asyncio.run(fetch_product("https://shop.hololivepro.com/products/x", client))
    assert snapshot.description == ""
    assert snapshot.variants == []
    assert snapshot.html_details == {}
    assert getattr(snapshot, "content_hash") == compute_content_hash(snapshot)


def _product_json(**extra) -> str:
    product = {
        "id": 123,
        "title": "Test Product",
        "body_html": "<p>desc</p>",
        "variants": [
            {
                "id": 1,
                "title": "グッズ / Item A",
                "price": "500",
                "sku": None,
                "price_currency": "JPY",
            }
        ],
    }
    product.update(extra)
    return json.dumps({"product": product})


def test_published_at_parsed_to_epoch():
    snap = _build_snapshot(
        _product_json(published_at="2023-06-30T19:00:07+09:00"), "<html></html>", "http://x/products/123"
    )
    # 2023-06-30T19:00:07+09:00 == 2023-06-30T10:00:07Z == 1688119207
    assert snap.published_at == 1688119207


def test_published_at_falls_back_to_created_at_then_zero():
    snap_created = _build_snapshot(
        _product_json(created_at="2023-06-30T19:00:07+09:00"), "<html></html>", "http://x/products/123"
    )
    assert snap_created.published_at == 1688119207
    snap_none = _build_snapshot(_product_json(), "<html></html>", "http://x/products/123")
    assert snap_none.published_at == 0


def test_snapshot_with_hash_constructs_with_published_at():
    obj = ProductSnapshotWithHash(
        product_id=1, title="t", description="d", variants=[], html_details={},
        published_at=42, content_hash="abc",
    )
    assert obj.published_at == 42 and obj.content_hash == "abc"


@pytest.mark.parametrize(
    "currency_value",
    ["USD", "EUR", 123, None],  # wrong currency, non-str, and missing (None)
)
def test_fetch_product_rejects_non_jpy_price_currency(currency_value):
    html_text = _read_fixture("product_html_hololive_basic.html")
    variant: dict[str, object] = {"id": 1, "title": "T", "price": "1000", "sku": None}
    if currency_value is not None:
        variant["price_currency"] = currency_value
    product = {"id": 123, "title": "X", "body_html": "", "variants": [variant]}
    client = _mk_client(
        json_text=json.dumps({"product": product}), html_text=html_text
    )
    with pytest.raises(ShopifyJSONError):
        _ = asyncio.run(fetch_product("https://shop.hololivepro.com/products/x", client))


def test_fetch_product_accepts_jpy_price_currency():
    html_text = _read_fixture("product_html_none.html")
    variant = {
        "id": 1,
        "title": "T",
        "price": "1000",
        "sku": None,
        "price_currency": "JPY",
    }
    product = {"id": 123, "title": "X", "body_html": "", "variants": [variant]}
    client = _mk_client(
        json_text=json.dumps({"product": product}), html_text=html_text
    )
    snapshot = asyncio.run(fetch_product("https://shop.hololivepro.com/products/x", client))
    assert len(snapshot.variants) == 1
    assert snapshot.variants[0].price == "1000"


class _RecordingAsyncClient:
    def __init__(self, *, json_text: str, html_text: str):
        self._json_text = json_text
        self._html_text = html_text
        self.requested_urls: list[str] = []

    async def get(self, url: str) -> str:
        self.requested_urls.append(url)
        if urlsplit(url).path.endswith(".json"):
            return self._json_text
        return self._html_text


def test_fetch_product_forces_jpy_currency_on_json_url():
    json_text = _read_fixture("product_json_hololive.json")
    html_text = _read_fixture("product_html_hololive_basic.html")
    client = _RecordingAsyncClient(json_text=json_text, html_text=html_text)

    asyncio.run(fetch_product("https://shop.hololivepro.com/products/sample", client))

    # fetch_product issues exactly two GETs in a fixed order: json_url then canonical_url.
    # Assert exact equality (not membership) so a malformed/double-query URL or any stray
    # request would fail the test.
    assert client.requested_urls == [
        "https://shop.hololivepro.com/products/sample.json?currency=JPY",
        "https://shop.hololivepro.com/products/sample",
    ]


def test_fetch_product_strips_existing_query_before_forcing_currency():
    json_text = _read_fixture("product_json_hololive.json")
    html_text = _read_fixture("product_html_hololive_basic.html")
    client = _RecordingAsyncClient(json_text=json_text, html_text=html_text)

    asyncio.run(
        fetch_product("https://shop.hololivepro.com/products/sample.json?foo=bar", client)
    )

    # The pre-existing ?foo=bar must be stripped before appending ?currency=JPY; assert
    # exact equality to guarantee no malformed double-query URL slips through.
    assert client.requested_urls == [
        "https://shop.hololivepro.com/products/sample.json?currency=JPY",
        "https://shop.hololivepro.com/products/sample",
    ]
