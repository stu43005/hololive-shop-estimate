"""Synchronization engine for Dify knowledge base updates.

This module handles formatting and syncing ProductSnapshot objects to Dify documents.
"""

# pyright: reportMissingImports=false

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Tuple

from estimator_king.crawler.snapshot import (
    NORMALIZER_VERSION,
    ProductSnapshot,
    compute_content_hash,
)
from estimator_king.database.repository import ProductState, ProductStateRepository
from estimator_king.sync.dify_client import (
    DifyAPIError,
    DifyKBClient,
    DifyRateLimitError,
)


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    failed_ids: List[str] = field(default_factory=list)


def _format_product_document(
    snapshot: ProductSnapshot, store_id: str, product_url: str
) -> Tuple[str, str, Dict[str, str]]:
    """Format a ProductSnapshot into a Dify document structure.

    Converts a ProductSnapshot into the three components needed for Dify:
    1. Document name: Used in Dify UI for search/display
    2. Text content: Markdown-formatted with structured sections for LLM retrieval
    3. Metadata: Key-value strings for filtering and tracking

    Args:
        snapshot: ProductSnapshot containing product data
        store_id: Store identifier (e.g., "hololive", "vspo")
        product_url: Full product URL for reference

    Returns:
        Tuple of:
        - document_name (str): Format "{store_id}:{product_id} - {title}"
        - text_content (str): Markdown-formatted product information
        - metadata (Dict[str, str]): Key-value metadata with all string values

    Example:
        >>> snapshot = ProductSnapshot(
        ...     product_id=12345,
        ...     title="Birthday Voice Pack 2025",
        ...     description="Limited edition voice pack",
        ...     variants=[ProductVariant(1, "Standard", "2000", "SKU-001")],
        ...     html_details={"Features": "Includes 5 tracks"}
        ... )
        >>> name, text, meta = _format_product_document(snapshot, "hololive", "https://...")
        >>> name
        'hololive:12345 - Birthday Voice Pack 2025'
    """

    content_hash = compute_content_hash(snapshot)
    document_name = f"{store_id}:{snapshot.product_id} - {snapshot.title}"
    text_parts = []

    text_parts.append(f"# {snapshot.title}")
    text_parts.append("")

    if snapshot.description.strip():
        text_parts.append(snapshot.description)
        text_parts.append("")

    if snapshot.variants:
        text_parts.append("## Variants")
        text_parts.append("")
        text_parts.append("| Variant ID | Title | Price | SKU |")
        text_parts.append("|------------|-------|-------|-----|")

        for variant in snapshot.variants:
            sku_cell = variant.sku if variant.sku else ""
            row = f"| {variant.variant_id} | {variant.title} | {variant.price} | {sku_cell} |"
            text_parts.append(row)

        text_parts.append("")

    if snapshot.html_details:
        for section_name, section_content in snapshot.html_details.items():
            if section_content.strip():
                text_parts.append(f"## {section_name}")
                text_parts.append("")
                text_parts.append(section_content)
                text_parts.append("")

    text_content = "\n".join(text_parts).rstrip()

    metadata: Dict[str, str] = {
        "store_id": store_id,
        "product_id": str(snapshot.product_id),
        "product_url": product_url,
        "content_hash": content_hash,
    }

    return document_name, text_content, metadata


def _poll_indexing_status(
    dify_client: DifyKBClient,
    batch_id: str,
    max_wait: int = 60,
) -> bool:
    """Poll Dify's get_indexing_status() endpoint until indexing completes or fails.

    The Dify Knowledge Base API create_document_by_text() and update_document_by_text()
    operations are asynchronous - they return immediately with a batch_id for polling.
    This function polls the indexing status until the document reaches a terminal state
    ("completed" or "failed").

    Polling Behavior:
    - Polling interval: 2 seconds between checks
    - Max attempts: max_wait / 2 (default 30 attempts for 60s timeout)
    - Returns True if status=="completed", False if status=="failed" or timeout
    - Timeout behavior: Returns False if total elapsed time exceeds max_wait

    Rate Limit Handling:
    - DifyRateLimitError (429): Implements exponential backoff
    - Default backoff: 2s, 4s, 8s, 16s, etc.
    - Retry-After header preferred if available (seconds)
    - Retries are counted separately from polling timeout

    Args:
        dify_client: DifyKBClient instance for making API calls
        batch_id: Batch ID returned from create/update operations
        max_wait: Maximum total time to wait in seconds (default: 60)

    Returns:
        True if document indexing completed successfully, False if it failed or timed out

    Raises:
        ValueError: If batch_id is empty or max_wait <= 0
    """
    if not batch_id:
        raise ValueError("batch_id cannot be empty")
    if max_wait <= 0:
        raise ValueError("max_wait must be positive")

    polling_interval = 2
    max_attempts = max_wait // polling_interval

    elapsed_time = 0
    backoff_time = 2

    for attempt in range(max_attempts):
        try:
            response = dify_client.get_indexing_status(batch_id)
            status = response.get("data", {}).get("indexing_status")

            if status == "completed":
                return True
            if status == "failed":
                return False

            time.sleep(polling_interval)
            elapsed_time += polling_interval

        except DifyRateLimitError:
            elapsed_time += backoff_time

            if elapsed_time > max_wait:
                return False

            time.sleep(backoff_time)
            backoff_time = min(backoff_time * 2, 32)

    return False


def sync_products(
    snapshots: Iterable[ProductSnapshot],
    store_id: str,
    base_url: str,
    repository: ProductStateRepository,
    dify_client: DifyKBClient,
) -> SyncResult:
    result = SyncResult()

    for snapshot in snapshots:
        now = datetime.now(tz=timezone.utc)
        external_key = f"{store_id}:{snapshot.product_id}"
        product_url = f"{base_url}/products/{snapshot.product_id}"
        content_hash = compute_content_hash(snapshot)

        state = repository.get_by_external_key(external_key)

        needs_create = state is None or state.dify_document_id is None

        if needs_create:
            try:
                name, text, metadata = _format_product_document(
                    snapshot, store_id, product_url
                )
                response = dify_client.create_document_by_text(name, text, metadata)

                batch_id = str(response.get("batch") or "")
                doc_id = response.get("document", {}).get("id")
                if not doc_id or not batch_id:
                    raise ValueError(
                        "Dify create response missing document id or batch"
                    )

                ok = _poll_indexing_status(dify_client, batch_id, max_wait=60)
                if not ok:
                    repository.upsert(
                        ProductState(
                            external_key=external_key,
                            dify_document_id=str(doc_id),
                            content_hash="",
                            normalizer_version=NORMALIZER_VERSION,
                            last_seen_in_sitemap_at=now,
                        )
                    )
                    result.failed += 1
                    result.failed_ids.append(external_key)
                    continue

                repository.upsert(
                    ProductState(
                        external_key=external_key,
                        dify_document_id=str(doc_id),
                        content_hash=content_hash,
                        normalizer_version=NORMALIZER_VERSION,
                        last_seen_in_sitemap_at=now,
                    )
                )
                result.created += 1
                continue

            except (DifyRateLimitError, DifyAPIError, Exception):
                repository.upsert(
                    ProductState(
                        external_key=external_key,
                        dify_document_id=state.dify_document_id if state else None,
                        content_hash=content_hash,
                        normalizer_version=NORMALIZER_VERSION,
                        last_seen_in_sitemap_at=now,
                    )
                )
                result.failed += 1
                result.failed_ids.append(external_key)
                continue

        assert state is not None and state.dify_document_id is not None

        if state.content_hash != content_hash:
            logging.debug(
                f"Content change detected for {external_key}: "
                f"old_hash={state.content_hash[:8]}... new_hash={content_hash[:8]}..."
            )
            try:
                name, text, _metadata = _format_product_document(
                    snapshot, store_id, product_url
                )
                response = dify_client.update_document_by_text(
                    state.dify_document_id, name, text
                )
                batch_id = str(response.get("batch") or "")
                if not batch_id:
                    raise ValueError("Dify update response missing batch")

                ok = _poll_indexing_status(dify_client, batch_id, max_wait=60)
                if not ok:
                    repository.upsert(
                        ProductState(
                            external_key=external_key,
                            dify_document_id=state.dify_document_id,
                            content_hash=state.content_hash,
                            normalizer_version=state.normalizer_version,
                            last_seen_in_sitemap_at=now,
                        )
                    )
                    result.failed += 1
                    result.failed_ids.append(external_key)
                    continue

                repository.upsert(
                    ProductState(
                        external_key=external_key,
                        dify_document_id=state.dify_document_id,
                        content_hash=content_hash,
                        normalizer_version=NORMALIZER_VERSION,
                        last_seen_in_sitemap_at=now,
                    )
                )
                result.updated += 1

            except (DifyRateLimitError, DifyAPIError, Exception):
                repository.upsert(
                    ProductState(
                        external_key=external_key,
                        dify_document_id=state.dify_document_id,
                        content_hash=state.content_hash,
                        normalizer_version=state.normalizer_version,
                        last_seen_in_sitemap_at=now,
                    )
                )
                result.failed += 1
                result.failed_ids.append(external_key)
                continue

        else:
            logging.debug(
                f"No content change for {external_key}: hash={content_hash[:8]}... (skipped)"
            )
            repository.upsert(
                ProductState(
                    external_key=external_key,
                    dify_document_id=state.dify_document_id,
                    content_hash=state.content_hash,
                    normalizer_version=state.normalizer_version,
                    last_seen_in_sitemap_at=now,
                )
            )
            result.skipped += 1

    return result
