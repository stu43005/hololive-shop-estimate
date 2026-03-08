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
        text_parts.append("| Title | Price |")
        text_parts.append("|-------|-------|")

        for variant in snapshot.variants:
            row = f"| {variant.title} | {variant.price} |"
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
    ("completed" or "error").

    API Response Structure:
    - The response contains a "data" key with a LIST of status objects
    - Access indexing_status from the first item: response["data"][0]["indexing_status"]
    - Valid terminal statuses: "completed" (success) and "error" (failure)

    Polling Behavior:
    - Polling interval: 2 seconds between checks
    - Max attempts: max_wait / 2 (default 30 attempts for 60s timeout)
    - Returns True if status=="completed", False if status=="error" or timeout
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
            status = (response.get("data") or [{}])[0].get("indexing_status")

            if status == "completed":
                return True
            if status == "error":
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


def _handle_sync_failure(
    external_key: str,
    dify_document_id: str | None,
    content_hash: str,
    normalizer_version: int,
    now: datetime,
    repository: ProductStateRepository,
    result: SyncResult,
    exception: Exception,
    operation: str,
) -> None:
    """Handle sync operation failure (create or update).

    Saves ProductState to database with error details and increments failure counters.
    
    Args:
        external_key: Product external key
        dify_document_id: Dify document ID if available (may be None)
        content_hash: Content hash to preserve or revert
        normalizer_version: Normalizer version
        now: Current timestamp
        repository: Database repository
        result: SyncResult to update with failure
        exception: The exception that occurred
        operation: 'create' or 'update' for logging
    """
    logging.error(
        f"Sync {operation} failed for {external_key}: {type(exception).__name__}: {str(exception)}"
    )
    repository.upsert(
        ProductState(
            external_key=external_key,
            dify_document_id=dify_document_id,
            content_hash=content_hash,
            normalizer_version=normalizer_version,
            last_seen_in_sitemap_at=now,
        )
    )
    result.failed += 1
    result.failed_ids.append(external_key)


def _try_create_document(
    snapshot: ProductSnapshot,
    store_id: str,
    product_url: str,
    external_key: str,
    dify_client: DifyKBClient,
    repository: ProductStateRepository,
    result: SyncResult,
    now: datetime,
) -> bool:
    """Attempt to create a product document in Dify.

    Handles the full create flow: format, API call, save doc_id on success.
    Failures are logged and counted but not raised (fire-and-forget).
    
    Args:
        snapshot: Product snapshot to create
        store_id: Store identifier
        product_url: Product URL
        external_key: External key for DB
        dify_client: Dify client
        repository: Product state repository
        result: SyncResult to update
        now: Current timestamp
    
    Returns:
        True if create succeeded, False if it failed
    """
    captured_doc_id: str | None = None
    try:
        name, text, metadata = _format_product_document(
            snapshot, store_id, product_url
        )
        response = dify_client.create_document_by_text(name, text, metadata)
        captured_doc_id = response.get("document", {}).get("id")
        if not captured_doc_id:
            raise ValueError("Dify create response missing document id")
        
        repository.upsert(
            ProductState(
                external_key=external_key,
                dify_document_id=str(captured_doc_id),
                content_hash=compute_content_hash(snapshot),
                normalizer_version=NORMALIZER_VERSION,
                last_seen_in_sitemap_at=now,
            )
        )
        result.created += 1
        return True
    
    except (DifyRateLimitError, DifyAPIError, ValueError) as e:
        _handle_sync_failure(
            external_key,
            captured_doc_id,
            compute_content_hash(snapshot),
            NORMALIZER_VERSION,
            now,
            repository,
            result,
            e,
            "create",
        )
        return False
    except Exception as e:
        logging.exception(f"Unexpected error in create for {external_key}")
        _handle_sync_failure(
            external_key,
            captured_doc_id,
            compute_content_hash(snapshot),
            NORMALIZER_VERSION,
            now,
            repository,
            result,
            e,
            "create",
        )
        return False


def _try_update_document(
    snapshot: ProductSnapshot,
    store_id: str,
    product_url: str,
    external_key: str,
    state: ProductState,
    dify_client: DifyKBClient,
    repository: ProductStateRepository,
    result: SyncResult,
    now: datetime,
) -> bool:
    """Attempt to update a product document in Dify.

    Handles the full update flow: format, API call, save doc_id on success.
    Failures are logged and counted but not raised (fire-and-forget).
    
    Args:
        snapshot: Updated product snapshot
        store_id: Store identifier
        product_url: Product URL
        external_key: External key for DB
        state: Existing product state
        dify_client: Dify client
        repository: Product state repository
        result: SyncResult to update
        now: Current timestamp
    
    Returns:
        True if update succeeded, False if it failed
    """
    new_content_hash = compute_content_hash(snapshot)
    captured_doc_id_update: str | None = None
    try:
        name, text, _metadata = _format_product_document(
            snapshot, store_id, product_url
        )
        response = dify_client.update_document_by_text(
            state.dify_document_id, name, text
        )
        captured_doc_id_update = response.get("document", {}).get("id")
        new_doc_id = captured_doc_id_update or state.dify_document_id
        
        repository.upsert(
            ProductState(
                external_key=external_key,
                dify_document_id=new_doc_id,
                content_hash=new_content_hash,
                normalizer_version=NORMALIZER_VERSION,
                last_seen_in_sitemap_at=now,
            )
        )
        result.updated += 1
        return True
    
    except (DifyRateLimitError, DifyAPIError) as e:
        _handle_sync_failure(
            external_key,
            captured_doc_id_update or state.dify_document_id,
            state.content_hash,
            state.normalizer_version,
            now,
            repository,
            result,
            e,
            "update",
        )
        return False
    except Exception as e:
        logging.exception(f"Unexpected error in update for {external_key}")
        _handle_sync_failure(
            external_key,
            captured_doc_id_update or state.dify_document_id,
            state.content_hash,
            state.normalizer_version,
            now,
            repository,
            result,
            e,
            "update",
        )
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
            _try_create_document(
                snapshot,
                store_id,
                product_url,
                external_key,
                dify_client,
                repository,
                result,
                now,
            )
            continue
        
        assert state is not None and state.dify_document_id is not None
        
        if state.content_hash != content_hash:
            logging.debug(
                f"Content change detected for {external_key}: "
                f"old_hash={state.content_hash[:8]}... new_hash={content_hash[:8]}..."
            )
            _try_update_document(
                snapshot,
                store_id,
                product_url,
                external_key,
                state,
                dify_client,
                repository,
                result,
                now,
            )
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
