"""Sync engine: format products, embed, and upsert into the vector store.

sync_products is the single writer of product rows on the success path.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Protocol

from estimator_king.crawler.snapshot import (
    NORMALIZER_VERSION,
    ProductSnapshot,
    compute_content_hash,
)
from estimator_king.database.repository import ProductState, ProductStateRepository


class _Embedder(Protocol):
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...


class _VectorStore(Protocol):
    def upsert(self, id: str, document: str, embedding: list[float],
               metadata: dict[str, object]) -> None: ...


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    failed_ids: list[str] = field(default_factory=list)


def _min_variant_price(snapshot: ProductSnapshot) -> int:
    prices: list[int] = []
    for variant in snapshot.variants:
        try:
            prices.append(int(float(variant.price)))
        except (TypeError, ValueError):
            continue
    return min(prices) if prices else 0


def _format_product_document(
    snapshot: ProductSnapshot, store_id: str, product_url: str
) -> tuple[str, str, dict[str, object]]:
    document_name = f"{store_id}:{snapshot.product_id} - {snapshot.title}"
    parts: list[str] = [f"# {snapshot.title}", ""]
    if snapshot.description.strip():
        parts.extend([snapshot.description, ""])
    if snapshot.variants:
        parts.extend(["## Variants", "", "| Title | Price |", "|-------|-------|"])
        for variant in snapshot.variants:
            parts.append(f"| {variant.title} | {variant.price} |")
        parts.append("")
    for section_name, section_content in snapshot.html_details.items():
        if section_content.strip():
            parts.extend([f"## {section_name}", "", section_content, ""])
    text_content = "\n".join(parts).rstrip()

    metadata: dict[str, object] = {
        "store_id": store_id,
        "product_id": str(snapshot.product_id),
        "product_url": product_url,
        "content_hash": compute_content_hash(snapshot),
        "title": snapshot.title,
        "price_jpy": _min_variant_price(snapshot),
    }
    return document_name, text_content, metadata


def sync_products(
    snapshots: Iterable[ProductSnapshot],
    store_id: str,
    base_url: str,
    repository: ProductStateRepository,
    embedder: _Embedder,
    vector_store: _VectorStore,
) -> SyncResult:
    result = SyncResult()
    for snapshot in snapshots:
        now = datetime.now(tz=timezone.utc)
        external_key = f"{store_id}:{snapshot.product_id}"
        product_url = f"{base_url}/products/{snapshot.product_id}"
        content_hash = compute_content_hash(snapshot)
        state = repository.get_by_external_key(external_key)

        # Sitemap-tracking fields carried forward so the fetch never clobbers them.
        seen_at = state.last_seen_in_sitemap_at if state else now
        sitemap_misses = state.consecutive_sitemap_misses if state else 0

        unchanged = (
            state is not None
            and state.content_hash == content_hash
            and state.last_indexed_at is not None
        )

        last_indexed_at = state.last_indexed_at if state else None
        try:
            if unchanged:
                result.skipped += 1
            else:
                _name, text, metadata = _format_product_document(
                    snapshot, store_id, product_url
                )
                embedding = embedder.embed_documents([text])[0]
                vector_store.upsert(external_key, text, embedding, metadata)
                last_indexed_at = now
                if state is None:
                    result.created += 1
                else:
                    result.updated += 1
        except Exception:  # embedding/vector failure: fire-and-forget
            logging.exception("Sync failed for %s", external_key)
            result.failed += 1
            result.failed_ids.append(external_key)
            # last_indexed_at stays at the previous value (not advanced)

        repository.upsert(
            ProductState(
                external_key=external_key,
                store_id=store_id,
                product_id=str(snapshot.product_id),
                product_url=product_url,
                content_hash=content_hash,
                normalizer_version=NORMALIZER_VERSION,
                last_seen_in_sitemap_at=seen_at,
                last_fetch_success_at=now,
                last_indexed_at=last_indexed_at,
                consecutive_failures=0,
                consecutive_sitemap_misses=sitemap_misses,
            )
        )
    return result
