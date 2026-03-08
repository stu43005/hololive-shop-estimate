"""Tests for dify_document_id preservation in sync_products exception handlers.

Tests verify that when create_document succeeds (returns doc_id) but a subsequent
operation fails (e.g., polling), the doc_id is preserved and saved, not lost.
"""

# pyright: reportMissingImports=false

import pytest
from unittest.mock import Mock, MagicMock
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


class TestSyncProductsDocIdPreservation:
    """Tests for dify_document_id preservation in exception handlers."""

    def test_create_path_api_succeeds_polling_fails_preserves_docid(
        self, state_repo, dify_client
    ):
        """
        CREATE path: create_document returns doc_id, polling raises exception.
        
        Expected: doc_id MUST be saved to DB (not None), so next run will update
        instead of create again.
        """
        snapshot = ProductSnapshot(
            product_id=123,
            title="Test Product",
            description="Test description",
            variants=[ProductVariant(1, "Standard", "¥2000", "SKU-001")],
            html_details={},
        )
        external_key = "test_store:123"

        doc_id = "doc-uuid-12345"
        dify_client.create_document_by_text.return_value = {
            "document": {"id": doc_id},
            "batch": "batch-001",
        }

        dify_client.get_indexing_status.side_effect = DifyAPIError(
            "API error (500): Polling service error"
        )

        result = sync_products(
            snapshots=[snapshot],
            store_id="test_store",
            base_url="https://shop.test",
            repository=state_repo,
            dify_client=dify_client,
        )

        assert result.failed == 1
        assert external_key in result.failed_ids

        saved_state = state_repo.get_by_external_key(external_key)
        assert saved_state is not None
        assert saved_state.dify_document_id == doc_id, (
            f"Expected dify_document_id to be '{doc_id}', but got "
            f"'{saved_state.dify_document_id}'. The doc_id from create_document "
            f"must be preserved even when polling fails."
        )

    def test_create_path_api_fails_docid_none(self, state_repo, dify_client):
        """
        CREATE path: create_document itself fails.

        Expected: dify_document_id remains None (this is OK, API didn't return one).
        """
        snapshot = ProductSnapshot(
            product_id=124,
            title="Test Product 2",
            description="Test description 2",
            variants=[ProductVariant(1, "Standard", "¥2000", "SKU-002")],
            html_details={},
        )
        external_key = "test_store:124"

        # Mock: create_document fails immediately
        dify_client.create_document_by_text.side_effect = DifyAPIError(
            "API error (500): Internal server error"
        )

        # Execute
        result = sync_products(
            snapshots=[snapshot],
            store_id="test_store",
            base_url="https://shop.test",
            repository=state_repo,
            dify_client=dify_client,
        )

        # Assert: operation marked as failed
        assert result.failed == 1
        assert external_key in result.failed_ids

        # Assert: dify_document_id is None (API never returned one)
        saved_state = state_repo.get_by_external_key(external_key)
        assert saved_state is not None
        assert saved_state.dify_document_id is None

    def test_update_path_existing_docid_preserved_on_exception(
        self, state_repo, dify_client
    ):
        """
        UPDATE path: update_document succeeds returning batch, but polling fails.

        Expected: existing dify_document_id is preserved in the exception handler.
        """
        # Setup: existing product with known doc_id
        snapshot = ProductSnapshot(
            product_id=125,
            title="Updated Product",
            description="Changed description",
            variants=[ProductVariant(1, "Standard", "¥3000", "SKU-003")],
            html_details={},
        )
        external_key = "test_store:125"
        existing_doc_id = "doc-existing-uuid"

        # Pre-populate DB with existing state
        state_repo.upsert(
            ProductState(
                external_key=external_key,
                dify_document_id=existing_doc_id,
                content_hash="old-hash",
                normalizer_version=1,
                last_seen_in_sitemap_at=datetime.now(tz=timezone.utc),
            )
        )

        # Mock: update_document returns batch
        dify_client.update_document_by_text.return_value = {
            "document": {"id": existing_doc_id},
            "batch": "batch-002",
        }

        # Mock: polling fails
        dify_client.get_indexing_status.return_value = {
            "data": {"indexing_status": "failed", "error": "Index error"}
        }

        # Execute
        result = sync_products(
            snapshots=[snapshot],
            store_id="test_store",
            base_url="https://shop.test",
            repository=state_repo,
            dify_client=dify_client,
        )

        # Assert: operation marked as failed
        assert result.failed == 1

        # Assert: existing doc_id is preserved
        saved_state = state_repo.get_by_external_key(external_key)
        assert saved_state is not None
        assert saved_state.dify_document_id == existing_doc_id, (
            f"Expected dify_document_id to remain '{existing_doc_id}', but got "
            f"'{saved_state.dify_document_id}'"
        )
