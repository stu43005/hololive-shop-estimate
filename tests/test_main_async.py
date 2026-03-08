"""Unit tests for async pipeline integration in __main__.py."""

from unittest.mock import MagicMock, patch
import pytest

from estimator_king.config_schema import AppConfig, Store, CrawlerPolicy, ProxyConfig
from estimator_king.crawler.async_pipeline import PipelineResult
from estimator_king.sync.inactive import InactiveResult


def test_async_process_queue_called_when_use_async_true():
    """Test that async_process_queue is called when USE_ASYNC is True.

    MUST: Verify that when USE_ASYNC flag is True, run_crawler uses
    asyncio.run(async_process_queue()) instead of sync process_queue().
    """
    with (
        patch("estimator_king.__main__.USE_ASYNC", True),
        patch("estimator_king.__main__.mark_inactive_products") as mock_inactive,
        patch("estimator_king.__main__.populate_queue_from_sitemap") as mock_populate,
        patch("estimator_king.__main__.enqueue_stale_products") as mock_enqueue_stale,
        patch("estimator_king.__main__.SitemapEnumerator") as mock_enum,
        patch("estimator_king.__main__.ProductStateRepository") as mock_repo_class,
        patch("estimator_king.__main__.asyncio.run") as mock_asyncio_run,
        patch("estimator_king.__main__.async_process_queue") as mock_async_process,
        patch("estimator_king.__main__.process_queue") as mock_sync_process,
        patch("estimator_king.__main__.sync_products") as mock_sync_products,
    ):
        # Setup repository mock
        mock_repo = MagicMock()
        mock_repo.__enter__ = MagicMock(return_value=mock_repo)
        mock_repo.__exit__ = MagicMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        # Setup pipeline mocks
        mock_populate.return_value = 2
        mock_enqueue_stale.return_value = 0
        mock_enum.return_value = MagicMock()

        # Setup async_process_queue to return PipelineResult
        async_result = PipelineResult(processed=2, failed=0, skipped=0)
        mock_asyncio_run.return_value = async_result

        # Setup sync_products mock (returns empty SyncResult since async path focuses on fetching)
        from estimator_king.sync.engine import SyncResult
        mock_sync_products.return_value = SyncResult(created=0, updated=0, skipped=0, failed=0)

        # Setup inactive
        mock_inactive.return_value = InactiveResult(
            marked_inactive=0, already_inactive=0
        )

        # Create config with one store
        config = AppConfig(
            stores=[
                Store(
                    id="test",
                    base_url="https://test.com",
                    sitemap_url="https://test.com/sitemap.xml",
                )
            ],
            crawler=CrawlerPolicy(),
            proxy=ProxyConfig(),
        )
        dify_client = MagicMock()

        # Import and run
        from estimator_king.__main__ import run_crawler

        result = run_crawler(config, ":memory:", dify_client)

        # Verify asyncio.run was called (async path taken)
        assert mock_asyncio_run.called, (
            "asyncio.run() should be called when USE_ASYNC is True"
        )

        # Verify sync process_queue was NOT called
        assert not mock_sync_process.called, (
            "sync process_queue() should NOT be called when USE_ASYNC is True"
        )

        # Verify result has expected structure from async conversion
        assert "fetched_ok" in result, "Result should have 'fetched_ok' key"
        assert result["fetched_ok"] == 2, "fetched_ok should equal processed count"


def test_sync_process_queue_fallback_when_use_async_false():
    """Test that sync process_queue is used when USE_ASYNC is False.

    MUST: Verify that when USE_ASYNC flag is False, run_crawler
    uses sync process_queue() without async/asyncio.run().
    """
    with (
        patch("estimator_king.__main__.USE_ASYNC", False),
        patch("estimator_king.__main__.mark_inactive_products") as mock_inactive,
        patch("estimator_king.__main__.populate_queue_from_sitemap") as mock_populate,
        patch("estimator_king.__main__.enqueue_stale_products") as mock_enqueue_stale,
        patch("estimator_king.__main__.SitemapEnumerator") as mock_enum,
        patch("estimator_king.__main__.ProductStateRepository") as mock_repo_class,
        patch("estimator_king.__main__.process_queue") as mock_sync_process,
        patch("estimator_king.__main__.asyncio.run") as mock_asyncio_run,
    ):
        # Setup repository mock
        mock_repo = MagicMock()
        mock_repo.__enter__ = MagicMock(return_value=mock_repo)
        mock_repo.__exit__ = MagicMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        # Setup pipeline mocks
        mock_populate.return_value = 0
        mock_enqueue_stale.return_value = 0
        mock_enum.return_value = MagicMock()

        # Setup sync process_queue to return dict
        sync_result = {
            "fetched_ok": 2,
            "created": 1,
            "updated": 0,
            "skipped": 0,
            "errors": 0,
        }
        mock_sync_process.return_value = sync_result

        # Setup inactive
        mock_inactive.return_value = InactiveResult(
            marked_inactive=0, already_inactive=0
        )

        # Create config with one store
        config = AppConfig(
            stores=[
                Store(
                    id="test",
                    base_url="https://test.com",
                    sitemap_url="https://test.com/sitemap.xml",
                )
            ],
            crawler=CrawlerPolicy(),
            proxy=ProxyConfig(),
        )
        dify_client = MagicMock()

        # Import and run
        from estimator_king.__main__ import run_crawler

        result = run_crawler(config, ":memory:", dify_client)

        # Verify sync process_queue was called (sync path taken)
        assert mock_sync_process.called, (
            "sync process_queue() should be called when USE_ASYNC is False"
        )

        # Verify asyncio.run was NOT called
        assert not mock_asyncio_run.called, (
            "asyncio.run() should NOT be called when USE_ASYNC is False"
        )

        # Verify result matches sync return value
        assert result["fetched_ok"] == 2, "Result should match sync return"


def test_async_result_conversion():
    """Test that async PipelineResult is correctly converted to result dict.

    MUST: Verify that when async_process_queue returns PipelineResult,
    it is converted to the expected dict format with proper key mapping.
    """
    with (
        patch("estimator_king.__main__.USE_ASYNC", True),
        patch("estimator_king.__main__.mark_inactive_products") as mock_inactive,
        patch("estimator_king.__main__.populate_queue_from_sitemap") as mock_populate,
        patch("estimator_king.__main__.enqueue_stale_products") as mock_enqueue_stale,
        patch("estimator_king.__main__.SitemapEnumerator") as mock_enum,
        patch("estimator_king.__main__.ProductStateRepository") as mock_repo_class,
        patch("estimator_king.__main__.asyncio.run") as mock_asyncio_run,
        patch("estimator_king.__main__.sync_products") as mock_sync_products,
    ):
        # Setup repository mock
        mock_repo = MagicMock()
        mock_repo.__enter__ = MagicMock(return_value=mock_repo)
        mock_repo.__exit__ = MagicMock(return_value=None)
        mock_repo_class.return_value = mock_repo

        # Setup pipeline mocks
        mock_populate.return_value = 1
        mock_enqueue_stale.return_value = 0
        mock_enum.return_value = MagicMock()

        # Return PipelineResult with specific values
        async_result = PipelineResult(processed=5, failed=2, skipped=1)
        mock_asyncio_run.return_value = async_result

        # Setup sync_products mock
        from estimator_king.sync.engine import SyncResult
        mock_sync_products.return_value = SyncResult(created=3, updated=1, skipped=1, failed=0)

        # Setup inactive
        mock_inactive.return_value = InactiveResult(
            marked_inactive=0, already_inactive=0
        )

        # Create config with one store
        config = AppConfig(
            stores=[
                Store(
                    id="test",
                    base_url="https://test.com",
                    sitemap_url="https://test.com/sitemap.xml",
                )
            ],
            crawler=CrawlerPolicy(),
            proxy=ProxyConfig(),
        )
        dify_client = MagicMock()

        # Import and run
        from estimator_king.__main__ import run_crawler

        result = run_crawler(config, ":memory:", dify_client)

        # Verify conversion: processed→fetched_ok, failed→errors, skipped→skipped
        assert result["fetched_ok"] == 5, "processed should map to fetched_ok"
        assert result["errors"] == 2, "failed should map to errors"
        assert result["skipped"] == 1, "skipped should map to skipped"
        assert result["created"] == 3, "created should come from sync_products result"
        assert result["updated"] == 1, "updated should come from sync_products result"


def test_use_async_flag_reflects_aiohttp_availability():
    """Test that USE_ASYNC flag correctly reflects aiohttp availability.

    MUST: Verify that USE_ASYNC is True when aiohttp can be imported,
    False otherwise. This test verifies the module-level flag state.
    """
    from estimator_king import __main__

    # Check that USE_ASYNC is a boolean
    assert isinstance(__main__.USE_ASYNC, bool), "USE_ASYNC should be a boolean flag"

    # If USE_ASYNC is True, async_process_queue should be available
    if __main__.USE_ASYNC:
        assert __main__.async_process_queue is not None, (
            "async_process_queue should be imported when USE_ASYNC is True"
        )
    else:
        assert __main__.async_process_queue is None, (
            "async_process_queue should be None when USE_ASYNC is False"
        )


def test_normalizer_function_converts_snapshot_to_state():
    """Test that _product_state_normalizer converts ProductSnapshot to ProductState.
    
    MUST: Verify that the normalizer function properly creates ProductState
    objects from ProductSnapshot data for async pipeline use.
    """
    from estimator_king.__main__ import _product_state_normalizer
    from estimator_king.database.repository import ProductState
    
    # Mock snapshot with required attributes for compute_content_hash
    mock_snapshot = MagicMock()
    mock_snapshot.product_id = "test-product-id"
    mock_snapshot.title = "Test Product"
    mock_snapshot.description = "Test Description"
    mock_snapshot.variants = []
    mock_snapshot.html_details = {}
    
    # Call normalizer
    result = _product_state_normalizer(
        mock_snapshot,
        store_id="store123",
        product_url="https://test.com/products/test",
        existing_state=None,
    )
    
    # Verify result is a ProductState
    assert isinstance(result, ProductState), (
        "Normalizer should return a ProductState instance"
    )
    
    # Verify key fields are set correctly
    assert result.external_key == "store123:test-product-id", "external_key should be store_id:product_id"
    assert result.product_url == "https://test.com/products/test", "product_url should be set"
    assert result.content_hash is not None, "content_hash should be computed"
    assert result.normalizer_version > 0, "normalizer_version should be set"
