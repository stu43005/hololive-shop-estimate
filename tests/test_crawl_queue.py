from __future__ import annotations

# pyright: reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false
# pyright: reportUnknownParameterType=false
# pyright: reportUntypedFunctionDecorator=false
# pyright: reportMissingImports=false

import pytest  # pyright: ignore[reportMissingImports]

from estimator_king.database.repository import (  # pyright: ignore[reportMissingImports]
    ProductStateRepository,
)


@pytest.fixture()
def repo():
    with ProductStateRepository(":memory:") as r:
        yield r


# ── enqueue_url ──────────────────────────────────────────────


def test_enqueue_url_inserts_new_entry(repo: ProductStateRepository) -> None:
    result = repo.enqueue_url("store-a", "https://example.com/product/1")
    assert result is True
    assert repo.queue_size() == 1


def test_enqueue_url_duplicate_returns_false(repo: ProductStateRepository) -> None:
    repo.enqueue_url("store-a", "https://example.com/product/1")
    result = repo.enqueue_url("store-a", "https://example.com/product/1")
    assert result is False
    assert repo.queue_size() == 1


# ── peek_next ────────────────────────────────────────────────


def test_peek_next_returns_none_on_empty(repo: ProductStateRepository) -> None:
    assert repo.peek_next() is None


def test_peek_next_returns_first_item(repo: ProductStateRepository) -> None:
    repo.enqueue_url("store-a", "https://example.com/product/1")
    repo.enqueue_url("store-a", "https://example.com/product/2")

    entry = repo.peek_next()
    assert entry is not None
    entry_id, store_id, product_url = entry
    assert isinstance(entry_id, int)
    assert store_id == "store-a"
    assert product_url == "https://example.com/product/1"

    # peek is non-destructive: calling again returns the same entry
    assert repo.peek_next() == entry


def test_peek_next_with_store_id_filter(repo: ProductStateRepository) -> None:
    repo.enqueue_url("store-a", "https://example.com/a/1")
    repo.enqueue_url("store-b", "https://example.com/b/1")

    entry = repo.peek_next(store_id="store-b")
    assert entry is not None
    _, store_id, product_url = entry
    assert store_id == "store-b"
    assert product_url == "https://example.com/b/1"

    # Non-existent store returns None
    assert repo.peek_next(store_id="store-z") is None


# ── delete_queue_entry ───────────────────────────────────────


def test_delete_queue_entry_removes_specific_entry(
    repo: ProductStateRepository,
) -> None:
    repo.enqueue_url("store-a", "https://example.com/product/1")
    repo.enqueue_url("store-a", "https://example.com/product/2")

    entry = repo.peek_next()
    assert entry is not None
    entry_id = entry[0]

    repo.delete_queue_entry(entry_id)
    assert repo.queue_size() == 1

    # Next peek should return the second entry
    next_entry = repo.peek_next()
    assert next_entry is not None
    assert next_entry[2] == "https://example.com/product/2"


# ── queue_size ───────────────────────────────────────────────


def test_queue_size_counts_correctly(repo: ProductStateRepository) -> None:
    assert repo.queue_size() == 0

    repo.enqueue_url("store-a", "https://example.com/a/1")
    repo.enqueue_url("store-a", "https://example.com/a/2")
    repo.enqueue_url("store-b", "https://example.com/b/1")

    assert repo.queue_size() == 3
    assert repo.queue_size(store_id="store-a") == 2
    assert repo.queue_size(store_id="store-b") == 1
    assert repo.queue_size(store_id="store-z") == 0


# ── clear_queue ──────────────────────────────────────────────


def test_clear_queue_deletes_all(repo: ProductStateRepository) -> None:
    repo.enqueue_url("store-a", "https://example.com/a/1")
    repo.enqueue_url("store-b", "https://example.com/b/1")

    deleted = repo.clear_queue()
    assert deleted == 2
    assert repo.queue_size() == 0


def test_clear_queue_with_store_id_filter(repo: ProductStateRepository) -> None:
    repo.enqueue_url("store-a", "https://example.com/a/1")
    repo.enqueue_url("store-a", "https://example.com/a/2")
    repo.enqueue_url("store-b", "https://example.com/b/1")

    deleted = repo.clear_queue(store_id="store-a")
    assert deleted == 2
    assert repo.queue_size() == 1
    assert repo.queue_size(store_id="store-b") == 1
