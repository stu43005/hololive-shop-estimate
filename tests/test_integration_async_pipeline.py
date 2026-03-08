from __future__ import annotations

# pyright: reportUnknownParameterType=false
# pyright: reportMissingParameterType=false
# pyright: reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false
# pyright: reportUnknownArgumentType=false
# pyright: reportUnusedCallResult=false

import asyncio
import json
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import MagicMock
from collections.abc import Generator
from typing import Protocol, TypedDict, cast

import pytest

from estimator_king import __main__ as main_mod
from estimator_king.config_schema import AppConfig, CrawlerPolicy, ProxyConfig, Store
from estimator_king.crawler.async_pipeline import async_process_queue
from estimator_king.crawler.snapshot import (
    ProductSnapshot,
    ProductVariant,
    compute_content_hash,
)
from estimator_king.database.repository import ProductState, ProductStateRepository
from estimator_king.sync.dify_client import DifyAPIError, DifyKBClient
from estimator_king.sync.engine import sync_products


STORE_ID = "test-store"
BASE_URL = "https://shop.example.com"


@pytest.fixture()
def repo() -> Generator[ProductStateRepository, None, None]:
    with ProductStateRepository(":memory:") as r:
        yield r


def _policy(concurrency_per_domain: int = 3) -> CrawlerPolicy:
    return CrawlerPolicy(
        rate_limit_rps=1000.0,
        jitter_max=0.0,
        concurrency_per_domain=concurrency_per_domain,
        timeout_connect=5,
        timeout_read=5,
        max_retries=1,
    )


def _product_url(pid: int) -> str:
    return f"{BASE_URL}/products/p{pid}"


class CatalogEntry(TypedDict):
    product_id: int
    title: str
    body_html: str
    variant_title: str
    price: str
    sku: str
    html: str


class DifyLike(Protocol):
    def create_document_by_text(
        self,
        name: str,
        text: str,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]: ...

    def update_document_by_text(
        self,
        document_id: str,
        name: str,
        text: str,
    ) -> dict[str, object]: ...


class _FakeDifyClient:
    def __init__(self, failing_create_ids: set[int] | None = None):
        self._failing_create_ids: set[int] = failing_create_ids or set()
        self.create_ids: list[int] = []
        self.update_doc_ids: list[str] = []

    def create_document_by_text(
        self,
        name: str,
        text: str,
        metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        _ = (name, text)
        metadata = metadata or {}
        pid = int(cast(str, metadata["product_id"]))
        if pid in self._failing_create_ids:
            raise DifyAPIError(f"create failed for {pid}")
        self.create_ids.append(pid)
        return {"document": {"id": f"doc-{pid}"}, "batch": f"batch-create-{pid}"}

    def update_document_by_text(
        self,
        document_id: str,
        name: str,
        text: str,
    ) -> dict[str, object]:
        _ = (name, text)
        self.update_doc_ids.append(document_id)
        return {
            "document": {"id": f"{document_id}-updated"},
            "batch": f"batch-update-{document_id}",
        }


def _catalog_entry(pid: int, *, changed: bool = False) -> CatalogEntry:
    suffix = "-changed" if changed else ""
    return {
        "product_id": pid,
        "title": f"Product {pid}{suffix}",
        "body_html": f"<p>Description {pid}{suffix}</p>",
        "variant_title": "Default",
        "price": str(1000 + pid),
        "sku": f"SKU-{pid}",
        "html": (
            "<html><body>"
            f"<h2>Details</h2><p>Detail block {pid}{suffix}</p>"
            "</body></html>"
        ),
    }


def _snapshot_from_catalog(entry: CatalogEntry) -> ProductSnapshot:
    suffix = "-changed" if str(entry["title"]).endswith("-changed") else ""
    return ProductSnapshot(
        product_id=int(entry["product_id"]),
        title=str(entry["title"]),
        description=f"Description {entry['product_id']}{suffix}",
        variants=[
            ProductVariant(
                variant_id=int(entry["product_id"]),
                title=str(entry["variant_title"]),
                price=str(entry["price"]),
                sku=str(entry["sku"]),
            )
        ],
        html_details={},
    )


def _mock_async_http_get(
    monkeypatch,
    catalog: dict[int, CatalogEntry],
    *,
    failing_ids: set[int] | None = None,
    delay_seconds: float = 0.0,
    use_domain_semaphore: bool = False,
    stats: dict[str, int] | None = None,
) -> None:
    failing_ids = failing_ids or set()
    lock = asyncio.Lock()

    async def fake_get(self, url: str) -> str:
        domain = urlsplit(url).hostname or ""
        semaphore = (
            self._get_domain_semaphore(domain)
            if use_domain_semaphore
            else asyncio.Semaphore(9999)
        )

        async with semaphore:
            if stats is not None:
                async with lock:
                    stats["active"] = stats.get("active", 0) + 1
                    stats["max_active"] = max(
                        stats.get("max_active", 0),
                        stats["active"],
                    )

            if delay_seconds:
                await asyncio.sleep(delay_seconds)

            path = urlsplit(url).path
            part = path.split("/")[-1]
            handle = part[:-5] if part.endswith(".json") else part
            pid = int(handle.lstrip("p"))

            try:
                if pid in failing_ids:
                    raise RuntimeError(f"fetch failed for {pid}")

                entry = catalog[pid]
                return (
                    json.dumps(
                        {
                            "product": {
                                "id": int(entry["product_id"]),
                                "title": entry["title"],
                                "body_html": entry["body_html"],
                                "variants": [
                                    {
                                        "id": int(entry["product_id"]),
                                        "title": entry["variant_title"],
                                        "price": entry["price"],
                                        "sku": entry["sku"],
                                    }
                                ],
                            }
                        }
                    )
                    if url.endswith(".json")
                    else str(entry["html"])
                )
            finally:
                if stats is not None:
                    async with lock:
                        stats["active"] -= 1

    monkeypatch.setattr(
        "estimator_king.crawler.async_http_client.AsyncHTTPClient.get", fake_get
    )


def _mock_dify(
    *,
    failing_create_ids: set[int] | None = None,
) -> tuple[DifyLike, list[int], list[str]]:
    client = _FakeDifyClient(failing_create_ids)
    return client, client.create_ids, client.update_doc_ids


def _syncing_normalizer(
    dify_client: DifyLike,
    state_repo: ProductStateRepository,
    *,
    raise_on_sync_failed: bool = False,
):
    def _normalizer(
        snapshot: ProductSnapshot,
        store_id: str,
        product_url: str,
        existing_state: ProductState | None,
    ) -> ProductState | None:
        _ = (product_url, existing_state)
        result = sync_products(
            [snapshot],
            store_id,
            BASE_URL,
            state_repo,
            cast(DifyKBClient, dify_client),
        )
        if raise_on_sync_failed and result.failed > 0:
            raise RuntimeError("sync failed")
        return state_repo.get_by_external_key(f"{store_id}:{snapshot.product_id}")

    return _normalizer


def _enqueue(repo: ProductStateRepository, count: int) -> None:
    for i in range(1, count + 1):
        _ = repo.enqueue_url(STORE_ID, _product_url(i))


@pytest.mark.asyncio
async def test_async_pipeline_new_products_end_to_end(
    repo: ProductStateRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = {i: _catalog_entry(i) for i in range(1, 11)}
    _enqueue(repo, 10)
    _mock_async_http_get(monkeypatch, catalog)
    dify_client, create_ids, update_doc_ids = _mock_dify()

    result = await async_process_queue(
        STORE_ID,
        _policy(),
        repo,
        _syncing_normalizer(dify_client, repo),
    )

    assert result.processed == 10
    assert result.failed == 0
    assert result.skipped == 0
    assert repo.queue_size(STORE_ID) == 0
    assert sorted(create_ids) == list(range(1, 11))
    assert update_doc_ids == []
    for i in range(1, 11):
        state = repo.get_by_external_key(f"{STORE_ID}:{i}")
        assert state is not None
        assert state.dify_document_id == f"doc-{i}"


@pytest.mark.asyncio
async def test_async_pipeline_mixed_create_update_skip(
    repo: ProductStateRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = {i: _catalog_entry(i) for i in range(1, 11)}
    _enqueue(repo, 10)
    _mock_async_http_get(monkeypatch, catalog)
    dify_client, create_ids, update_doc_ids = _mock_dify()

    for i in (5, 6, 7):
        repo.upsert(
            ProductState(
                external_key=f"{STORE_ID}:{i}",
                dify_document_id=f"doc-existing-{i}",
                content_hash="stale-hash",
                normalizer_version=1,
            )
        )
    for i in (8, 9, 10):
        skip_hash = compute_content_hash(_snapshot_from_catalog(catalog[i]))
        repo.upsert(
            ProductState(
                external_key=f"{STORE_ID}:{i}",
                dify_document_id=f"doc-existing-{i}",
                content_hash=skip_hash,
                normalizer_version=1,
            )
        )

    result = await async_process_queue(
        STORE_ID,
        _policy(),
        repo,
        _syncing_normalizer(dify_client, repo),
    )

    assert result.processed == 10
    assert result.failed == 0
    assert result.skipped == 0
    assert sorted(create_ids) == [1, 2, 3, 4]
    assert sorted(update_doc_ids) == [
        "doc-existing-5",
        "doc-existing-6",
        "doc-existing-7",
    ]
    assert len(create_ids) == 4
    assert len(update_doc_ids) == 3


@pytest.mark.asyncio
async def test_async_pipeline_partial_fetch_failure(
    repo: ProductStateRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = {i: _catalog_entry(i) for i in range(1, 11)}
    _enqueue(repo, 10)
    _mock_async_http_get(monkeypatch, catalog, failing_ids={3, 7})
    dify_client, create_ids, _ = _mock_dify()

    result = await async_process_queue(
        STORE_ID,
        _policy(),
        repo,
        _syncing_normalizer(dify_client, repo),
    )

    assert result.processed == 8
    assert result.failed == 2
    assert result.skipped == 0
    assert repo.queue_size(STORE_ID) == 2
    assert sorted(create_ids) == [1, 2, 4, 5, 6, 8, 9, 10]


@pytest.mark.asyncio
async def test_async_pipeline_partial_dify_failure_marks_product_failed(
    repo: ProductStateRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = {i: _catalog_entry(i) for i in range(1, 11)}
    _enqueue(repo, 10)
    _mock_async_http_get(monkeypatch, catalog)
    dify_client, create_ids, _ = _mock_dify(failing_create_ids={2, 6})

    result = await async_process_queue(
        STORE_ID,
        _policy(),
        repo,
        _syncing_normalizer(dify_client, repo, raise_on_sync_failed=True),
    )

    assert result.processed == 8
    assert result.failed == 2
    assert sorted(create_ids) == [1, 3, 4, 5, 7, 8, 9, 10]
    for failed_id in (2, 6):
        state = repo.get_by_external_key(f"{STORE_ID}:{failed_id}")
        assert state is not None
        assert state.dify_document_id is None


@pytest.mark.asyncio
async def test_async_pipeline_empty_queue_returns_zero_counts(
    repo: ProductStateRepository,
) -> None:
    dify_client, _, _ = _mock_dify()
    result = await async_process_queue(
        STORE_ID,
        _policy(),
        repo,
        _syncing_normalizer(dify_client, repo),
    )
    assert result.processed == 0
    assert result.failed == 0
    assert result.skipped == 0


@pytest.mark.asyncio
async def test_async_pipeline_concurrency_per_domain_three(
    repo: ProductStateRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = {i: _catalog_entry(i) for i in range(1, 11)}
    _enqueue(repo, 10)
    stats = {"active": 0, "max_active": 0}
    _mock_async_http_get(
        monkeypatch,
        catalog,
        delay_seconds=0.02,
        use_domain_semaphore=True,
        stats=stats,
    )
    dify_client, _, _ = _mock_dify()

    result = await async_process_queue(
        STORE_ID,
        _policy(concurrency_per_domain=3),
        repo,
        _syncing_normalizer(dify_client, repo),
    )

    assert result.processed == 10
    assert result.failed == 0
    assert stats["max_active"] <= 3
    assert stats["max_active"] >= 2


@pytest.mark.asyncio
async def test_dify_document_id_persisted_then_second_run_uses_update(
    repo: ProductStateRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = {i: _catalog_entry(i) for i in range(1, 11)}
    _enqueue(repo, 10)
    _mock_async_http_get(monkeypatch, catalog)
    dify_client, create_ids, update_doc_ids = _mock_dify()

    normalizer = _syncing_normalizer(dify_client, repo)
    first = await async_process_queue(STORE_ID, _policy(), repo, normalizer)
    assert first.processed == 10
    assert len(create_ids) == 10
    assert len(update_doc_ids) == 0

    for i in range(1, 11):
        catalog[i] = _catalog_entry(i, changed=True)
        repo.enqueue_url(STORE_ID, _product_url(i))

    second = await async_process_queue(STORE_ID, _policy(), repo, normalizer)
    assert second.processed == 10
    assert len(create_ids) == 10
    assert len(update_doc_ids) == 10
    for i in range(1, 11):
        state = repo.get_by_external_key(f"{STORE_ID}:{i}")
        assert state is not None
        assert state.dify_document_id == f"doc-{i}-updated"


@pytest.mark.asyncio
async def test_content_hash_unchanged_skips_dify_calls(
    repo: ProductStateRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = {i: _catalog_entry(i) for i in range(1, 6)}
    for i in range(1, 6):
        repo.enqueue_url(STORE_ID, _product_url(i))
    _mock_async_http_get(monkeypatch, catalog)
    dify_client, create_ids, update_doc_ids = _mock_dify()
    normalizer = _syncing_normalizer(dify_client, repo)

    first = await async_process_queue(STORE_ID, _policy(), repo, normalizer)
    assert first.processed == 5
    assert len(create_ids) == 5
    assert len(update_doc_ids) == 0

    for i in range(1, 6):
        repo.enqueue_url(STORE_ID, _product_url(i))

    second = await async_process_queue(STORE_ID, _policy(), repo, normalizer)
    assert second.processed == 5
    assert len(create_ids) == 5
    assert len(update_doc_ids) == 0


@pytest.mark.asyncio
async def test_error_isolation_one_failure_does_not_abort_others(
    repo: ProductStateRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = {i: _catalog_entry(i) for i in range(1, 6)}
    _enqueue(repo, 5)
    _mock_async_http_get(monkeypatch, catalog, failing_ids={3})
    dify_client, create_ids, _ = _mock_dify()

    result = await async_process_queue(
        STORE_ID,
        _policy(),
        repo,
        _syncing_normalizer(dify_client, repo),
    )

    assert result.processed == 4
    assert result.failed == 1
    assert sorted(create_ids) == [1, 2, 4, 5]
    assert repo.get_by_external_key(f"{STORE_ID}:5") is not None


def test_sync_fallback_uses_sync_pipeline_when_use_async_false(monkeypatch) -> None:
    config = AppConfig(
        stores=[
            Store(
                id=STORE_ID,
                base_url=BASE_URL,
                sitemap_url=f"{BASE_URL}/sitemap.xml",
            )
        ],
        crawler=_policy(),
        proxy=ProxyConfig(),
    )

    class _RepoCtx:
        def __enter__(self) -> "_RepoCtx":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            _ = (exc_type, exc, tb)

    mock_repo = _RepoCtx()

    monkeypatch.setattr(main_mod, "USE_ASYNC", False)

    def fake_repo_factory(_db_path: str) -> _RepoCtx:
        return mock_repo

    def fake_http_client(*_args: object, **_kwargs: object) -> MagicMock:
        return MagicMock()

    def fake_enumerator(*_args: object, **_kwargs: object) -> MagicMock:
        return MagicMock()

    def fake_populate(*_args: object, **_kwargs: object) -> int:
        return 0

    def fake_enqueue(*_args: object, **_kwargs: object) -> int:
        return 0

    monkeypatch.setattr(main_mod, "ProductStateRepository", fake_repo_factory)
    monkeypatch.setattr(main_mod, "HTTPClient", fake_http_client)
    monkeypatch.setattr(main_mod, "SitemapEnumerator", fake_enumerator)
    monkeypatch.setattr(main_mod, "populate_queue_from_sitemap", fake_populate)
    monkeypatch.setattr(main_mod, "enqueue_stale_products", fake_enqueue)

    sync_called = {"value": False}

    def fake_process_queue(*args, **kwargs):
        _ = (args, kwargs)
        sync_called["value"] = True
        return {"fetched_ok": 1, "created": 0, "updated": 0, "skipped": 0, "errors": 0}

    async_called = {"value": False}

    async def fake_async_process_queue(*args, **kwargs):
        _ = (args, kwargs)
        async_called["value"] = True
        return SimpleNamespace(processed=1, failed=0, skipped=0)

    monkeypatch.setattr(main_mod, "process_queue", fake_process_queue)
    monkeypatch.setattr(main_mod, "async_process_queue", fake_async_process_queue)

    def fake_mark_inactive(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(marked_inactive=0, already_inactive=0)

    monkeypatch.setattr(main_mod, "mark_inactive_products", fake_mark_inactive)

    counters = main_mod.run_crawler(config, ":memory:", MagicMock(spec=DifyKBClient))
    assert sync_called["value"] is True
    assert async_called["value"] is False
    assert counters["fetched_ok"] == 1
