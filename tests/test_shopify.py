# pyright: reportMissingImports=false
# pyright: reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false
# pyright: reportUnknownArgumentType=false
# pyright: reportUnknownParameterType=false
# pyright: reportMissingParameterType=false

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from estimator_king.crawler.shopify import (
    ShopifyHTTPError,
    ShopifyJSONError,
    fetch_product,
)
from estimator_king.crawler.snapshot import compute_content_hash


class _Resp:
    status_code: int
    text: str

    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


def _read_fixture(name: str) -> str:
    return (Path(__file__).parent / "fixtures" / name).read_text(encoding="utf-8")


def _mk_client(
    *, json_text: str, html_text: str, json_status: int = 200, html_status: int = 200
):
    client = Mock()

    def fake_get(url: str, **_kwargs: object):
        if url.endswith(".json"):
            return _Resp(json_status, json_text)
        return _Resp(html_status, html_text)

    client.get = Mock(side_effect=fake_get)
    return client


def test_fetch_product_success_hololive_extracts_details_and_hash():
    json_text = _read_fixture("product_json_hololive.json")
    html_text = _read_fixture("product_html_hololive_basic.html")
    client = _mk_client(json_text=json_text, html_text=html_text)

    url = "https://shop.hololivepro.com/products/sample"
    snapshot = fetch_product(url, client)

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
    snapshot = fetch_product(url, client)

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

    snapshot = fetch_product("https://shop.hololivepro.com/products/x", client)
    assert snapshot.html_details == {}


@pytest.mark.parametrize("status", [404, 500])
def test_fetch_product_http_error_raises_shopify_http_error(status: int):
    json_text = _read_fixture("product_json_hololive.json")
    html_text = _read_fixture("product_html_hololive_basic.html")
    client = _mk_client(json_text=json_text, html_text=html_text, json_status=status)

    with pytest.raises(ShopifyHTTPError):
        _ = fetch_product("https://shop.hololivepro.com/products/x", client)


def test_fetch_product_html_http_error_raises_shopify_http_error():
    json_text = _read_fixture("product_json_hololive.json")
    html_text = _read_fixture("product_html_hololive_basic.html")
    client = _mk_client(json_text=json_text, html_text=html_text, html_status=500)

    with pytest.raises(ShopifyHTTPError):
        _ = fetch_product("https://shop.hololivepro.com/products/x", client)


def test_fetch_product_malformed_json_raises_shopify_json_error():
    html_text = _read_fixture("product_html_hololive_basic.html")
    client = _mk_client(json_text="{not json", html_text=html_text)

    with pytest.raises(ShopifyJSONError):
        _ = fetch_product("https://shop.hololivepro.com/products/x", client)


def test_fetch_product_missing_product_object_raises_shopify_json_error():
    html_text = _read_fixture("product_html_hololive_basic.html")
    client = _mk_client(json_text=json.dumps({"nope": {}}), html_text=html_text)

    with pytest.raises(ShopifyJSONError):
        _ = fetch_product("https://shop.hololivepro.com/products/x", client)


def test_fetch_product_accepts_url_with_json_suffix():
    json_text = _read_fixture("product_json_hololive.json")
    html_text = _read_fixture("product_html_hololive_basic.html")
    client = _mk_client(json_text=json_text, html_text=html_text)

    snapshot = fetch_product("https://shop.hololivepro.com/products/x.json", client)
    assert snapshot.product_id == 1000000001


def test_fetch_product_empty_url_raises_value_error():
    client = _mk_client(json_text="{}", html_text="")
    with pytest.raises(ValueError):
        _ = fetch_product("   ", client)


def test_fetch_product_json_root_not_object_raises_shopify_json_error():
    html_text = _read_fixture("product_html_hololive_basic.html")
    client = _mk_client(json_text=json.dumps([1, 2, 3]), html_text=html_text)
    with pytest.raises(ShopifyJSONError):
        _ = fetch_product("https://shop.hololivepro.com/products/x", client)


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
        _ = fetch_product("https://shop.hololivepro.com/products/x", client)


def test_fetch_product_allows_null_body_html_and_variants():
    html_text = _read_fixture("product_html_none.html")
    product = {"id": 123, "title": "X", "body_html": None, "variants": None}
    client = _mk_client(json_text=json.dumps({"product": product}), html_text=html_text)

    snapshot = fetch_product("https://shop.hololivepro.com/products/x", client)
    assert snapshot.description == ""
    assert snapshot.variants == []
    assert snapshot.html_details == {}
    assert getattr(snapshot, "content_hash") == compute_content_hash(snapshot)
