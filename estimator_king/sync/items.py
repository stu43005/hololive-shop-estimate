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

_TOKEN_SPLIT = re.compile(r"[\s（）()]+")  # split on whitespace + full/half-width parens
_MAX_TALENT_TOKENS = 4  # greedy n-gram cap (spaced 姓 名 names are 2 tokens; cap covers rare longer)
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


@dataclass(frozen=True)
class DecomposeResult:
    items: list[ProductItem]
    excluded_set: int
    excluded_zero: int


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


def _talents_nospace(talents: frozenset[str]) -> dict[str, str]:
    """Map whitespace-stripped normalized talent -> original, for space-insensitive matching."""
    return {normalize_text(t).replace(" ", ""): t for t in talents}


def _canonical_key(residual: str, talents_nospace: dict[str, str]) -> tuple[str, list[str]]:
    """Drop talent tokens (greedy longest n-gram, whitespace-insensitive); return
    (canonical_key, removed_talent_originals)."""
    toks = [t for t in _TOKEN_SPLIT.split(normalize_text(residual)) if t]
    kept: list[str] = []
    removed: list[str] = []
    i = 0
    while i < len(toks):
        matched = False
        for j in range(min(len(toks), i + _MAX_TALENT_TOKENS), i, -1):  # longest first
            cand = "".join(toks[i:j])
            if cand in talents_nospace:
                removed.append(talents_nospace[cand])
                i = j
                matched = True
                break
        if not matched:
            kept.append(toks[i])
            i += 1
    return " ".join(kept), removed


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


def decompose_items(snapshot: ProductSnapshot, *, talents: frozenset[str]) -> DecomposeResult:
    # Step 1+2: keep non-SET, non-zero variants as (residual, price_int, variant_id).
    kept: list[tuple[str, int, int]] = []
    excluded_set = 0
    excluded_zero = 0
    for v in snapshot.variants:
        residual, prefix = _strip_prefix(v.title)
        if prefix is not None and prefix.startswith("セット"):
            excluded_set += 1
            continue
        price = _price_to_int(v.price)
        if price is None or price == 0:
            excluded_zero += 1
            continue
        kept.append((residual, price, v.variant_id))

    # Step 3: talent-gated canonical-key dedup, grouped by price.
    talents_nospace = _talents_nospace(talents)
    by_price: dict[int, list[tuple[str, int]]] = defaultdict(list)
    for residual, price, vid in kept:
        by_price[price].append((residual, vid))

    @dataclass
    class _Item:
        residual: str | None  # None => merged group (name from key, or product title if key empty)
        key: str              # group canonical key (common part); "" for non-merged items
        price: int
        variant_ids: list[int]
        talents: list[str]

    raw_items: list[_Item] = []
    for price, members in by_price.items():
        groups: dict[str, list[tuple[str, int, list[str]]]] = defaultdict(list)
        for residual, vid in members:
            key, removed = _canonical_key(residual, talents_nospace)
            groups[key].append((residual, vid, removed))
        for key, group in groups.items():
            removed_any = any(r for _, _, r in group)
            if len(group) >= 2 and removed_any:
                merged_talents: list[str] = []
                for _, _, removed in group:
                    for t in removed:
                        if t not in merged_talents:
                            merged_talents.append(t)
                raw_items.append(_Item(
                    residual=None, key=key, price=price,
                    variant_ids=[vid for _, vid, _ in group], talents=merged_talents,
                ))
            else:
                for residual, vid, _ in group:
                    raw_items.append(_Item(residual=residual, key="", price=price,
                                           variant_ids=[vid], talents=[]))

    # Step 4: naming (two branches) + snippet.
    items: list[ProductItem] = []
    for ri in raw_items:
        if ri.residual is None:
            name = ri.key.strip() or snapshot.title   # merged: common part; product title if key empty
        else:
            name = normalize_text(ri.residual)         # non-merged: normalized residual
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
    return DecomposeResult(
        items=items, excluded_set=excluded_set, excluded_zero=excluded_zero)
