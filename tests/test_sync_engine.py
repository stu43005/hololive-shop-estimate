"""Tests for sync engine document formatting and indexing status polling."""

# pyright: reportMissingImports=false

import pytest
from unittest.mock import MagicMock, Mock, patch

from estimator_king.crawler.snapshot import ProductSnapshot, ProductVariant
from estimator_king.database.repository import ProductStateRepository
from estimator_king.sync.dify_client import DifyKBClient
from estimator_king.sync.engine import (
    SyncResult,
    _format_product_document,
    _poll_indexing_status,
    sync_products,
)
from estimator_king.sync.dify_client import DifyRateLimitError, DifyAPIError


@pytest.fixture
def dify_client():
    return Mock()


@pytest.fixture
def state_repo():
    with ProductStateRepository(":memory:") as repo:
        yield repo


class TestPollIndexingStatus:
    """Tests for _poll_indexing_status() polling logic."""

    def test_poll_immediate_complete(self, dify_client):
        """Status 'completed' on first call returns True immediately."""
        dify_client.get_indexing_status.return_value = {
            "data": {"indexing_status": "completed"}
        }

        result = _poll_indexing_status(dify_client, "batch-123", max_wait=60)

        assert result is True
        assert dify_client.get_indexing_status.call_count == 1

    def test_poll_eventual_complete(self, dify_client):
        """Status transitions from indexing to completed after retries."""
        dify_client.get_indexing_status.side_effect = [
            {"data": {"indexing_status": "indexing"}},
            {"data": {"indexing_status": "indexing"}},
            {"data": {"indexing_status": "completed"}},
        ]

        result = _poll_indexing_status(dify_client, "batch-123", max_wait=60)

        assert result is True
        assert dify_client.get_indexing_status.call_count == 3

    def test_poll_immediate_failure(self, dify_client):
        """Status 'failed' on first call returns False immediately."""
        dify_client.get_indexing_status.return_value = {
            "data": {"indexing_status": "failed", "error": "Index error"}
        }

        result = _poll_indexing_status(dify_client, "batch-123", max_wait=60)

        assert result is False
        assert dify_client.get_indexing_status.call_count == 1

    def test_poll_timeout_exceeded(self, dify_client):
        """Never reaches completed, times out and returns False."""
        dify_client.get_indexing_status.return_value = {
            "data": {"indexing_status": "indexing"}
        }

        result = _poll_indexing_status(dify_client, "batch-123", max_wait=6)

        assert result is False
        assert dify_client.get_indexing_status.call_count == 3

    def test_poll_rate_limit_with_backoff(self, dify_client):
        """DifyRateLimitError triggers exponential backoff, then succeeds."""
        dify_client.get_indexing_status.side_effect = [
            DifyRateLimitError("Rate limited (429). Retry after: unknown"),
            DifyRateLimitError("Rate limited (429). Retry after: unknown"),
            {"data": {"indexing_status": "completed"}},
        ]

        result = _poll_indexing_status(dify_client, "batch-123", max_wait=60)

        assert result is True
        assert dify_client.get_indexing_status.call_count == 3

    def test_poll_rate_limit_timeout(self, dify_client):
        """DifyRateLimitError repeatedly, eventually timeout."""
        dify_client.get_indexing_status.side_effect = DifyRateLimitError("Rate limited")

        result = _poll_indexing_status(dify_client, "batch-123", max_wait=10)

        assert result is False
        assert dify_client.get_indexing_status.call_count >= 2

    def test_poll_invalid_batch_id(self, dify_client):
        """Empty batch_id raises ValueError."""
        with pytest.raises(ValueError, match="batch_id cannot be empty"):
            _poll_indexing_status(dify_client, "", max_wait=60)

    def test_poll_invalid_max_wait(self, dify_client):
        """Non-positive max_wait raises ValueError."""
        with pytest.raises(ValueError, match="max_wait must be positive"):
            _poll_indexing_status(dify_client, "batch-123", max_wait=0)

    def test_poll_response_missing_data_key(self, dify_client):
        """Response missing data key continues polling."""
        dify_client.get_indexing_status.side_effect = [
            {},
            {"data": {"indexing_status": "completed"}},
        ]

        result = _poll_indexing_status(dify_client, "batch-123", max_wait=60)

        assert result is True
        assert dify_client.get_indexing_status.call_count == 2

    def test_poll_api_error_propagates(self, dify_client):
        """DifyAPIError (non-rate-limit) propagates immediately."""
        dify_client.get_indexing_status.side_effect = DifyAPIError(
            "API error (500): Server error"
        )

        with pytest.raises(DifyAPIError):
            _poll_indexing_status(dify_client, "batch-123", max_wait=60)


class TestFormatProductDocument:
    """Tests for _format_product_document() function."""

    def test_format_basic(self):
        """Test basic product with single variant and no html_details."""
        snapshot = ProductSnapshot(
            product_id=12345,
            title="Birthday Voice Pack 2025",
            description="Limited edition voice greetings",
            variants=[
                ProductVariant(
                    variant_id=1,
                    title="Standard Edition",
                    price="¥2,000",
                    sku="HLV-2025-STD",
                )
            ],
            html_details={},
        )

        name, text, metadata = _format_product_document(
            snapshot, "hololive", "https://hololive.booth.pm/items/12345"
        )

        assert name == "hololive:12345 - Birthday Voice Pack 2025"
        assert "# Birthday Voice Pack 2025" in text
        assert "Limited edition voice greetings" in text
        assert "## Variants" in text
        assert "| Variant ID | Title | Price | SKU |" in text
        assert "| 1 | Standard Edition | ¥2,000 | HLV-2025-STD |" in text

        assert metadata["store_id"] == "hololive"
        assert metadata["product_id"] == "12345"
        assert metadata["product_url"] == "https://hololive.booth.pm/items/12345"
        assert len(metadata["content_hash"]) == 64

    def test_format_complete(self):
        """Test product with multiple variants and html_details sections."""
        snapshot = ProductSnapshot(
            product_id=67890,
            title="Merch Collection",
            description="Exclusive merchandise collection",
            variants=[
                ProductVariant(1, "Blue", "¥3,000", "MERCH-BLUE"),
                ProductVariant(2, "Red", "¥3,000", "MERCH-RED"),
                ProductVariant(3, "Yellow", "¥3,500", None),
            ],
            html_details={
                "セット詳細": "セット内容：\n- Tシャツ\n- ステッカー",
                "グッズ詳細": "サイズ：M, L, XL\nmaterial: 100% cotton",
            },
        )

        name, text, metadata = _format_product_document(
            snapshot, "vspo", "https://example.com/products/67890"
        )

        assert name == "vspo:67890 - Merch Collection"
        assert "| 1 | Blue | ¥3,000 | MERCH-BLUE |" in text
        assert "| 2 | Red | ¥3,000 | MERCH-RED |" in text
        assert "| 3 | Yellow | ¥3,500 |  |" in text
        assert "## セット詳細" in text
        assert "セット内容：\n- Tシャツ\n- ステッカー" in text
        assert "## グッズ詳細" in text
        assert "サイズ：M, L, XL\nmaterial: 100% cotton" in text
        assert isinstance(metadata["product_id"], str)
        assert isinstance(metadata["content_hash"], str)

    def test_format_empty_fields(self):
        """Test product with empty description and missing SKUs."""
        snapshot = ProductSnapshot(
            product_id=11111,
            title="Basic Product",
            description="",
            variants=[
                ProductVariant(1, "Variant A", "¥1,000", None),
                ProductVariant(2, "Variant B", "¥1,500", ""),
            ],
            html_details={},
        )

        name, text, metadata = _format_product_document(
            snapshot, "test_store", "https://test.example.com/11111"
        )

        assert name == "test_store:11111 - Basic Product"
        assert "| 1 | Variant A | ¥1,000 |  |" in text
        assert "| 2 | Variant B | ¥1,500 |  |" in text
        assert metadata["store_id"] == "test_store"
        assert metadata["product_id"] == "11111"

    def test_format_no_variants(self):
        """Test product with no variants."""
        snapshot = ProductSnapshot(
            product_id=22222,
            title="Digital Product",
            description="Digital-only product",
            variants=[],
            html_details={"Details": "Download available"},
        )

        name, text, metadata = _format_product_document(
            snapshot, "digital_store", "https://digital.example.com/22222"
        )

        assert name == "digital_store:22222 - Digital Product"
        assert "## Variants" not in text
        assert "## Details" in text
        assert "Download available" in text

    def test_format_html_details_ordering(self):
        """Test that html_details sections appear in document."""
        snapshot = ProductSnapshot(
            product_id=33333,
            title="Product with Details",
            description="Test description",
            variants=[],
            html_details={
                "First Section": "Content 1",
                "Second Section": "Content 2",
                "Third Section": "Content 3",
            },
        )

        name, text, metadata = _format_product_document(
            snapshot, "store", "https://example.com/33333"
        )

        assert "## First Section" in text
        assert "## Second Section" in text
        assert "## Third Section" in text
        assert "Content 1" in text
        assert "Content 2" in text
        assert "Content 3" in text

    def test_content_hash_consistency(self):
        """Test that identical snapshots produce identical content hashes."""
        snapshot1 = ProductSnapshot(
            product_id=44444,
            title="Consistent Product",
            description="Same description",
            variants=[ProductVariant(1, "Var", "100", "SKU1")],
            html_details={"Section": "Content"},
        )
        snapshot2 = ProductSnapshot(
            product_id=44444,
            title="Consistent Product",
            description="Same description",
            variants=[ProductVariant(1, "Var", "100", "SKU1")],
            html_details={"Section": "Content"},
        )

        _, _, meta1 = _format_product_document(snapshot1, "store", "https://url1")
        _, _, meta2 = _format_product_document(snapshot2, "store", "https://url2")

        assert meta1["content_hash"] == meta2["content_hash"]
        assert meta1["product_url"] != meta2["product_url"]

    def test_metadata_all_strings(self):
        """Test that all metadata values are strings (Dify requirement)."""
        snapshot = ProductSnapshot(
            product_id=55555,
            title="String Test",
            description="",
            variants=[],
            html_details={},
        )

        _, _, metadata = _format_product_document(
            snapshot, "str_store", "https://str.example.com/55555"
        )

        for key, value in metadata.items():
            assert isinstance(key, str), f"Key {key} is not string"
            assert isinstance(value, str), f"Value for {key} is not string"

        assert metadata["product_id"] == "55555"
        assert metadata["store_id"] == "str_store"


class TestSyncProducts:
    def _snapshot(self, product_id: int, *, title: str = "T", description: str = "D"):
        return ProductSnapshot(
            product_id=product_id,
            title=title,
            description=description,
            variants=[ProductVariant(variant_id=1, title="V", price="100", sku="S")],
            html_details={"Details": "X"},
        )

    def _client(self) -> MagicMock:
        client = MagicMock(spec=DifyKBClient)
        client.create_document_by_text.return_value = {
            "document": {"id": "doc-1"},
            "batch": "batch-1",
        }
        client.update_document_by_text.return_value = {
            "document": {"id": "doc-1"},
            "batch": "batch-2",
        }
        return client

    def test_sync_new_products(self, state_repo):
        client = self._client()
        s1 = self._snapshot(1001)
        s2 = self._snapshot(1002)

        with patch(
            "estimator_king.sync.engine._poll_indexing_status", return_value=True
        ):
            result = sync_products([s1, s2], "hololive", state_repo, client)

        assert result == SyncResult(
            created=2, updated=0, skipped=0, failed=0, failed_ids=[]
        )
        assert client.create_document_by_text.call_count == 2
        assert client.update_document_by_text.call_count == 0
        assert state_repo.get_by_external_key("hololive:1001") is not None

    def test_sync_updated_products(self, state_repo):
        client = self._client()
        s1 = self._snapshot(2001, title="Old")
        s1_updated = self._snapshot(2001, title="New")

        with patch(
            "estimator_king.sync.engine._poll_indexing_status", return_value=True
        ):
            r1 = sync_products([s1], "hololive", state_repo, client)
            r2 = sync_products([s1_updated], "hololive", state_repo, client)

        assert r1.created == 1
        assert r2.updated == 1
        assert client.update_document_by_text.call_count == 1

    def test_sync_unchanged_products(self, state_repo):
        client = self._client()
        s1 = self._snapshot(3001)

        with patch(
            "estimator_king.sync.engine._poll_indexing_status", return_value=True
        ):
            _ = sync_products([s1], "hololive", state_repo, client)
            before = state_repo.get_by_external_key("hololive:3001")
            r2 = sync_products([s1], "hololive", state_repo, client)
            after = state_repo.get_by_external_key("hololive:3001")

        assert r2.skipped == 1
        assert before is not None and after is not None
        assert after.last_seen_in_sitemap_at is not None
        assert before.last_seen_in_sitemap_at is not None
        assert after.last_seen_in_sitemap_at >= before.last_seen_in_sitemap_at
        assert client.update_document_by_text.call_count == 0

    def test_sync_idempotent(self, state_repo):
        client = self._client()
        s1 = self._snapshot(4001)
        s2 = self._snapshot(4002)

        with patch(
            "estimator_king.sync.engine._poll_indexing_status", return_value=True
        ):
            r1 = sync_products([s1, s2], "hololive", state_repo, client)
            r2 = sync_products([s1, s2], "hololive", state_repo, client)

        assert r1.created == 2
        assert r2.created == 0
        assert r2.updated == 0
        assert r2.skipped == 2

    def test_sync_no_delete_call(self, state_repo):
        client = self._client()
        s1 = self._snapshot(5001)
        s2 = self._snapshot(5002)

        with patch(
            "estimator_king.sync.engine._poll_indexing_status", return_value=True
        ):
            _ = sync_products([s1, s2], "hololive", state_repo, client)
            _ = sync_products([s1], "hololive", state_repo, client)

        assert (
            not hasattr(client, "delete_document")
            or client.delete_document.call_count == 0
        )

    def test_sync_rate_limit_handling(self, state_repo):
        client = self._client()
        s1 = self._snapshot(6001)
        s2 = self._snapshot(6002)

        client.create_document_by_text.side_effect = [
            DifyRateLimitError("Rate limited"),
            {"document": {"id": "doc-2"}, "batch": "batch-2"},
        ]

        with patch(
            "estimator_king.sync.engine._poll_indexing_status", return_value=True
        ):
            r = sync_products([s1, s2], "hololive", state_repo, client)

        assert r.failed == 1
        assert r.created == 1
        assert r.failed_ids == ["hololive:6001"]

    def test_sync_indexing_failure(self, state_repo):
        client = self._client()
        s1 = self._snapshot(7001)

        with patch(
            "estimator_king.sync.engine._poll_indexing_status", return_value=False
        ):
            r = sync_products([s1], "hololive", state_repo, client)

        assert r.failed == 1
        assert r.created == 0

    def test_sync_mixed_results(self, state_repo):
        client = self._client()
        s_new = self._snapshot(8001)
        s_unchanged = self._snapshot(8002)
        s_updated_old = self._snapshot(8003, title="A")
        s_updated_new = self._snapshot(8003, title="B")
        s_fail = self._snapshot(8004)

        with patch(
            "estimator_king.sync.engine._poll_indexing_status", return_value=True
        ):
            _ = sync_products(
                [s_unchanged, s_updated_old], "hololive", state_repo, client
            )

        client.create_document_by_text.side_effect = [
            {"document": {"id": "doc-801"}, "batch": "batch-801"},
            DifyAPIError("API error"),
        ]

        with patch(
            "estimator_king.sync.engine._poll_indexing_status", return_value=True
        ):
            r = sync_products(
                [s_new, s_unchanged, s_updated_new, s_fail],
                "hololive",
                state_repo,
                client,
            )

        assert r.created == 1
        assert r.updated == 1
        assert r.skipped == 1
        assert r.failed == 1
        assert r.failed_ids == ["hololive:8004"]
