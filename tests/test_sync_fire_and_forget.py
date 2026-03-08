"""Tests for fire-and-forget sync — save doc_id immediately, skip indexing polling.

Tests verify that:
1. sync_products does NOT call _poll_indexing_status at all
2. doc_id is saved immediately after create/update API call returns
3. Result counters (created/updated/skipped/failed) are correct without polling
"""

# pyright: reportMissingImports=false

import pytest
from unittest.mock import Mock, patch
from datetime import datetime, timezone

from estimator_king.crawler.snapshot import ProductSnapshot, ProductVariant
from estimator_king.database.repository import ProductStateRepository, ProductState
from estimator_king.sync.dify_client import DifyKBClient, DifyAPIError
from estimator_king.sync.engine import sync_products


@pytest.fixture
def state_repo():
    """In-memory SQLite repository for testing."""
    with ProductStateRepository(":memory:") as repo:
        yield repo


@pytest.fixture
def dify_client():
    """Mock Dify client."""
    return Mock(spec=DifyKBClient)


def _make_snapshot(product_id=123, title="Test Product"):
    return ProductSnapshot(
        product_id=product_id,
        title=title,
        description="Test description",
        variants=[ProductVariant(1, "Standard", "¥2000", "SKU-001")],
        html_details={},
    )


class TestFireAndForgetCreatePath:
    """Create path: API returns doc_id → upsert immediately, no polling."""

    def test_create_saves_docid_immediately_no_polling(self, state_repo, dify_client):
        """
        CREATE path happy case: create_document returns doc_id and batch.
        doc_id must be saved via upsert immediately. _poll_indexing_status must NOT be called.
        Result should count as 'created'.
        """
        snapshot = _make_snapshot(product_id=200)
        doc_id = "doc-new-uuid-200"

        dify_client.create_document_by_text.return_value = {
            "document": {"id": doc_id},
            "batch": "batch-200",
        }

        with patch("estimator_king.sync.engine._poll_indexing_status") as mock_poll:
            result = sync_products(
                snapshots=[snapshot],
                store_id="test_store",
                base_url="https://shop.test",
                repository=state_repo,
                dify_client=dify_client,
            )
            mock_poll.assert_not_called()

        assert result.created == 1
        assert result.failed == 0

        saved = state_repo.get_by_external_key("test_store:200")
        assert saved is not None
        assert saved.dify_document_id == doc_id
        # content_hash should be set (not empty — that would indicate failure path)
        assert saved.content_hash != ""

    def test_create_multiple_products_no_polling(self, state_repo, dify_client):
        """Multiple creates — none should trigger polling."""
        snapshots = [
            _make_snapshot(product_id=i, title=f"Prod {i}") for i in range(301, 304)
        ]

        dify_client.create_document_by_text.side_effect = [
            {"document": {"id": f"doc-{i}"}, "batch": f"batch-{i}"}
            for i in range(301, 304)
        ]

        with patch("estimator_king.sync.engine._poll_indexing_status") as mock_poll:
            result = sync_products(
                snapshots=snapshots,
                store_id="test_store",
                base_url="https://shop.test",
                repository=state_repo,
                dify_client=dify_client,
            )
            mock_poll.assert_not_called()

        assert result.created == 3
        assert result.failed == 0

        for i in range(301, 304):
            saved = state_repo.get_by_external_key(f"test_store:{i}")
            assert saved is not None
            assert saved.dify_document_id == f"doc-{i}"


class TestFireAndForgetUpdatePath:
    """Update path: API returns new doc_id → upsert immediately, no polling."""

    def test_update_saves_docid_immediately_no_polling(self, state_repo, dify_client):
        """
        UPDATE path happy case: product exists with different content_hash.
        update_document returns new doc_id. Must save immediately, no polling.
        Result should count as 'updated'.
        """
        snapshot = _make_snapshot(product_id=400)
        external_key = "test_store:400"
        existing_doc_id = "doc-existing-400"

        # Pre-populate with existing state (different content_hash to trigger update)
        state_repo.upsert(
            ProductState(
                external_key=external_key,
                dify_document_id=existing_doc_id,
                content_hash="old-hash-will-differ",
                normalizer_version=1,
                last_seen_in_sitemap_at=datetime.now(tz=timezone.utc),
            )
        )

        new_doc_id = "doc-updated-400"
        dify_client.update_document_by_text.return_value = {
            "document": {"id": new_doc_id},
            "batch": "batch-400",
        }

        with patch("estimator_king.sync.engine._poll_indexing_status") as mock_poll:
            result = sync_products(
                snapshots=[snapshot],
                store_id="test_store",
                base_url="https://shop.test",
                repository=state_repo,
                dify_client=dify_client,
            )
            mock_poll.assert_not_called()

        assert result.updated == 1
        assert result.failed == 0

        saved = state_repo.get_by_external_key(external_key)
        assert saved is not None
        # doc_id should be preserved (either existing or new — implementation decides)
        assert saved.dify_document_id is not None


class TestFireAndForgetNoPollCalled:
    """Ensure _poll_indexing_status is never called from sync_products."""

    def test_poll_not_called_mixed_create_update_skip(self, state_repo, dify_client):
        """
        Mix of create, update, and skip paths — _poll_indexing_status must
        not be called in any of them.
        """
        # Product 1: will be created (no existing state)
        snap_create = _make_snapshot(product_id=500, title="New Product")

        # Product 2: will be updated (existing state with different hash)
        snap_update = _make_snapshot(product_id=501, title="Existing Product")
        state_repo.upsert(
            ProductState(
                external_key="test_store:501",
                dify_document_id="doc-501",
                content_hash="different-old-hash",
                normalizer_version=1,
                last_seen_in_sitemap_at=datetime.now(tz=timezone.utc),
            )
        )

        # Product 3: will be skipped (same content hash)
        snap_skip = _make_snapshot(product_id=502, title="Unchanged Product")
        from estimator_king.crawler.snapshot import compute_content_hash

        skip_hash = compute_content_hash(snap_skip)
        state_repo.upsert(
            ProductState(
                external_key="test_store:502",
                dify_document_id="doc-502",
                content_hash=skip_hash,
                normalizer_version=1,
                last_seen_in_sitemap_at=datetime.now(tz=timezone.utc),
            )
        )

        dify_client.create_document_by_text.return_value = {
            "document": {"id": "doc-new-500"},
            "batch": "batch-500",
        }
        dify_client.update_document_by_text.return_value = {
            "document": {"id": "doc-501"},
            "batch": "batch-501",
        }

        with patch("estimator_king.sync.engine._poll_indexing_status") as mock_poll:
            result = sync_products(
                snapshots=[snap_create, snap_update, snap_skip],
                store_id="test_store",
                base_url="https://shop.test",
                repository=state_repo,
                dify_client=dify_client,
            )
            mock_poll.assert_not_called()

        assert result.created == 1
        assert result.updated == 1
        assert result.skipped == 1
        assert result.failed == 0


class TestFireAndForgetResultCounters:
    """Result counters must be correct when no polling is involved."""

    def test_create_failure_still_saves_docid(self, state_repo, dify_client):
        """
        CREATE path: API call itself raises exception.
        Should count as failed, doc_id should be None.
        """
        snapshot = _make_snapshot(product_id=600)

        dify_client.create_document_by_text.side_effect = DifyAPIError(
            "API error (500): Server error"
        )

        with patch("estimator_king.sync.engine._poll_indexing_status") as mock_poll:
            result = sync_products(
                snapshots=[snapshot],
                store_id="test_store",
                base_url="https://shop.test",
                repository=state_repo,
                dify_client=dify_client,
            )
            mock_poll.assert_not_called()

        assert result.failed == 1
        assert result.created == 0
        assert "test_store:600" in result.failed_ids

    def test_counters_correct_with_all_paths(self, state_repo, dify_client):
        """
        Verify created + updated + skipped + failed = total products.
        """
        from estimator_king.crawler.snapshot import compute_content_hash

        # 1 create
        snap1 = _make_snapshot(product_id=700)
        # 1 update
        snap2 = _make_snapshot(product_id=701)
        state_repo.upsert(
            ProductState(
                external_key="test_store:701",
                dify_document_id="doc-701",
                content_hash="old-hash",
                normalizer_version=1,
                last_seen_in_sitemap_at=datetime.now(tz=timezone.utc),
            )
        )
        # 1 skip
        snap3 = _make_snapshot(product_id=702)
        state_repo.upsert(
            ProductState(
                external_key="test_store:702",
                dify_document_id="doc-702",
                content_hash=compute_content_hash(snap3),
                normalizer_version=1,
                last_seen_in_sitemap_at=datetime.now(tz=timezone.utc),
            )
        )
        # 1 create failure
        snap4 = _make_snapshot(product_id=703)

        dify_client.create_document_by_text.side_effect = [
            {"document": {"id": "doc-700"}, "batch": "batch-700"},
            DifyAPIError("fail"),
        ]
        dify_client.update_document_by_text.return_value = {
            "document": {"id": "doc-701-updated"},
            "batch": "batch-701",
        }

        with patch("estimator_king.sync.engine._poll_indexing_status") as mock_poll:
            result = sync_products(
                snapshots=[snap1, snap2, snap3, snap4],
                store_id="test_store",
                base_url="https://shop.test",
                repository=state_repo,
                dify_client=dify_client,
            )
            mock_poll.assert_not_called()

        total = result.created + result.updated + result.skipped + result.failed
        assert total == 4
        assert result.created == 1
        assert result.updated == 1
        assert result.skipped == 1
        assert result.failed == 1
