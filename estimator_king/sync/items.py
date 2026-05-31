"""Decompose a ProductSnapshot into priceable items.

Pipeline per product: drop SET / ¥0 variants → talent-gated canonical-key dedup
→ name each item → best-effort spec-snippet extraction. published_at is carried
from the snapshot onto every item of that product.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from collections import defaultdict

from estimator_king.crawler.snapshot import ProductSnapshot, normalize_text

_SIZE_RE = re.compile(
    r"(^|[\s/])(XX?[SML]|[SML]|フリー)?サイズ|^(XX?[SML]|[SML])([\s/]|$)|フリーサイズ"
)
_SEGMENT_SPLIT = re.compile(r"[・◇\n]")


@dataclass(frozen=True)
class ProductItem:
    product_id: int
    product_title: str
    item_name: str
    price_jpy: int
    source_variant_ids: tuple[int, ...]
    talents: tuple[str, ...]
    detail_snippet: str
    published_at: int


def _strip_prefix(title: str) -> tuple[str, str | None]:
    """Return (residual, option_prefix) from a Shopify variant title 'X / Y'."""
    if " / " in title:
        prefix, rest = title.split(" / ", 1)
        return rest.strip(), prefix.strip()
    return title.strip(), None


def _price_to_int(price: str) -> int | None:
    try:
        return int(float(price))
    except (TypeError, ValueError):
        return None


def _meaningful_tokens(text: str) -> list[str]:
    return [t for t in normalize_text(text).split() if len(t) >= 2]  # drop single-char tokens (CJK particles/punctuation noise)


def _canonical_key(residual: str, talents: frozenset[str]) -> tuple[str, list[str]]:
    """Drop talent tokens; return (canonical_key, removed_talent_tokens)."""
    kept: list[str] = []
    removed: list[str] = []
    for tok in normalize_text(residual).split():
        if tok in talents:
            removed.append(tok)
        else:
            kept.append(tok)
    return " ".join(kept), removed


def _is_option_value(residual: str) -> bool:
    norm = normalize_text(residual)
    return len(norm) < 4 or bool(_SIZE_RE.search(norm))  # too short to be a standalone item name (size/color option like "黒 M")


def _extract_snippet(item_name: str, html_details: dict[str, str], talents: frozenset[str]) -> str:
    cores: list[str] = [normalize_text(item_name)]
    if " - " in item_name:
        cores.append(item_name.split(" - ")[0].strip())
        cores.append(item_name.split(" - ")[-1].strip())
    stripped = " ".join(t for t in normalize_text(item_name).split() if t not in talents)
    if stripped:
        cores.append(stripped)
    item_tokens = set(_meaningful_tokens(item_name))

    best = ""
    best_score = 0
    for text in html_details.values():
        for seg in _SEGMENT_SPLIT.split(text):
            seg = seg.strip()
            if not seg:
                continue
            score = 0
            for core in cores:
                if len(core) >= 4 and core in seg:  # ignore trivially short cores to avoid false matches
                    score = max(score, len(core))
            if score == 0:
                overlap = len(item_tokens & set(_meaningful_tokens(seg)))
                if overlap >= 2:  # require ≥2 shared tokens for a fallback match
                    score = overlap
            if score > best_score:
                best_score = score
                best = seg
    return best


def decompose_items(snapshot: ProductSnapshot, *, talents: frozenset[str]) -> list[ProductItem]:
    # Step 1+2: keep non-SET, non-zero variants as (residual, price_int, variant_id).
    kept: list[tuple[str, int, int]] = []
    for v in snapshot.variants:
        residual, prefix = _strip_prefix(v.title)
        if prefix is not None and prefix.startswith("セット"):
            continue
        price = _price_to_int(v.price)
        if price is None or price == 0:
            continue
        kept.append((residual, price, v.variant_id))

    # Step 3: talent-gated canonical-key dedup, grouped by price.
    by_price: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for residual, price, vid in kept:
        by_price[price].append((residual, vid))

    @dataclass
    class _Item:
        residual: str | None  # None => whole-group merge (name from product title)
        price: int
        variant_ids: list[int]
        talents: list[str]

    raw_items: list[_Item] = []
    for price, members in by_price.items():
        groups: dict[str, list[tuple[str, int, list[str]]]] = defaultdict(list)
        for residual, vid in members:
            key, removed = _canonical_key(residual, talents)
            groups[key].append((residual, vid, removed))
        for key, group in groups.items():
            removed_any = any(r for _, _, r in group)
            if len(group) >= 2 and key.strip() and removed_any:
                merged_talents: list[str] = []
                for _, _, removed in group:
                    for t in removed:
                        if t not in merged_talents:
                            merged_talents.append(t)
                raw_items.append(_Item(
                    residual=None, price=price,
                    variant_ids=[vid for _, vid, _ in group], talents=merged_talents,
                ))
            else:
                for residual, vid, _ in group:
                    raw_items.append(_Item(residual=residual, price=price,
                                           variant_ids=[vid], talents=[]))

    # Step 4: naming (three branches) + snippet.
    # whole product collapsed to one item -> name it by the product title
    whole_product_single = (
        len(raw_items) == 1 and raw_items[0].residual is None and len(raw_items[0].variant_ids) >= 2
    )
    items: list[ProductItem] = []
    for ri in raw_items:
        if ri.residual is None or whole_product_single:
            name = snapshot.title
        elif _is_option_value(ri.residual):
            name = f"{snapshot.title} {normalize_text(ri.residual)}".strip()
        else:
            name = ri.residual
        items.append(ProductItem(
            product_id=snapshot.product_id,
            product_title=snapshot.title,
            item_name=name,
            price_jpy=ri.price,
            source_variant_ids=tuple(ri.variant_ids),
            talents=tuple(ri.talents),
            detail_snippet=_extract_snippet(name, snapshot.html_details, talents),
            published_at=snapshot.published_at,
        ))
    return items
