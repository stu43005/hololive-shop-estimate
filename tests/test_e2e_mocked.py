"""E2E tests with mock HTTP server setup and test fixtures.

Foundation file for E2E testing with mocked Shopify and Dify endpoints.
Provides:
- Mock data fixtures (sitemap XML, product JSON/HTML)
- Pytest fixtures for temporary config and database paths
- Helper functions to setup mock endpoints
- Infrastructure ready for scenario tests (Task 20b, 20c, 20d)

Test scenarios will be added in subtasks:
- test_e2e_first_run_creates_documents (20b)
- test_e2e_second_run_idempotent (20c)
- test_e2e_content_change_updates (20d)
"""

# pyright: reportMissingImports=false

import json
import re
import pytest
import responses
from pathlib import Path
from tempfile import TemporaryDirectory

from estimator_king.config_schema import Store, CrawlerPolicy, AppConfig
from estimator_king.database.repository import ProductStateRepository


# ============================================================================
# MOCK DATA CONSTANTS
# ============================================================================

MOCK_SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://test-shop.example/products/product-a</loc>
    <lastmod>2024-02-01</lastmod>
  </url>
  <url>
    <loc>https://test-shop.example/products/product-b</loc>
    <lastmod>2024-02-01</lastmod>
  </url>
</urlset>
"""

MOCK_PRODUCT_A_JSON = {
    "product": {
        "id": 1000000001,
        "title": "Product A - Basic Item",
        "body_html": "<p>This is a basic product description.</p>",
        "handle": "product-a",
        "images": [{"id": 1, "src": "https://test-shop.example/images/a.jpg"}],
        "variants": [
            {
                "id": 2000000001,
                "title": "Default",
                "price": "1000",
                "sku": "PROD-A-001",
            }
        ],
    }
}

MOCK_PRODUCT_A_HTML = """<!doctype html>
<html lang="ja">
  <head>
    <meta charset="utf-8" />
    <title>Product A</title>
  </head>
  <body>
    <main>
      <h2>商品説明</h2>
      <p>This is product A.</p>

      <h2> セット詳細 </h2>
      <div>
        <p>・Item 1</p>
        <p>・Item 2</p>
      </div>

      <h2>注意事項</h2>
      <p>Disclaimer here.</p>
    </main>
  </body>
</html>
"""

MOCK_PRODUCT_B_JSON = {
    "product": {
        "id": 1000000002,
        "title": "Product B - Advanced Item",
        "body_html": "<p>This is an advanced product description.</p>",
        "handle": "product-b",
        "images": [{"id": 2, "src": "https://test-shop.example/images/b.jpg"}],
        "variants": [
            {
                "id": 2000000002,
                "title": "Standard",
                "price": "2000",
                "sku": "PROD-B-001",
            },
            {
                "id": 2000000003,
                "title": "Premium",
                "price": "3000",
                "sku": "PROD-B-002",
            },
        ],
    }
}

MOCK_PRODUCT_B_HTML = """<!doctype html>
<html lang="ja">
  <head>
    <meta charset="utf-8" />
    <title>Product B</title>
  </head>
  <body>
    <main>
      <h2>商品説明</h2>
      <p>This is product B.</p>

      <h2>グッズ詳細</h2>
      <div>
        <h3>サイズ</h3>
        <p>M, L, XL</p>
        <h3>素材</h3>
        <p>100% Cotton</p>
      </div>
    </main>
  </body>
</html>
"""


# ============================================================================
# PYTEST FIXTURES
# ============================================================================


@pytest.fixture
def test_config_path(tmp_path: Path) -> Path:
    """Create test configuration YAML file.

    Returns path to temporary config.yaml with test store setup.
    """
    config_path = tmp_path / "config.yaml"

    config_content = """stores:
  - id: test-shop
    base_url: https://test-shop.example
    sitemap_url: https://test-shop.example/sitemap.xml

crawler_policy:
  rate_limit_rps: 10.0
  jitter_max: 0.1
  concurrency_per_domain: 1
  timeout_connect: 10
  timeout_read: 30
  max_retries: 1

proxy:
  enabled: false
"""

    config_path.write_text(config_content)
    return config_path


@pytest.fixture
def test_db_path(tmp_path: Path) -> Path:
    """Create empty test database path.

    Returns path to temporary SQLite database.
    Database is initialized (schema created) when first repository is opened.
    """
    return tmp_path / "test.db"


@pytest.fixture
def test_repository(test_db_path: Path) -> ProductStateRepository:
    """Create test repository with initialized schema.

    Yields open repository context. Schema is created on first connection.
    """
    with ProductStateRepository(str(test_db_path)) as repo:
        yield repo


# ============================================================================
# MOCK SETUP HELPER FUNCTIONS
# ============================================================================


def setup_shopify_mocks(
    base_url: str = "https://test-shop.example",
) -> None:
    """Setup mock Shopify HTTP endpoints.

    Requires @responses.activate decorator on calling test.

    Args:
        base_url: Shopify store base URL (default: test-shop.example)

    Mocks:
        - GET {base_url}/sitemap.xml → MOCK_SITEMAP_XML
        - GET {base_url}/products/product-a.json → MOCK_PRODUCT_A_JSON
        - GET {base_url}/products/product-a → MOCK_PRODUCT_A_HTML
        - GET {base_url}/products/product-b.json → MOCK_PRODUCT_B_JSON
        - GET {base_url}/products/product-b → MOCK_PRODUCT_B_HTML
    """
    # Sitemap endpoint
    responses.add(
        responses.GET,
        f"{base_url}/sitemap.xml",
        body=MOCK_SITEMAP_XML,
        status=200,
        content_type="application/xml",
    )

    # Product A - JSON endpoint
    responses.add(
        responses.GET,
        f"{base_url}/products/product-a.json",
        json=MOCK_PRODUCT_A_JSON,
        status=200,
        content_type="application/json",
    )

    # Product A - HTML endpoint
    responses.add(
        responses.GET,
        f"{base_url}/products/product-a",
        body=MOCK_PRODUCT_A_HTML,
        status=200,
        content_type="text/html",
    )

    # Product B - JSON endpoint
    responses.add(
        responses.GET,
        f"{base_url}/products/product-b.json",
        json=MOCK_PRODUCT_B_JSON,
        status=200,
        content_type="application/json",
    )

    # Product B - HTML endpoint
    responses.add(
        responses.GET,
        f"{base_url}/products/product-b",
        body=MOCK_PRODUCT_B_HTML,
        status=200,
        content_type="text/html",
    )


def setup_dify_mocks(
    base_url: str = "https://mock-dify.example/v1",
) -> dict:
    """Setup mock Dify Knowledge Base API endpoints.

    Requires @responses.activate decorator on calling test.

    Args:
        base_url: Dify API base URL (default: mock-dify.example/v1)

    Mocks:
        - POST {base_url}/datasets/test-dataset-123/document/create_by_text
        - POST {base_url}/datasets/test-dataset-123/documents/{document_id}/update_by_text
        - GET {base_url}/datasets/test-dataset-123/documents
        - GET {base_url}/datasets/test-dataset-123/documents/{batch_id}/indexing-status

    Returns:
        Tracker dict for verifying call counts:
        {
            "create_count": int,
            "update_count": int,
            "list_count": int,
            "status_count": int,
        }
    """
    dataset_id = "test-dataset-123"
    tracker = {
        "create_count": 0,
        "update_count": 0,
        "list_count": 0,
        "status_count": 0,
    }

    # Create document endpoint
    def create_callback(request):
        tracker["create_count"] += 1
        body = json.loads(request.body)
        return (
            200,
            {},
            json.dumps(
                {
                    "document": {
                        "id": f"doc-{tracker['create_count']}",
                        "name": body.get("name", ""),
                        "word_count": len(body.get("text", "")),
                        "tokens": 0,
                        "created_at": 1705180800,
                        "updated_at": 1705180800,
                    },
                    "batch": f"batch-create-{tracker['create_count']}",
                }
            ),
        )

        # Create document callback registration
        responses.add_callback(
            responses.POST,
            re.compile(
                r"https://mock-dify\.example/v1/datasets/test-dataset-123/documents/.+/update_by_text"
            ),
            callback=capture_update,
            content_type="application/json",
        )

    # Update document endpoint
    def update_callback(request):
        tracker["update_count"] += 1
        body = json.loads(request.body)
        # Extract document_id from URL path
        doc_id = request.url.split("/documents/")[1].split("/")[0]
        return (
            200,
            {},
            json.dumps(
                {
                    "document": {
                        "id": doc_id,
                        "name": body.get("name", ""),
                        "word_count": len(body.get("text", "")),
                        "tokens": 0,
                        "created_at": 1705180800,
                        "updated_at": 1705180800,
                    },
                    "batch": f"batch-update-{tracker['update_count']}",
                }
            ),
        )

    # List documents endpoint (pagination)
    responses.add(
        responses.GET,
        f"{base_url}/datasets/{dataset_id}/documents",
        json={
            "data": [],
            "total": 0,
            "limit": 100,
            "offset": 0,
        },
        status=200,
        content_type="application/json",
    )

    # Update document endpoint
    responses.add_callback(
        responses.POST,
        re.compile(
            r"https://mock-dify\.example/v1/datasets/test-dataset-123/documents/.+/update_by_text"
        ),
        callback=update_callback,
        content_type="application/json",
    )

    # Indexing status endpoint (returns completed by default)
    def status_callback(request):
        tracker["status_count"] += 1
        return (
            200,
            {},
            json.dumps(
                {
                    "data": {
                        "id": request.url.split("/")[-1],
                        "indexing_status": "completed",
                        "processing_started_at": 1705180800,
                        "parsing_completed_at": 1705180805,
                        "cleaning_completed_at": 1705180810,
                        "splitting_completed_at": 1705180815,
                        "indexing_completed_at": 1705180820,
                        "error": None,
                        "completed_at": 1705180820,
                        "completed_segments": 100,
                        "total_segments": 100,
                    }
                }
            ),
        )

    responses.add_callback(
        responses.GET,
        f"{base_url}/datasets/{dataset_id}/documents/.+/indexing-status",
        callback=status_callback,
        content_type="application/json",
    )

    return tracker


# ============================================================================
# TEST SCENARIOS
# ============================================================================


@responses.activate
def test_e2e_first_run_creates_documents(
    test_config_path, test_db_path, test_repository
):
    """Test: First crawler run with empty DB creates documents in Dify.

    Scenario:
    - Start with empty database (test_repository initialized but no products)
    - Mock Shopify endpoints (sitemap + 2 products JSON/HTML)
    - Mock Dify endpoints with call tracking
    - Run crawler CLI with test config and database
    - Verify: 2 documents created in Dify, 2 entries in DB with correct fields
    """
    # 1. Setup mock Shopify endpoints
    sitemap_index_xml = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap>
    <loc>https://test-shop.example/sitemap-products.xml</loc>
  </sitemap>
</sitemapindex>
"""

    product_sitemap_xml = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://test-shop.example/products/product-a</loc>
    <lastmod>2024-02-01</lastmod>
  </url>
  <url>
    <loc>https://test-shop.example/products/product-b</loc>
    <lastmod>2024-02-01</lastmod>
  </url>
</urlset>
"""

    responses.add(
        responses.GET,
        "https://test-shop.example/sitemap.xml",
        body=sitemap_index_xml,
        status=200,
        content_type="application/xml",
    )

    responses.add(
        responses.GET,
        "https://test-shop.example/sitemap-products.xml",
        body=product_sitemap_xml,
        status=200,
        content_type="application/xml",
    )

    responses.add(
        responses.GET,
        "https://test-shop.example/products/product-a.json",
        json=MOCK_PRODUCT_A_JSON,
        status=200,
        content_type="application/json",
    )

    responses.add(
        responses.GET,
        "https://test-shop.example/products/product-a",
        body=MOCK_PRODUCT_A_HTML,
        status=200,
        content_type="text/html",
    )

    responses.add(
        responses.GET,
        "https://test-shop.example/products/product-b.json",
        json=MOCK_PRODUCT_B_JSON,
        status=200,
        content_type="application/json",
    )

    responses.add(
        responses.GET,
        "https://test-shop.example/products/product-b",
        body=MOCK_PRODUCT_B_HTML,
        status=200,
        content_type="text/html",
    )

    # 2. Setup mock Dify endpoints with call tracking
    create_calls = []

    def capture_create(request):
        """Capture create_by_text requests and return mock response."""
        body = json.loads(request.body)
        create_calls.append(body)
        # Return Dify create response
        return (
            200,
            {},
            json.dumps(
                {
                    "document": {
                        "id": f"doc-{len(create_calls)}",
                        "name": body.get("name", ""),
                        "data_source_type": "upload_file",
                        "word_count": len(body.get("text", "")),
                        "created_at": 1705180800,
                    },
                    "batch": f"batch-{len(create_calls)}",
                }
            ),
        )

    # Register create callback
    responses.add_callback(
        responses.POST,
        "https://mock-dify.example/v1/datasets/test-dataset-123/document/create_by_text",
        callback=capture_create,
        content_type="application/json",
    )

    # Mock Dify list documents (empty initially)
    responses.add(
        responses.GET,
        "https://mock-dify.example/v1/datasets/test-dataset-123/documents",
        json={"data": [], "total": 0, "limit": 100, "offset": 0},
        status=200,
    )

    # Mock Dify indexing status (completed)
    def status_callback(request):
        return (
            200,
            {},
            json.dumps(
                {
                    "data": {
                        "id": request.url.split("/")[-1],
                        "indexing_status": "completed",
                        "processing_started_at": 1705180800,
                        "indexing_completed_at": 1705180820,
                        "completed_segments": 100,
                        "total_segments": 100,
                    }
                }
            ),
        )

    responses.add_callback(
        responses.GET,
        "https://mock-dify.example/v1/datasets/test-dataset-123/documents/batch-1/indexing-status",
        callback=status_callback,
        content_type="application/json",
    )

    responses.add_callback(
        responses.GET,
        "https://mock-dify.example/v1/datasets/test-dataset-123/documents/batch-2/indexing-status",
        callback=status_callback,
        content_type="application/json",
    )

    # 3. Run crawler CLI
    import sys
    import os
    from estimator_king.__main__ import parse_args, run_crawler
    from estimator_king.config_schema import AppConfig
    from estimator_king.sync.dify_client import DifyKBClient

    old_argv = sys.argv
    old_env = {}
    try:
        sys.argv = [
            "estimator_king",
            "--config",
            str(test_config_path),
            "--db",
            str(test_db_path),
            "--dify-base-url",
            "https://mock-dify.example/v1",
            "--dify-api-key",
            "dataset-test-key",
            "--dify-dataset-id",
            "test-dataset-123",
        ]

        for key in (
            "DIFY_API_KEY",
            "DIFY_BASE_URL",
            "DIFY_DATASET_ID",
            "DISCORD_TOKEN",
        ):
            old_env[key] = os.environ.get(key)

        os.environ["DIFY_API_KEY"] = "dataset-test-key"
        os.environ["DISCORD_TOKEN"] = "discord-test-token"

        args = parse_args()
        config = AppConfig.from_yaml(args.config)
        dify_client = DifyKBClient(
            api_key=args.dify_api_key,
            base_url=args.dify_base_url,
            dataset_id=args.dify_dataset_id,
        )
        counters = run_crawler(config, args.db, dify_client)
    finally:
        sys.argv = old_argv
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # 4. Assertions
    # a) Verify Dify received 2 create calls
    assert len(create_calls) == 2, f"Expected 2 create calls, got {len(create_calls)}"

    for call in create_calls:
        assert "name" in call, "Create call missing 'name'"
        assert "text" in call, "Create call missing 'text'"
        assert "metadata" in call, "Create call missing 'metadata'"

    states = test_repository.get_all_active()
    assert len(states) == 2, f"Expected 2 active states in DB, got {len(states)}"

    external_keys = {state.external_key for state in states}
    assert external_keys == {"test-shop:1000000001", "test-shop:1000000002"}, (
        f"Unexpected external keys: {external_keys}"
    )

    for state in states:
        assert state.content_hash is not None, (
            f"{state.external_key}: content_hash is None"
        )
        assert len(state.content_hash) == 64, (
            f"{state.external_key}: content_hash not SHA-256"
        )
        try:
            int(state.content_hash, 16)
        except ValueError:
            raise AssertionError(f"{state.external_key}: content_hash not valid hex")

    for state in states:
        assert state.dify_document_id is not None, (
            f"{state.external_key}: dify_document_id is None"
        )
        assert state.dify_document_id.startswith("doc-"), (
            f"{state.external_key}: unexpected doc ID"
        )

    for state in states:
        assert state.inactive is False, f"{state.external_key}: should be active"

    for call in create_calls:
        metadata = call.get("metadata", {})
        assert "store_id" in metadata, "Metadata missing store_id"
        assert "product_id" in metadata, "Metadata missing product_id"
        assert "product_url" in metadata, "Metadata missing product_url"
        assert "content_hash" in metadata, "Metadata missing content_hash"


@responses.activate
def test_e2e_second_run_idempotent(test_config_path, test_db_path, test_repository):
    """Test: Second crawler run with unchanged data skips all products (idempotent).

    Scenario:
    - Run crawler once → creates 2 documents in Dify, stores 2 in DB
    - Clear call tracking
    - Run crawler again with same data → should create 0, update 0, skip 2
    - Verify: DB state unchanged, content hashes identical, no Dify operations
    """
    # 1. Setup mock Shopify endpoints (same as first run)
    sitemap_index_xml = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap>
    <loc>https://test-shop.example/sitemap-products.xml</loc>
  </sitemap>
</sitemapindex>
"""

    product_sitemap_xml = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://test-shop.example/products/product-a</loc>
    <lastmod>2024-02-01</lastmod>
  </url>
  <url>
    <loc>https://test-shop.example/products/product-b</loc>
    <lastmod>2024-02-01</lastmod>
  </url>
</urlset>
"""

    responses.add(
        responses.GET,
        "https://test-shop.example/sitemap.xml",
        body=sitemap_index_xml,
        status=200,
        content_type="application/xml",
    )

    responses.add(
        responses.GET,
        "https://test-shop.example/sitemap-products.xml",
        body=product_sitemap_xml,
        status=200,
        content_type="application/xml",
    )

    responses.add(
        responses.GET,
        "https://test-shop.example/products/product-a.json",
        json=MOCK_PRODUCT_A_JSON,
        status=200,
        content_type="application/json",
    )

    responses.add(
        responses.GET,
        "https://test-shop.example/products/product-a",
        body=MOCK_PRODUCT_A_HTML,
        status=200,
        content_type="text/html",
    )

    responses.add(
        responses.GET,
        "https://test-shop.example/products/product-b.json",
        json=MOCK_PRODUCT_B_JSON,
        status=200,
        content_type="application/json",
    )

    responses.add(
        responses.GET,
        "https://test-shop.example/products/product-b",
        body=MOCK_PRODUCT_B_HTML,
        status=200,
        content_type="text/html",
    )

    # 2. Setup mock Dify endpoints with call tracking for first run
    create_calls_run1 = []

    def capture_create_run1(request):
        """Capture create_by_text requests for first run."""
        body = json.loads(request.body)
        create_calls_run1.append(body)
        return (
            200,
            {},
            json.dumps(
                {
                    "document": {
                        "id": f"doc-{len(create_calls_run1)}",
                        "name": body.get("name", ""),
                        "data_source_type": "upload_file",
                        "word_count": len(body.get("text", "")),
                        "created_at": 1705180800,
                    },
                    "batch": f"batch-{len(create_calls_run1)}",
                }
            ),
        )

    responses.add_callback(
        responses.POST,
        "https://mock-dify.example/v1/datasets/test-dataset-123/document/create_by_text",
        callback=capture_create_run1,
        content_type="application/json",
    )

    # Mock Dify list documents (empty initially)
    responses.add(
        responses.GET,
        "https://mock-dify.example/v1/datasets/test-dataset-123/documents",
        json={"data": [], "total": 0, "limit": 100, "offset": 0},
        status=200,
    )

    # Mock Dify indexing status (completed)
    def status_callback(request):
        return (
            200,
            {},
            json.dumps(
                {
                    "data": {
                        "id": request.url.split("/")[-1],
                        "indexing_status": "completed",
                        "processing_started_at": 1705180800,
                        "indexing_completed_at": 1705180820,
                        "completed_segments": 100,
                        "total_segments": 100,
                    }
                }
            ),
        )

    responses.add_callback(
        responses.GET,
        "https://mock-dify.example/v1/datasets/test-dataset-123/documents/batch-1/indexing-status",
        callback=status_callback,
        content_type="application/json",
    )

    responses.add_callback(
        responses.GET,
        "https://mock-dify.example/v1/datasets/test-dataset-123/documents/batch-2/indexing-status",
        callback=status_callback,
        content_type="application/json",
    )

    # 3. FIRST RUN - create documents
    import sys
    import os
    from estimator_king.__main__ import parse_args, run_crawler
    from estimator_king.config_schema import AppConfig
    from estimator_king.sync.dify_client import DifyKBClient

    old_argv = sys.argv
    old_env = {}
    try:
        sys.argv = [
            "estimator_king",
            "--config",
            str(test_config_path),
            "--db",
            str(test_db_path),
            "--dify-base-url",
            "https://mock-dify.example/v1",
            "--dify-api-key",
            "dataset-test-key",
            "--dify-dataset-id",
            "test-dataset-123",
        ]

        for key in (
            "DIFY_API_KEY",
            "DIFY_BASE_URL",
            "DIFY_DATASET_ID",
            "DISCORD_TOKEN",
        ):
            old_env[key] = os.environ.get(key)

        os.environ["DIFY_API_KEY"] = "dataset-test-key"
        os.environ["DISCORD_TOKEN"] = "discord-test-token"

        args = parse_args()
        config = AppConfig.from_yaml(args.config)
        dify_client = DifyKBClient(
            api_key=args.dify_api_key,
            base_url=args.dify_base_url,
            dataset_id=args.dify_dataset_id,
        )
        result1 = run_crawler(config, args.db, dify_client)

        # Verify first run created documents
        assert len(create_calls_run1) == 2, (
            f"Expected 2 creates on first run, got {len(create_calls_run1)}"
        )

        # Capture state after first run
        states_after_first = test_repository.get_all_active()
        assert len(states_after_first) == 2, (
            f"Expected 2 active states after first run, got {len(states_after_first)}"
        )

        first_hashes = {s.external_key: s.content_hash for s in states_after_first}

        # FIX: Force WAL checkpoint to ensure second crawler run can see the data.
        # SQLite WAL mode requires explicit checkpoint for new connections to see writes.
        import sqlite3

        checkpoint_conn = sqlite3.connect(str(test_db_path))
        checkpoint_conn.execute("PRAGMA wal_checkpoint(FULL)")
        checkpoint_conn.close()

        # Close test_repository connection before second run
        test_repository.close()

        # 4. SECOND RUN - same data, should skip all
        # Reset create callback to track second run separately
        create_calls_run2 = []

        def capture_create_run2(request):
            """Capture create_by_text requests for second run."""
            body = json.loads(request.body)
            create_calls_run2.append(body)
            return (
                200,
                {},
                json.dumps(
                    {
                        "document": {
                            "id": f"doc-{len(create_calls_run1) + len(create_calls_run2)}",
                            "name": body.get("name", ""),
                            "data_source_type": "upload_file",
                            "word_count": len(body.get("text", "")),
                            "created_at": 1705180800,
                        },
                        "batch": f"batch-{len(create_calls_run1) + len(create_calls_run2)}",
                    }
                ),
            )

        # Replace create callback for second run
        responses.reset()

        # Re-setup all mocks for second run
        responses.add(
            responses.GET,
            "https://test-shop.example/sitemap.xml",
            body=sitemap_index_xml,
            status=200,
            content_type="application/xml",
        )

        responses.add(
            responses.GET,
            "https://test-shop.example/sitemap-products.xml",
            body=product_sitemap_xml,
            status=200,
            content_type="application/xml",
        )

        responses.add(
            responses.GET,
            "https://test-shop.example/products/product-a.json",
            json=MOCK_PRODUCT_A_JSON,
            status=200,
            content_type="application/json",
        )

        responses.add(
            responses.GET,
            "https://test-shop.example/products/product-a",
            body=MOCK_PRODUCT_A_HTML,
            status=200,
            content_type="text/html",
        )

        responses.add(
            responses.GET,
            "https://test-shop.example/products/product-b.json",
            json=MOCK_PRODUCT_B_JSON,
            status=200,
            content_type="application/json",
        )

        responses.add(
            responses.GET,
            "https://test-shop.example/products/product-b",
            body=MOCK_PRODUCT_B_HTML,
            status=200,
            content_type="text/html",
        )

        # Setup update callback for tracking
        update_calls = []

        def capture_update(request):
            """Capture update_by_text requests."""
            body = json.loads(request.body)
            update_calls.append(body)
            doc_id = request.url.split("/documents/")[1].split("/")[0]
            return (
                200,
                {},
                json.dumps(
                    {
                        "document": {
                            "id": doc_id,
                            "name": body.get("name", ""),
                            "data_source_type": "upload_file",
                            "word_count": len(body.get("text", "")),
                            "created_at": 1705180800,
                        },
                        "batch": f"batch-update",
                    }
                ),
            )

        responses.add_callback(
            responses.POST,
            "https://mock-dify.example/v1/datasets/test-dataset-123/document/create_by_text",
            callback=capture_create_run2,
            content_type="application/json",
        )

        responses.add_callback(
            responses.POST,
            re.compile(
                r"https://mock-dify\.example/v1/datasets/test-dataset-123/documents/.+/update_by_text"
            ),
            callback=capture_update,
            content_type="application/json",
        )

        responses.add(
            responses.GET,
            "https://mock-dify.example/v1/datasets/test-dataset-123/documents",
            json={"data": [], "total": 0, "limit": 100, "offset": 0},
            status=200,
        )

        responses.add_callback(
            responses.GET,
            "https://mock-dify.example/v1/datasets/test-dataset-123/documents/batch-1/indexing-status",
            callback=status_callback,
            content_type="application/json",
        )

        responses.add_callback(
            responses.GET,
            "https://mock-dify.example/v1/datasets/test-dataset-123/documents/batch-2/indexing-status",
            callback=status_callback,
            content_type="application/json",
        )

        responses.add_callback(
            responses.GET,
            "https://mock-dify.example/v1/datasets/test-dataset-123/documents/batch-update/indexing-status",
            callback=status_callback,
            content_type="application/json",
        )

        # Run crawler again
        args = parse_args()
        config = AppConfig.from_yaml(args.config)
        dify_client = DifyKBClient(
            api_key=args.dify_api_key,
            base_url=args.dify_base_url,
            dataset_id=args.dify_dataset_id,
        )
        result2 = run_crawler(config, args.db, dify_client)

        # Reopen test_repository to read final state after second run
        test_repository.open()

        # 5. Assertions for second run idempotency
        # a) No create calls on second run
        assert len(create_calls_run2) == 0, (
            f"Second run should not create documents, but got {len(create_calls_run2)} creates"
        )

        # b) No update calls on second run
        assert len(update_calls) == 0, (
            f"Second run should not update documents, but got {len(update_calls)} updates"
        )

        # c) DB state unchanged
        states_after_second = test_repository.get_all_active()
        assert len(states_after_second) == 2, (
            f"Expected 2 active states after second run, got {len(states_after_second)}"
        )

        # d) Content hashes remain the same
        second_hashes = {s.external_key: s.content_hash for s in states_after_second}
        assert first_hashes == second_hashes, (
            f"Content hashes changed between runs: {first_hashes} vs {second_hashes}"
        )

        # e) Verify counters show 0 operations
        assert result2["created"] == 0, f"Expected 0 creates, got {result2['created']}"
        assert result2["updated"] == 0, f"Expected 0 updates, got {result2['updated']}"
        assert result2["skipped"] == 2, f"Expected 2 skips, got {result2['skipped']}"

    finally:
        sys.argv = old_argv
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@responses.activate
def test_e2e_content_change_updates(test_config_path, test_db_path):
    """Test: Content change triggers selective updates (only changed products).

    Scenario:
    - Run crawler once → creates 2 documents in Dify, stores 2 in DB
    - Modify product-a HTML content (change detail section)
    - Run crawler again with modified data → should create 0, update 1, skip 1
    - Verify: Product-a content hash changed, product-b unchanged, only product-a updated
    """
    # Setup lists to track calls
    create_calls_run1 = []
    update_calls = []
    create_calls_run2 = []

    def capture_update(request):
        """Capture update_by_text requests."""
        body = json.loads(request.body)
        update_calls.append(body)
        doc_id = request.url.split("/documents/")[1].split("/")[0]
        return (
            200,
            {},
            json.dumps(
                {
                    "document": {
                        "id": doc_id,
                        "name": body.get("name", ""),
                        "data_source_type": "upload_file",
                        "word_count": len(body.get("text", "")),
                        "created_at": 1705180800,
                    },
                    "batch": "batch-update",
                }
            ),
        )

    # ========================================================================
    # FIRST RUN - Setup and create documents
    # ========================================================================

    sitemap_index_xml = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap>
    <loc>https://test-shop.example/sitemap-products.xml</loc>
  </sitemap>
</sitemapindex>
"""

    product_sitemap_xml = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://test-shop.example/products/product-a</loc>
    <lastmod>2024-02-01</lastmod>
  </url>
  <url>
    <loc>https://test-shop.example/products/product-b</loc>
    <lastmod>2024-02-01</lastmod>
  </url>
</urlset>
"""

    responses.add(
        responses.GET,
        "https://test-shop.example/sitemap.xml",
        body=sitemap_index_xml,
        status=200,
        content_type="application/xml",
    )

    responses.add(
        responses.GET,
        "https://test-shop.example/sitemap-products.xml",
        body=product_sitemap_xml,
        status=200,
        content_type="application/xml",
    )

    responses.add(
        responses.GET,
        "https://test-shop.example/products/product-a.json",
        json=MOCK_PRODUCT_A_JSON,
        status=200,
        content_type="application/json",
    )

    # Track product-a HTML fetch count for stateful response
    html_fetch_count = {"product_a": 0}

    def get_product_a_html_stateful(request):
        """Return original HTML on first fetch, modified HTML on subsequent fetches."""
        html_fetch_count["product_a"] += 1

        if html_fetch_count["product_a"] == 1:
            # First fetch (run 1) - return original
            html = MOCK_PRODUCT_A_HTML
        else:
            # Subsequent fetches (run 2+) - return modified
            html = MOCK_PRODUCT_A_HTML.replace("・Item 1", "・UPDATED Item 1")

        return (
            200,
            {"content-type": "text/html; charset=utf-8"},
            html.encode("utf-8"),
        )

        if html_fetch_count["product_a"] == 1:
            # First fetch (run 1) - return original
            html = MOCK_PRODUCT_A_HTML
        else:
            # Subsequent fetches (run 2+) - return modified
            html = MOCK_PRODUCT_A_HTML.replace("・Item 1", "・UPDATED Item 1")

        return (
            200,
            {"content-type": "text/html; charset=utf-8"},
            html.encode("utf-8"),
        )

    responses.add_callback(
        responses.GET,
        "https://test-shop.example/products/product-a",
        callback=get_product_a_html_stateful,
        content_type="text/html; charset=utf-8",
    )

    responses.add(
        responses.GET,
        "https://test-shop.example/products/product-b.json",
        json=MOCK_PRODUCT_B_JSON,
        status=200,
        content_type="application/json",
    )

    responses.add(
        responses.GET,
        "https://test-shop.example/products/product-b",
        body=MOCK_PRODUCT_B_HTML.encode("utf-8"),
        status=200,
        content_type="text/html; charset=utf-8",
    )

    # Setup Dify mocks for first run
    def capture_create_run1(request):
        """Capture create_by_text requests for first run."""
        body = json.loads(request.body)
        create_calls_run1.append(body)
        return (
            200,
            {},
            json.dumps(
                {
                    "document": {
                        "id": f"doc-{len(create_calls_run1)}",
                        "name": body.get("name", ""),
                        "data_source_type": "upload_file",
                        "word_count": len(body.get("text", "")),
                        "created_at": 1705180800,
                    },
                    "batch": f"batch-{len(create_calls_run1)}",
                }
            ),
        )

    responses.add_callback(
        responses.POST,
        "https://mock-dify.example/v1/datasets/test-dataset-123/document/create_by_text",
        callback=capture_create_run1,
        content_type="application/json",
    )

    responses.add_callback(
        responses.POST,
        re.compile(
            r"https://mock-dify\.example/v1/datasets/test-dataset-123/documents/.+/update_by_text"
        ),
        callback=capture_update,
    )

    responses.add(
        responses.GET,
        "https://mock-dify.example/v1/datasets/test-dataset-123/documents",
        json={"data": [], "total": 0, "limit": 100, "offset": 0},
        status=200,
    )

    def status_callback(request):
        return (
            200,
            {},
            json.dumps(
                {
                    "data": {
                        "id": request.url.split("/")[-1],
                        "indexing_status": "completed",
                        "processing_started_at": 1705180800,
                        "indexing_completed_at": 1705180820,
                        "completed_segments": 100,
                        "total_segments": 100,
                    }
                }
            ),
        )

    responses.add_callback(
        responses.GET,
        "https://mock-dify.example/v1/datasets/test-dataset-123/documents/batch-1/indexing-status",
        callback=status_callback,
        content_type="application/json",
    )

    responses.add_callback(
        responses.GET,
        "https://mock-dify.example/v1/datasets/test-dataset-123/documents/batch-2/indexing-status",
        callback=status_callback,
        content_type="application/json",
    )

    import sys
    import os
    from estimator_king.__main__ import parse_args, run_crawler
    from estimator_king.config_schema import AppConfig
    from estimator_king.sync.dify_client import DifyKBClient

    old_argv = sys.argv
    old_env = {}
    try:
        sys.argv = [
            "estimator_king",
            "--config",
            str(test_config_path),
            "--db",
            str(test_db_path),
            "--dify-base-url",
            "https://mock-dify.example/v1",
            "--dify-api-key",
            "dataset-test-key",
            "--dify-dataset-id",
            "test-dataset-123",
        ]

        for key in (
            "DIFY_API_KEY",
            "DIFY_BASE_URL",
            "DIFY_DATASET_ID",
            "DISCORD_TOKEN",
        ):
            old_env[key] = os.environ.get(key)

        os.environ["DIFY_API_KEY"] = "dataset-test-key"
        os.environ["DISCORD_TOKEN"] = "discord-test-token"

        args = parse_args()
        config = AppConfig.from_yaml(args.config)
        dify_client = DifyKBClient(
            api_key=args.dify_api_key,
            base_url=args.dify_base_url,
            dataset_id=args.dify_dataset_id,
        )
        result1 = run_crawler(config, args.db, dify_client)

        # Verify first run created documents
        assert len(create_calls_run1) == 2, (
            f"Expected 2 creates on first run, got {len(create_calls_run1)}"
        )

        # Capture state and hashes after first run
        with ProductStateRepository(str(test_db_path)) as repo:
            states_after_first = repo.get_all_active()
            assert len(states_after_first) == 2, (
                f"Expected 2 active states after first run, got {len(states_after_first)}"
            )
            first_hashes = {s.external_key: s.content_hash for s in states_after_first}
        product_a_hash_v1 = first_hashes["test-shop:1000000001"]
        product_b_hash_v1 = first_hashes["test-shop:1000000002"]

        # ====================================================================
        # SECOND RUN - Test selective update with modified product-a HTML
        # ====================================================================

        # Setup Dify mocks for second run
        def capture_create_run2(request):
            """Capture create_by_text requests for second run."""
            body = json.loads(request.body)
            create_calls_run2.append(body)
            return (
                200,
                {},
                json.dumps(
                    {
                        "document": {
                            "id": f"doc-{len(create_calls_run1) + len(create_calls_run2)}",
                            "name": body.get("name", ""),
                            "data_source_type": "upload_file",
                            "word_count": len(body.get("text", "")),
                            "created_at": 1705180800,
                        },
                        "batch": f"batch-{len(create_calls_run1) + len(create_calls_run2)}",
                    }
                ),
            )

        responses.add_callback(
            responses.POST,
            "https://mock-dify.example/v1/datasets/test-dataset-123/document/create_by_text",
            callback=capture_create_run2,
            content_type="application/json",
        )

        responses.add_callback(
            responses.POST,
            re.compile(
                r"https://mock-dify\.example/v1/datasets/test-dataset-123/documents/.+/update_by_text"
            ),
            callback=capture_update,
        )

        responses.add_callback(
            responses.GET,
            "https://mock-dify.example/v1/datasets/test-dataset-123/documents/batch-update/indexing-status",
            callback=status_callback,
            content_type="application/json",
        )

        # Run crawler again (product-a callback will return modified HTML on second fetch)
        args = parse_args()
        config = AppConfig.from_yaml(args.config)
        dify_client = DifyKBClient(
            api_key=args.dify_api_key,
            base_url=args.dify_base_url,
            dataset_id=args.dify_dataset_id,
        )
        result2 = run_crawler(config, args.db, dify_client)

        with ProductStateRepository(str(test_db_path)) as repo:
            states_after_second_pre_assert = repo.get_all_active()
            second_hashes_pre = {
                s.external_key: s.content_hash for s in states_after_second_pre_assert
            }

        # ====================================================================
        # ASSERTIONS - Verify selective update
        # ====================================================================

        # a) Verify 1 update call (product-a only)
        assert len(update_calls) == 1, (
            f"Expected 1 update call, got {len(update_calls)}"
        )

        updated_call = update_calls[0]
        assert "UPDATED Item 1" in updated_call["text"], (
            "Update should contain modified content"
        )

        # b) Verify no create calls on second run
        assert len(create_calls_run2) == 0, (
            f"Second run should not create documents, but got {len(create_calls_run2)}"
        )

        # c) Verify DB state after second run
        with ProductStateRepository(str(test_db_path)) as repo:
            states_after_second = repo.get_all_active()
            assert len(states_after_second) == 2, (
                f"Expected 2 active states after second run, got {len(states_after_second)}"
            )

            # d) Verify hashes changed for product-a, unchanged for product-b
            second_hashes = {
                s.external_key: s.content_hash for s in states_after_second
            }
            product_a_hash_v2 = second_hashes["test-shop:1000000001"]
            product_b_hash_v2 = second_hashes["test-shop:1000000002"]

        assert product_a_hash_v1 != product_a_hash_v2, (
            "Product-a hash should change after content modification"
        )

        assert product_b_hash_v1 == product_b_hash_v2, (
            "Product-b hash should remain unchanged (no content change)"
        )

        # e) Verify result counters
        assert result2["created"] == 0, f"Expected 0 creates, got {result2['created']}"
        assert result2["updated"] == 1, f"Expected 1 update, got {result2['updated']}"
        assert result2["skipped"] == 1, f"Expected 1 skip, got {result2['skipped']}"

    finally:
        sys.argv = old_argv
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
