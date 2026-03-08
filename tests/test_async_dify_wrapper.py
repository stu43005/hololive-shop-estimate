"""Tests for AsyncDifySync async wrapper around DifyKBClient."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from estimator_king.sync.async_dify import AsyncDifySync
from estimator_king.sync.dify_client import DifyAPIError, DifyKBClient


@pytest.fixture
def mock_client():
    """Create a mock DifyKBClient."""
    client = MagicMock(spec=DifyKBClient)
    return client


@pytest.fixture
def async_wrapper(mock_client):
    """Create an AsyncDifySync instance with a mock client."""
    return AsyncDifySync(mock_client)


@pytest.mark.asyncio
async def test_create_document_calls_sync_client_in_thread(async_wrapper, mock_client):
    """Test that create_document delegates to sync client in a thread."""
    # Arrange
    mock_response = {
        "document": {"id": "doc-123", "name": "test"},
        "batch": "batch-456",
    }
    mock_client.create_document_by_text.return_value = mock_response

    # Act
    result = await async_wrapper.create_document(
        dataset_id="ds-123",
        name="Test Doc",
        content="Test content",
        metadata={"source": "test"},
    )

    # Assert
    assert result == mock_response
    mock_client.create_document_by_text.assert_called_once_with(
        name="Test Doc",
        text="Test content",
        metadata={"source": "test"},
    )


@pytest.mark.asyncio
async def test_update_document_calls_sync_client_in_thread(async_wrapper, mock_client):
    """Test that update_document delegates to sync client in a thread."""
    # Arrange
    mock_response = {
        "document": {"id": "doc-123", "name": "updated"},
        "batch": "batch-456",
    }
    mock_client.update_document_by_text.return_value = mock_response

    # Act
    result = await async_wrapper.update_document(
        dataset_id="ds-123",
        doc_id="doc-123",
        name="Updated Doc",
        content="Updated content",
        metadata={"source": "test"},
    )

    # Assert
    assert result == mock_response
    mock_client.update_document_by_text.assert_called_once_with(
        document_id="doc-123",
        name="Updated Doc",
        text="Updated content",
    )


@pytest.mark.asyncio
async def test_delete_document_calls_sync_client_in_thread(async_wrapper, mock_client):
    """Test that delete_document delegates to sync client in a thread."""
    # Arrange
    mock_client.delete_document.return_value = None

    # Act
    result = await async_wrapper.delete_document(
        dataset_id="ds-123",
        doc_id="doc-123",
    )

    # Assert
    assert result is None
    mock_client.delete_document.assert_called_once_with(
        document_id="doc-123",
    )


@pytest.mark.asyncio
async def test_create_document_propagates_exceptions(async_wrapper, mock_client):
    """Test that exceptions from sync client propagate correctly."""
    # Arrange
    mock_client.create_document_by_text.side_effect = DifyAPIError("API error")

    # Act & Assert
    with pytest.raises(DifyAPIError, match="API error"):
        await async_wrapper.create_document(
            dataset_id="ds-123",
            name="Test Doc",
            content="Test content",
            metadata=None,
        )


@pytest.mark.asyncio
async def test_update_document_propagates_exceptions(async_wrapper, mock_client):
    """Test that exceptions from sync client propagate correctly in update."""
    # Arrange
    mock_client.update_document_by_text.side_effect = DifyAPIError("Update failed")

    # Act & Assert
    with pytest.raises(DifyAPIError, match="Update failed"):
        await async_wrapper.update_document(
            dataset_id="ds-123",
            doc_id="doc-123",
            name="Updated",
            content="Updated",
            metadata=None,
        )


@pytest.mark.asyncio
async def test_delete_document_propagates_exceptions(async_wrapper, mock_client):
    """Test that exceptions from sync client propagate correctly in delete."""
    # Arrange
    mock_client.delete_document.side_effect = DifyAPIError("Delete failed")

    # Act & Assert
    with pytest.raises(DifyAPIError, match="Delete failed"):
        await async_wrapper.delete_document(
            dataset_id="ds-123",
            doc_id="doc-123",
        )


@pytest.mark.asyncio
async def test_multiple_operations_run_concurrently(async_wrapper, mock_client):
    """Test that multiple async operations can run concurrently."""

    # Arrange
    def slow_create(*args, **kwargs):
        """Simulate a slow sync operation."""
        import time

        time.sleep(0.05)
        return {"id": "doc-1"}

    def slow_update(*args, **kwargs):
        """Simulate another slow sync operation."""
        import time

        time.sleep(0.05)
        return {"id": "doc-1"}

    mock_client.create_document_by_text.side_effect = slow_create
    mock_client.update_document_by_text.side_effect = slow_update

    # Act - run both operations concurrently
    import time

    start = time.time()
    results = await asyncio.gather(
        async_wrapper.create_document("ds", "doc1", "content1", None),
        async_wrapper.update_document("ds", "doc-1", "doc2", "content2", None),
    )
    elapsed = time.time() - start

    # Assert - should be roughly 0.05s (parallel) not 0.1s (serial)
    assert len(results) == 2
    assert elapsed < 0.08  # Allow some overhead
