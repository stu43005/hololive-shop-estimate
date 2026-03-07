"""Product snapshot canonicalization and content hashing."""

import hashlib
import html
import json
from dataclasses import dataclass
from typing import Dict, List, Optional

NORMALIZER_VERSION = 2


@dataclass
class ProductVariant:
    """Product variant data."""

    variant_id: int
    title: str
    price: str
    sku: Optional[str] = None


@dataclass
class ProductSnapshot:
    """Canonical product snapshot for change detection."""

    product_id: int
    title: str
    description: str
    variants: List[ProductVariant]
    html_details: Dict[str, str]  # Section name → content


def canonicalize_snapshot(snapshot: ProductSnapshot) -> str:
    """Convert snapshot to canonical deterministic text for hashing.

    Normalization:
    - HTML entities decoded
    - Whitespace collapsed to single space
    - Dict keys sorted alphabetically
    - Excludes: updated_at, created_at, timestamps
    """
    # Build canonical dict with sorted keys
    canonical = {
        "normalizer_version": NORMALIZER_VERSION,
        "product_id": snapshot.product_id,
        "title": _normalize_text(snapshot.title),
        "description": _normalize_text(snapshot.description),
        "variants": [
            {
                "variant_id": v.variant_id,
                "title": _normalize_text(v.title),
                "price": v.price,
                "sku": v.sku or "",
            }
            for v in sorted(snapshot.variants, key=lambda x: x.variant_id)
        ],
        "html_details": {
            key: _normalize_text(value)
            for key, value in sorted(snapshot.html_details.items())
        },
    }

    # Serialize to JSON with sorted keys
    return json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def compute_content_hash(snapshot: ProductSnapshot) -> str:
    """Compute SHA-256 hash of canonical snapshot.

    Returns 64-character hex string.
    """
    canonical_text = canonicalize_snapshot(snapshot)
    return hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()


def _normalize_text(text: str) -> str:
    """Normalize text: decode entities, collapse whitespace."""
    # Decode HTML entities
    decoded = html.unescape(text)
    # Collapse whitespace
    normalized = " ".join(decoded.split())
    return normalized.strip()
