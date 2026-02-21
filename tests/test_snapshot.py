"""Tests for product snapshot canonicalization and hashing."""

import pytest
from estimator_king.crawler.snapshot import (
    ProductSnapshot,
    ProductVariant,
    canonicalize_snapshot,
    compute_content_hash,
    NORMALIZER_VERSION,
)


def test_identical_snapshots_produce_same_hash():
    """Identical upstream content should yield identical hash."""
    snapshot1 = ProductSnapshot(
        product_id=123,
        title="Test Product",
        description="Description",
        variants=[ProductVariant(1, "Variant A", "1000", "SKU1")],
        html_details={"Set Details": "Details content"},
    )
    snapshot2 = ProductSnapshot(
        product_id=123,
        title="Test Product",
        description="Description",
        variants=[ProductVariant(1, "Variant A", "1000", "SKU1")],
        html_details={"Set Details": "Details content"},
    )

    hash1 = compute_content_hash(snapshot1)
    hash2 = compute_content_hash(snapshot2)

    assert hash1 == hash2
    assert len(hash1) == 64  # SHA-256 hex length


def test_whitespace_only_changes_produce_same_hash():
    """Whitespace-only changes should NOT change hash."""
    snapshot1 = ProductSnapshot(
        product_id=123,
        title="Test  Product",  # Double space
        description="Description\n\nwith newlines",
        variants=[ProductVariant(1, "Variant", "1000", "SKU1")],
        html_details={"Set Details": "Details   content"},
    )
    snapshot2 = ProductSnapshot(
        product_id=123,
        title="Test Product",  # Single space
        description="Description with newlines",
        variants=[ProductVariant(1, "Variant", "1000", "SKU1")],
        html_details={"Set Details": "Details content"},
    )

    assert compute_content_hash(snapshot1) == compute_content_hash(snapshot2)


def test_field_order_independence():
    """Different dict key order should produce same hash (sorted internally)."""
    snapshot1 = ProductSnapshot(
        product_id=123,
        title="Product",
        description="Desc",
        variants=[ProductVariant(1, "V", "100", "S")],
        html_details={"A": "1", "B": "2", "C": "3"},
    )
    snapshot2 = ProductSnapshot(
        product_id=123,
        title="Product",
        description="Desc",
        variants=[ProductVariant(1, "V", "100", "S")],
        html_details={"C": "3", "A": "1", "B": "2"},  # Different order
    )

    assert compute_content_hash(snapshot1) == compute_content_hash(snapshot2)


def test_html_entity_normalization():
    """HTML entities should be decoded consistently."""
    snapshot1 = ProductSnapshot(
        product_id=123,
        title="Product &amp; Stuff",
        description="Less than &lt; more than &gt;",
        variants=[ProductVariant(1, "Variant", "1000", "SKU")],
        html_details={},
    )
    snapshot2 = ProductSnapshot(
        product_id=123,
        title="Product & Stuff",
        description="Less than < more than >",
        variants=[ProductVariant(1, "Variant", "1000", "SKU")],
        html_details={},
    )

    assert compute_content_hash(snapshot1) == compute_content_hash(snapshot2)


def test_variant_order_stability():
    """Variants should be sorted by ID for deterministic output."""
    snapshot1 = ProductSnapshot(
        product_id=123,
        title="Product",
        description="Desc",
        variants=[
            ProductVariant(2, "B", "200", "SKU2"),
            ProductVariant(1, "A", "100", "SKU1"),
            ProductVariant(3, "C", "300", "SKU3"),
        ],
        html_details={},
    )
    snapshot2 = ProductSnapshot(
        product_id=123,
        title="Product",
        description="Desc",
        variants=[
            ProductVariant(1, "A", "100", "SKU1"),
            ProductVariant(3, "C", "300", "SKU3"),
            ProductVariant(2, "B", "200", "SKU2"),
        ],
        html_details={},
    )

    assert compute_content_hash(snapshot1) == compute_content_hash(snapshot2)


def test_normalizer_version_included():
    """Normalizer version should be in canonical text."""
    snapshot = ProductSnapshot(
        product_id=123,
        title="Product",
        description="Desc",
        variants=[],
        html_details={},
    )
    canonical = canonicalize_snapshot(snapshot)
    assert f'"normalizer_version":{NORMALIZER_VERSION}' in canonical


def test_hash_changes_when_content_changes():
    """Different content should produce different hash."""
    snapshot1 = ProductSnapshot(123, "A", "D", [], {})
    snapshot2 = ProductSnapshot(123, "B", "D", [], {})  # Different title

    assert compute_content_hash(snapshot1) != compute_content_hash(snapshot2)
