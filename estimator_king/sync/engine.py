"""Sync engine: decompose products into items, classify, embed, and upsert one
vector per item. sync_products is the single writer of product rows on success.
"""

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Protocol, Sequence

from estimator_king.crawler.snapshot import (
    NORMALIZER_VERSION,
    ProductSnapshot,
    compute_content_hash,
    normalize_text,
)
from estimator_king.database.repository import ProductState, ProductStateRepository
from estimator_king.sync.items import ProductItem, decompose_items
from estimator_king.sync.typing import classify_item

logger = logging.getLogger(__name__)


class _Embedder(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...


class _VectorStoreHit(Protocol):
    id: str
    metadata: dict[str, object]


class _VectorStore(Protocol):
    def upsert(self, id: str, document: str, embedding: list[float],
               metadata: dict[str, object]) -> None: ...
    def delete(self, ids: list[str]) -> None: ...
    def get_by_product(self, store_id: str, product_id: str) -> Sequence[_VectorStoreHit]: ...


class _TypingProvider(Protocol):
    def classify_via_llm(self, text: str, item_types: list[str]) -> str: ...


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    failed_ids: list[str] = field(default_factory=list)


def _item_slug(item_name: str, price_jpy: int) -> str:
    # Include price so two non-merged variants in one product that share an
    # identical residual name but differ in price get distinct ids (no overwrite).
    payload = f"{normalize_text(item_name)}\x1f{price_jpy}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _format_item_document(item: ProductItem, item_type: str) -> str:
    parts = [f"{item_type} {item.item_name}", "", f"# {item.product_title}"]
    if item.detail_snippet.strip():
        parts.extend(["", item.detail_snippet])
    return "\n".join(parts).rstrip()


def _item_hash(document: str, price_jpy: int, item_type: str) -> str:
    payload = f"{document}\x1f{price_jpy}\x1f{item_type}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sync_products(
    items: Iterable[tuple[str, ProductSnapshot]],
    store_id: str,
    repository: ProductStateRepository,
    embedder: _Embedder,
    vector_store: _VectorStore,
    *,
    typing_provider: _TypingProvider,
    talents: frozenset[str],
    item_types: list[str],
    item_types_version: int,
) -> SyncResult:
    result = SyncResult()
    for product_url, snapshot in items:
        now = datetime.now(tz=timezone.utc)
        external_key = f"{store_id}:{snapshot.product_id}"
        content_hash = compute_content_hash(snapshot)
        state = repository.get_by_external_key(external_key)

        seen_at = state.last_seen_in_sitemap_at if state else now
        sitemap_misses = state.consecutive_sitemap_misses if state else 0

        unchanged = (
            state is not None
            and state.content_hash == content_hash
            and state.normalizer_version == NORMALIZER_VERSION
            and state.item_types_version == item_types_version
            and state.last_indexed_at is not None
        )

        last_indexed_at = state.last_indexed_at if state else None
        try:
            if unchanged:
                result.skipped += 1
            else:
                _rebuild_product_items(
                    snapshot, store_id, product_url, repository, embedder,
                    vector_store, typing_provider, talents, item_types, item_types_version,
                )
                last_indexed_at = now
                if state is None:
                    result.created += 1
                else:
                    result.updated += 1
        except Exception:  # embedding/vector/typing failure: fire-and-forget
            logger.exception("Sync failed for %s", external_key)
            result.failed += 1
            result.failed_ids.append(external_key)

        repository.upsert(
            ProductState(
                external_key=external_key,
                store_id=store_id,
                product_id=str(snapshot.product_id),
                product_url=product_url,
                content_hash=content_hash,
                normalizer_version=NORMALIZER_VERSION,
                item_types_version=item_types_version,
                last_seen_in_sitemap_at=seen_at,
                last_fetch_success_at=now,
                last_indexed_at=last_indexed_at,
                consecutive_failures=0,
                consecutive_sitemap_misses=sitemap_misses,
            )
        )
    return result


def _rebuild_product_items(
    snapshot: ProductSnapshot,
    store_id: str,
    product_url: str,
    repository: ProductStateRepository,
    embedder: _Embedder,
    vector_store: _VectorStore,
    typing_provider: _TypingProvider,
    talents: frozenset[str],
    item_types: list[str],
    item_types_version: int,
) -> None:
    product_id = str(snapshot.product_id)
    existing = {h.id: str(h.metadata.get("item_hash", "")) for h in
                vector_store.get_by_product(store_id, product_id)}

    decomposed = decompose_items(snapshot, talents=talents)
    desired_ids: set[str] = set()
    for item in decomposed:
        item_type = classify_item(
            f"{item.item_name} {item.product_title}", item_types=item_types,
            item_types_version=item_types_version, typing_provider=typing_provider,
            repository=repository,
        )
        document = _format_item_document(item, item_type)
        item_hash = _item_hash(document, item.price_jpy, item_type)
        item_id = f"{store_id}:{product_id}:{_item_slug(item.item_name, item.price_jpy)}"
        desired_ids.add(item_id)
        if existing.get(item_id) == item_hash:
            continue  # unchanged item — skip re-embed
        embedding = embedder.embed_documents([document])[0]
        metadata: dict[str, object] = {
            "store_id": store_id,
            "product_id": product_id,
            "product_url": product_url,
            "product_title": item.product_title,
            "item_name": item.item_name,
            "item_type": item_type,
            "price_jpy": item.price_jpy,
            "published_at": item.published_at,
            "detail_snippet": item.detail_snippet,
            "item_hash": item_hash,
        }
        vector_store.upsert(item_id, document, embedding, metadata)

    stale = [vid for vid in existing if vid not in desired_ids]
    if stale:
        vector_store.delete(stale)
