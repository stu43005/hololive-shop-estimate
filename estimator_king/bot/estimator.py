"""Price estimation pipeline: retrieve references from the vector store and ask
the chat model for structured estimates (replaces the Dify workflow)."""

import logging
from collections.abc import Sequence
from typing import Any, Protocol

from estimator_king.llm.chat import EstimateBatch

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are the Estimator King, a price estimation assistant for Japanese "
    "merchandise (hololive / vspo goods). For each product line in the user "
    "message, find the closest matches in the provided reference context and "
    "produce a price estimate. Confidence: 'high' = direct/very close match, "
    "'medium' = similar product types, 'low' = no strong match. Include up to 3 "
    "reference_products drawn from the context. Prices are integer JPY. Return "
    "estimates for every product line, in order."
)


class _Embedder(Protocol):
    def embed_query(self, text: str) -> list[float]: ...


class _Chat(Protocol):
    def estimate(self, system_prompt: str, user_prompt: str) -> EstimateBatch: ...


class _Hit(Protocol):
    metadata: dict[str, Any]


class _VectorStore(Protocol):
    def query(self, embedding: list[float], n_results: int,
              where: dict[str, Any] | None = None) -> Sequence[_Hit]: ...


class Estimator:
    CHUNK_SIZE = 10

    def __init__(self, embedder: _Embedder, chat: _Chat, vector_store: _VectorStore,
                 *, top_k: int = 10) -> None:
        self._embedder = embedder
        self._chat = chat
        self._vector_store = vector_store
        self._top_k = top_k

    def estimate_products(self, product_names: list[str], user_id: str) -> EstimateBatch:
        if not product_names:
            return EstimateBatch(estimates=[])
        logger.info("estimate request from %s for %d products", user_id, len(product_names))
        all_estimates = []
        for start in range(0, len(product_names), self.CHUNK_SIZE):
            chunk = product_names[start : start + self.CHUNK_SIZE]
            batch = self._estimate_chunk(chunk)
            all_estimates.extend(batch.estimates)
        return EstimateBatch(estimates=all_estimates)

    def _estimate_chunk(self, chunk: list[str]) -> EstimateBatch:
        context_blocks: list[str] = []
        for name in chunk:
            embedding = self._embedder.embed_query(name)
            hits = self._vector_store.query(embedding, self._top_k)
            refs = "\n".join(
                f"- {h.metadata.get('title')} | ¥{h.metadata.get('price_jpy')} "
                f"| {h.metadata.get('store_id')}"
                for h in hits
            )
            context_blocks.append(f"### Query: {name}\n{refs or '(no matches)'}")
        user_prompt = (
            "Products to estimate (one per line):\n"
            + "\n".join(chunk)
            + "\n\nReference context:\n"
            + "\n\n".join(context_blocks)
        )
        return self._chat.estimate(SYSTEM_PROMPT, user_prompt)
