"""Price estimation: per-line type-aware retrieval + recency rerank, then ask the
chat model for structured estimates, reconciled back to the input lines."""

import hashlib
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
    "<role>\n"
    "You are the Estimator King, a price estimator for Japanese hololive/vspo "
    "merchandise. You price exactly one item per input line, using only the "
    "reference items provided in the user message.\n"
    "</role>\n\n"
    "<goal>\n"
    "For each product line, output a JPY price estimate grounded in the reference "
    "items: a single suggested price, a plausible price range, a confidence level, "
    "a short rationale, and up to 3 of the references you actually used.\n"
    "</goal>\n\n"
    "<grounding_rules>\n"
    "- Use ONLY the provided reference context. Never invent prices or products not "
    "present in it.\n"
    "- Do NOT use outside market knowledge or general '相場' price ranges. If a "
    "rationale would cite a typical/general market price that is not taken from the "
    "references, that is a violation — do not use it.\n"
    "- Cite up to 3 reference_products you actually drew from the context.\n"
    "</grounding_rules>\n\n"
    "<matching_priority>\n"
    "Rank references in this strict order:\n"
    "1. item_type: references of the SAME item_type as the queried line dominate; "
    "cross-type references are only weak signal.\n"
    "2. size/material: among same-type references, prefer those whose item_name and "
    "detail line match the queried size and material.\n"
    "3. recency: use the published date ONLY to break ties among references that are "
    "otherwise equally comparable. A more recent but less-comparable reference must "
    "NOT override a closer same-type/size match.\n"
    "</matching_priority>\n\n"
    "<anchoring>\n"
    "Among the comparable same-type references, decide where to anchor the "
    "suggested price:\n"
    "- Default: anchor at the MEDIAN-to-UPPER of the comparable references — do "
    "NOT anchor below their median unless the queried line names a clearly simpler "
    "or physically smaller variant (smaller size, plain/no special material). A "
    "lower type or piece count is NOT such a signal — see <set_and_count>. Real "
    "prices tend to exceed conservative midpoints, so a below-median guess is "
    "rarely correct.\n"
    "- Premium signal: if the queried line names a premium feature or material the "
    "references lack (heated/温感, fluffy/もこもこ・あったか, oversized, character "
    "cosplay/なりきり, special material), anchor at the UPPER end instead of the "
    "median.\n"
    "</anchoring>\n\n"
    "<set_and_count>\n"
    "A type or piece count in the name (1種, 2個セット, 全4種, etc.) is NOT a "
    "reliable price multiplier:\n"
    "- Do NOT interpolate price by count — a 2-piece set is not necessarily "
    "cheaper than a 3-piece set; price on the same-type set references at the same "
    "single-vs-set tier, not on the exact number.\n"
    "- A standalone single item (e.g. 1種) can cost as much as or MORE than a "
    "bundled multi-type set, because multi-type bundles are often discounted per "
    "unit. Do not assume \"fewer types = cheaper\".\n"
    "- Treat the single-vs-set distinction and item_type as the signal; treat the "
    "specific count as a weak detail, not a price driver.\n"
    "</set_and_count>\n\n"
    "<price_format>\n"
    "All Japanese retail prices are tax-included and are exact multiples of ¥110 "
    "(pre-tax base × 1.1). suggested_price and BOTH price_range bounds must be "
    "integer JPY and exact multiples of 110.\n"
    "</price_format>\n\n"
    "<range_and_confidence>\n"
    "- price_range must bracket realistic outcomes with an upward skew (more "
    "headroom above than below), because real prices tend to exceed conservative "
    "estimates:\n"
    "  - high confidence: span roughly -20% to +30% around the suggested price.\n"
    "  - medium confidence: span roughly -25% to +45%.\n"
    "  - low confidence: span roughly -30% to +60%.\n"
    "  Keep min ≤ suggested ≤ max.\n"
    "- confidence:\n"
    "  - high = a near-exact same-NAME, same-type reference exists AND the queried "
    "line carries no extra qualifier (collaboration/brand/series name, size, "
    "material, set count) the reference lacks AND the suggested price sits within "
    "the price span of same-type references (not extrapolated).\n"
    "  - medium = same-type references exist but size/variant/feature/set-count "
    "differs, OR the name is a generic single word whose same-type references span "
    "a wide price range.\n"
    "  - low = only cross-type or weak matches.\n"
    "</range_and_confidence>\n\n"
    "<output_rules>\n"
    "- Produce exactly one estimate per input line, in the same order; none skipped, "
    "none merged.\n"
    "- If no strong match exists, still return an estimate with confidence \"low\" "
    "and a rationale stating the limitation — do NOT fabricate a closer match.\n"
    "</output_rules>"
)

_TAX_GRID_JPY = 110


def snap_to_tax_grid(price: int) -> int:
    """Round a JPY price to the nearest ¥110 tax-inclusive grid point.

    Japanese retail prices are tax-included and are exact multiples of ¥110
    (pre-tax base x 1.1). Ties (remainder exactly 55) round up, matching the
    observed upward price drift. Non-positive input returns 0, preserving the
    "no estimate" sentinel produced by reconciliation.
    """
    if price <= 0:
        return 0
    quotient, remainder = divmod(price, _TAX_GRID_JPY)
    if remainder * 2 >= _TAX_GRID_JPY:
        quotient += 1
    return quotient * _TAX_GRID_JPY


def _percentile(values: list[int], pct: float) -> float | None:
    """Linear-interpolated percentile of `values` (pct in 0-100). None if empty."""
    s = sorted(values)
    if not s:
        return None
    if len(s) == 1:
        return float(s[0])
    pos = (pct / 100.0) * (len(s) - 1)
    lo = int(pos)
    frac = pos - lo
    if lo + 1 < len(s):
        return s[lo] + (s[lo + 1] - s[lo]) * frac
    return float(s[lo])


def _snap_estimate(est: ProductEstimate) -> ProductEstimate:
    """Snap an estimate's prices onto the ¥110 grid, keeping min <= suggested <= max."""
    suggested = snap_to_tax_grid(est.suggested_price_jpy)
    low = snap_to_tax_grid(est.price_range_jpy.min)
    high = snap_to_tax_grid(est.price_range_jpy.max)
    low = min(low, suggested)
    high = max(high, suggested)
    return est.model_copy(update={
        "suggested_price_jpy": suggested,
        "price_range_jpy": PriceRange(min=low, max=high),
    })


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
                 recency_weight: float = 0.05,
                 diversity_weight: float = 0.05,
                 fetch_multiplier: int = 2) -> None:
        self._embedder = embedder
        self._chat = chat
        self._vector_store = vector_store
        self._typing_provider = typing_provider
        self._item_types = item_types
        self._item_types_version = item_types_version
        self._top_k = top_k
        self._recency_weight = recency_weight
        self._diversity_weight = diversity_weight
        self._fetch_multiplier = fetch_multiplier
        self._prompt_hash = hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()[:8]

    def estimate_products(self, product_names: list[str], user_id: str) -> EstimateBatch:
        if not product_names:
            return EstimateBatch(estimates=[])
        logger.info("estimate request from %s for %d products prompt=%s",
                    user_id, len(product_names), self._prompt_hash)
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
        reconciled = [_snap_estimate(est) for est in reconciled]
        logger.info("estimate done for %s: %d estimates in %.1fs prompt=%s",
                    user_id, len(reconciled), time.monotonic() - start, self._prompt_hash)
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
            fetch_n = self._top_k * self._fetch_multiplier
            for where in queries:
                for hit in self._vector_store.query(embedding, fetch_n, where=where):
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

        def base(h: _Hit) -> float:
            similarity = 1.0 - h.distance
            pub = int(h.metadata.get("published_at", 0) or 0)
            if span > 0 and pub > 0:
                recency = (pub - min_pub) / span
            else:
                recency = 0.0
            return similarity + self._recency_weight * recency

        def key_of(h: _Hit) -> tuple[str, int]:
            return (str(h.metadata.get("item_type", "") or ""),
                    int(h.metadata.get("price_jpy", 0) or 0))

        base_by_id = {h.id: base(h) for h in hits}
        selected: list[_Hit] = []
        selected_keys: list[tuple[str, int]] = []
        remaining = list(hits)
        while remaining:
            best_i = 0
            best_score = float("-inf")
            for i, h in enumerate(remaining):
                dup = selected_keys.count(key_of(h))
                adjusted = base_by_id[h.id] - self._diversity_weight * dup
                if adjusted > best_score:
                    best_score = adjusted
                    best_i = i
            picked = remaining.pop(best_i)
            selected.append(picked)
            selected_keys.append(key_of(picked))
        return selected

    def _format_reference(self, hit: _Hit) -> str:
        m = hit.metadata
        pub = int(m.get("published_at", 0) or 0)
        date = "?" if pub == 0 else datetime.fromtimestamp(pub, tz=timezone.utc).strftime("%Y-%m")
        item_name = str(m.get("item_name") or "")
        product_title = str(m.get("product_title") or "")
        fields = [item_name, str(m.get("item_type") or "")]
        if product_title and product_title != item_name:
            fields.append(product_title)
        fields += [f"¥{m.get('price_jpy')}", date, str(m.get("store_id") or "")]
        line = "- " + " | ".join(fields)
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
