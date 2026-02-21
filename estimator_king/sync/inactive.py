"""Inactive product policy: marks products as inactive when thresholds exceeded.

This module handles marking products as inactive when they exceed defined thresholds
for fetch failures or sitemap misses. This prevents auto-deletion while allowing
operational visibility of problematic products.

Thresholds:
  - 3 consecutive fetch failures → inactive_reason = "fetch_failure_threshold_exceeded"
  - 4 consecutive sitemap misses → inactive_reason = "sitemap_miss_threshold_exceeded"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from estimator_king.database.repository import ProductState, ProductStateRepository


@dataclass
class InactiveResult:
    """Result of mark_inactive_products operation."""

    marked_inactive: int = 0
    already_inactive: int = 0
    failure_reasons: list[str] = field(default_factory=list)
    sitemap_reasons: list[str] = field(default_factory=list)


def mark_inactive_products(repository: ProductStateRepository) -> InactiveResult:
    """Mark products as inactive when thresholds exceeded.

    Query all active products and check if they exceed failure or sitemap miss thresholds.
    Products meeting either threshold are marked inactive with appropriate reason.
    Fetch failure threshold takes precedence if both are exceeded.

    Args:
        repository: ProductStateRepository for querying and updating product state

    Returns:
        InactiveResult with counts of marked/already-inactive products and reasons
    """
    result = InactiveResult()
    now = datetime.now(tz=timezone.utc)
    active_products = repository.get_all_active()

    for product in active_products:
        if product.consecutive_failures >= 3:
            reason = "fetch_failure_threshold_exceeded"
            result.failure_reasons.append(product.external_key)
        elif product.consecutive_sitemap_misses >= 4:
            reason = "sitemap_miss_threshold_exceeded"
            result.sitemap_reasons.append(product.external_key)
        else:
            continue

        updated_state = ProductState(
            external_key=product.external_key,
            dify_document_id=product.dify_document_id,
            content_hash=product.content_hash,
            normalizer_version=product.normalizer_version,
            last_seen_in_sitemap_at=product.last_seen_in_sitemap_at,
            last_fetch_success_at=product.last_fetch_success_at,
            consecutive_failures=product.consecutive_failures,
            consecutive_sitemap_misses=product.consecutive_sitemap_misses,
            inactive=True,
            inactive_reason=reason,
            inactive_since=now,
        )
        repository.upsert(updated_state)
        result.marked_inactive += 1

    try:
        rows = repository.connection.execute(
            "SELECT COUNT(*) FROM products WHERE inactive = 1"
        ).fetchone()
        if rows:
            total_inactive = int(rows[0])
            result.already_inactive = max(0, total_inactive - result.marked_inactive)
    except Exception:
        pass

    return result
