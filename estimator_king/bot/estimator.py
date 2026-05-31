"""Price estimation: per-line type-aware retrieval + recency rerank, then ask the
chat model for structured estimates, reconciled back to the input lines."""

import logging
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any, Protocol

from estimator_king.crawler.snapshot import normalize_text
from estimator_king.llm.chat import EstimateBatch, PriceRange, ProductEstimate
from estimator_king.sync.typing import classify_query

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Role: You are the Estimator King, a price estimator for Japanese hololive/vspo "
    "merchandise. You price one item per input line using only the provided references.\n\n"
    "# Goal\nFor each product line, output a JPY price estimate grounded in the reference items.\n\n"
    "# Success criteria\n"
    "- One estimate per input line, in the same order; none skipped.\n"
    "- suggested_price and price_range are integer JPY justified by the references.\n"
    "- confidence reflects match quality (see constraints).\n\n"
    "<constraints>\n"
    "- Ground every estimate ONLY in the provided reference context; never invent prices "
    "or products not present in it.\n"
    "- Prefer references of the SAME item_type as the queried line; use cross-type references "
    "only as weak signal.\n"
    "- When references of comparable type span different dates, weight more RECENT prices "
    "higher (merchandise prices drift upward over time).\n"
    "- Match size/material using each reference's item_name and detail line when present.\n"
    "- Prices are integer JPY. Include up to 3 reference_products actually drawn from the context.\n"
    "</constraints>\n\n"
    "# Output\n"
    "Return an estimate object per line (product_name, suggested_price_jpy, price_range_jpy, "
    "confidence, rationale, reference_products). confidence: high = direct/near-exact same-type "
    "match; medium = same-type but size/variant differs; low = only cross-type or weak matches.\n\n"
    "<stop_rules>\n"
    "- If no strong match exists, still return an estimate with confidence \"low\" and a rationale "
    "stating the limitation — do NOT fabricate a closer match.\n"
    "</stop_rules>"
)


class _Embedder(Protocol):
    def embed_query(self, text: str) -> list[float]: ...


class _Chat(Protocol):
    def estimate(self, system_prompt: str, user_prompt: str) -> EstimateBatch: ...


class _TypingProvider(Protocol):
    def classify_via_llm(self, text: str, item_types: list[str]) -> str: ...


class _Hit(Protocol):
    id: str
    metadata: dict[str, Any]
    distance: float


class _VectorStore(Protocol):
    def query(self, embedding: list[float], n_results: int,
              where: dict[str, Any] | None = None) -> Sequence[_Hit]: ...


class Estimator:
    CHUNK_SIZE = 10

    def __init__(self, embedder: _Embedder, chat: _Chat, vector_store: _VectorStore,
                 typing_provider: _TypingProvider, *, item_types: list[str],
                 item_types_version: int, top_k: int = 10,
                 recency_weight: float = 0.05) -> None:
        self._embedder = embedder
        self._chat = chat
        self._vector_store = vector_store
        self._typing_provider = typing_provider
        self._item_types = item_types
        self._item_types_version = item_types_version
        self._top_k = top_k
        self._recency_weight = recency_weight

    def estimate_products(self, product_names: list[str], user_id: str) -> EstimateBatch:
        if not product_names:
            return EstimateBatch(estimates=[])
        logger.info("estimate request from %s for %d products", user_id, len(product_names))
        start = time.monotonic()
        total_chunks = (len(product_names) + self.CHUNK_SIZE - 1) // self.CHUNK_SIZE
        all_estimates: list[ProductEstimate] = []
        for start_idx in range(0, len(product_names), self.CHUNK_SIZE):
            chunk = product_names[start_idx:start_idx + self.CHUNK_SIZE]
            logger.debug("chunk %d/%d: %d products",
                         start_idx // self.CHUNK_SIZE + 1, total_chunks, len(chunk))
            batch = self._estimate_chunk(chunk)
            all_estimates.extend(batch.estimates)
        reconciled = self._reconcile(product_names, all_estimates)
        logger.info("estimate done for %s: %d estimates in %.1fs",
                    user_id, len(reconciled), time.monotonic() - start)
        return EstimateBatch(estimates=reconciled)

    def _estimate_chunk(self, chunk: list[str]) -> EstimateBatch:
        context_blocks: list[str] = []
        for name in chunk:
            embedding = self._embedder.embed_query(name)
            types = classify_query(
                name, item_types=self._item_types,
                item_types_version=self._item_types_version,
                typing_provider=self._typing_provider, repository=None,
            )
            merged: dict[str, _Hit] = {}
            queries: list[dict[str, Any] | None] = [{"item_type": t} for t in types]
            queries.append(None)  # always one plain query
            for where in queries:
                for hit in self._vector_store.query(embedding, self._top_k, where=where):
                    prev = merged.get(hit.id)
                    if prev is None or hit.distance < prev.distance:
                        merged[hit.id] = hit
            ranked = self._rerank(list(merged.values()))[: self._top_k]
            refs = "\n".join(self._format_reference(h) for h in ranked)
            context_blocks.append(f"### Query: {name}\n{refs or '(no matches)'}")
        user_prompt = (
            "Products to estimate (one per line):\n"
            + "\n".join(chunk)
            + "\n\nReference context:\n"
            + "\n\n".join(context_blocks)
        )
        return self._chat.estimate(SYSTEM_PROMPT, user_prompt)

    def _rerank(self, hits: list[_Hit]) -> list[_Hit]:
        pubs = [int(h.metadata.get("published_at", 0) or 0) for h in hits]
        positive = [p for p in pubs if p > 0]
        min_pub = min(positive) if positive else 0
        max_pub = max(positive) if positive else 0
        span = max_pub - min_pub

        def score(h: _Hit) -> float:
            similarity = 1.0 - h.distance
            pub = int(h.metadata.get("published_at", 0) or 0)
            if span > 0 and pub > 0:
                recency = (pub - min_pub) / span
            else:
                recency = 0.0
            return similarity + self._recency_weight * recency

        return sorted(hits, key=score, reverse=True)

    def _format_reference(self, hit: _Hit) -> str:
        m = hit.metadata
        pub = int(m.get("published_at", 0) or 0)
        date = "?" if pub == 0 else datetime.fromtimestamp(pub, tz=timezone.utc).strftime("%Y-%m")
        line = (f"- {m.get('item_name')} | {m.get('item_type')} | "
                f"¥{m.get('price_jpy')} | {date} | {m.get('store_id')}")
        snippet = str(m.get("detail_snippet", "") or "")
        if snippet:
            line += f"\n    {snippet[:120]}"
        return line

    def _reconcile(self, product_names: list[str],
                   estimates: list[ProductEstimate]) -> list[ProductEstimate]:
        by_name: dict[str, ProductEstimate] = {}
        for est in estimates:
            key = normalize_text(est.product_name)
            by_name.setdefault(key, est)
        matched_keys: set[str] = set()
        out: list[ProductEstimate] = []
        for line in product_names:
            key = normalize_text(line)
            est = by_name.get(key)
            if est is not None:
                matched_keys.add(key)
                out.append(est)
            else:
                out.append(ProductEstimate(
                    product_name=line, suggested_price_jpy=0,
                    price_range_jpy=PriceRange(min=0, max=0), confidence="low",
                    rationale="No estimate returned for this item.", reference_products=[]))
        surplus = len(estimates) - len(matched_keys)
        if surplus > 0:
            logger.warning("estimate reconciliation dropped %d unmatched estimate(s)", surplus)
        return out
