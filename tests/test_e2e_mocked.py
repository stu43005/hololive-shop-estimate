"""E2E tests for the estimation pipeline using fakes (no real LLM or vector DB calls)."""

from __future__ import annotations

# pyright: reportUnknownParameterType=false
# pyright: reportMissingParameterType=false
# pyright: reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false
# pyright: reportUnknownArgumentType=false

from pathlib import Path
from typing import Any, Final

import discord
import pytest

from estimator_king.bot.commands import format_estimates
from estimator_king.bot.estimator import Estimator
from estimator_king.llm.chat import EstimateBatch, PriceRange, ProductEstimate, ReferenceProduct
from estimator_king.vectorstore.store import VectorStore


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeEmbedder:
    """Returns a fixed embedding vector for any text."""

    DIM: Final[int] = 4

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        _ = text
        return [0.1, 0.2, 0.3, 0.4]


class FakeTypingProvider:
    """Always returns 'その他' (no LLM call)."""

    def classify_via_llm(self, text: str, item_types: list[str]) -> str:
        _ = text, item_types
        return "その他"


class FakeChat:
    """Returns a canned EstimateBatch regardless of the prompt."""

    _batch: EstimateBatch

    def __init__(self, batch: EstimateBatch) -> None:
        self._batch = batch
        self.calls: list[tuple[str, str]] = []

    def estimate(self, system_prompt: str, user_prompt: str) -> EstimateBatch:
        self.calls.append((system_prompt, user_prompt))
        return self._batch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _estimate_batch(product_names: list[str]) -> EstimateBatch:
    return EstimateBatch(
        estimates=[
            ProductEstimate(
                product_name=name,
                suggested_price_jpy=3300,
                price_range_jpy=PriceRange(min=2800, max=3800),
                confidence="high",
                rationale=f"Similar goods sell around ¥3,300 (matched: {name})",
                reference_products=[
                    ReferenceProduct(name="Holo Acrylic Stand", price_jpy=3300, store="hololive"),
                ],
            )
            for name in product_names
        ]
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def real_vector_store(tmp_path: Path) -> VectorStore:
    """A real ChromaDB VectorStore backed by a temporary directory."""
    return VectorStore(str(tmp_path / "chroma"))


@pytest.fixture()
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_estimate_products_returns_batch(
    embedder: FakeEmbedder, real_vector_store: VectorStore
) -> None:
    """Estimator.estimate_products returns an EstimateBatch with one entry per name."""
    names = ["Holo T-Shirt", "Acrylic Stand"]
    chat = FakeChat(_estimate_batch(names))

    estimator = Estimator(embedder, chat, real_vector_store, FakeTypingProvider(),  # pyright: ignore[reportArgumentType]
                          item_types=[], item_types_version=1, top_k=3)
    batch = estimator.estimate_products(names, "discord-1")

    assert len(batch.estimates) == len(names)
    assert chat.calls, "Chat provider should have been called"
    assert batch.estimates[0].product_name == "Holo T-Shirt"
    assert batch.estimates[1].product_name == "Acrylic Stand"


def test_estimate_products_with_pre_seeded_store(
    embedder: FakeEmbedder, real_vector_store: VectorStore
) -> None:
    """Estimator queries the real VectorStore and passes context to chat."""
    # Seed one document into the real vector store.
    embedding: list[float] = embedder.embed_documents(["dummy"])[0]
    metadata: dict[str, Any] = {
        "item_name": "Reference Acrylic Stand",
        "item_type": "アクリルスタンド",
        "price_jpy": 3300,
        "store_id": "hololive",
        "published_at": 0,
        "detail_snippet": "",
    }
    real_vector_store.upsert(
        id="hololive:999",
        document="# Reference Acrylic Stand\nA popular acrylic stand product",
        embedding=embedding,
        metadata=metadata,
    )

    names = ["Acrylic Stand"]
    chat = FakeChat(_estimate_batch(names))
    estimator = Estimator(embedder, chat, real_vector_store, FakeTypingProvider(),  # pyright: ignore[reportArgumentType]
                          item_types=[], item_types_version=1, top_k=5)

    batch = estimator.estimate_products(names, "discord-42")

    assert len(batch.estimates) == 1
    assert chat.calls, "Chat should have been called"
    # The retrieved reference should appear in the user prompt.
    _, user_prompt = chat.calls[0]
    assert "Reference Acrylic Stand" in user_prompt


def test_format_estimates_renders_discord_embeds(
    embedder: FakeEmbedder, real_vector_store: VectorStore
) -> None:
    """format_estimates produces valid discord.Embed objects with price info."""
    names = ["Voice Pack", "Tapestry"]
    chat = FakeChat(_estimate_batch(names))
    estimator = Estimator(embedder, chat, real_vector_store, FakeTypingProvider(),  # pyright: ignore[reportArgumentType]
                          item_types=[], item_types_version=1)

    batch = estimator.estimate_products(names, "discord-99")
    embeds = format_estimates(batch)

    assert embeds, "Should return at least one embed"
    assert all(isinstance(e, discord.Embed) for e in embeds)
    # Combined description should mention both products and the price.
    combined = "\n".join(e.description or "" for e in embeds)
    assert "Voice Pack" in combined
    assert "Tapestry" in combined
    assert "¥3,300" in combined


def test_format_estimates_empty_batch_returns_placeholder() -> None:
    """format_estimates on an empty batch returns a single placeholder embed."""
    embeds = format_estimates(EstimateBatch(estimates=[]))
    assert len(embeds) == 1
    title: str = embeds[0].title or ""
    assert "0 products" in title


def test_estimator_empty_names_returns_empty_batch(
    embedder: FakeEmbedder, real_vector_store: VectorStore
) -> None:
    """Estimator.estimate_products([]) returns an empty EstimateBatch without calling chat."""
    chat = FakeChat(_estimate_batch([]))
    estimator = Estimator(embedder, chat, real_vector_store, FakeTypingProvider(),  # pyright: ignore[reportArgumentType]
                          item_types=[], item_types_version=1)

    batch = estimator.estimate_products([], "discord-0")

    assert batch.estimates == []
    assert not chat.calls, "Chat should NOT be called for an empty name list"
