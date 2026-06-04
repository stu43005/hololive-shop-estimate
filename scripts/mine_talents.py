"""One-time talent-seed miner. Reads the live ChromaDB 'products' collection,
finds tokens that vary as the single differing token within same-price variant
groups (these are reliably talent names), and prints a YAML 'talents:' list for
human review before adding to stores_config.yaml.

Usage: .venv/bin/python -m scripts.mine_talents [chroma_path]
"""

from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from typing import cast


_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")
_HANDLE_RE = re.compile(r'href="/collections/([a-z0-9._-]+)"')


def extract_collection_handles(html: str) -> set[str]:
    """Extract collection handles from anchor hrefs, skipping CDN image paths."""
    handles: set[str] = set()
    for handle in cast(list[str], _HANDLE_RE.findall(html)):
        if handle.endswith(_IMAGE_SUFFIXES):
            continue
        handles.add(handle)
    return handles


def filter_handles(
    handles: set[str],
    denylist_exact: frozenset[str],
    denylist_prefixes: tuple[str, ...],
) -> set[str]:
    """Drop group/category handles by exact match or handle prefix."""
    kept: set[str] = set()
    for handle in handles:
        if handle in denylist_exact:
            continue
        if handle.startswith(denylist_prefixes):
            continue
        kept.add(handle)
    return kept


def normalize_talent_name(title: str) -> str:
    """Collapse a collection title into a single whitespace-free token.

    No-arg str.split() splits on all Unicode whitespace (ASCII space, U+3000
    full-width space, tab, newline), so joining removes every kind of space.
    """
    return "".join(title.split())


def mine_talents(
    docs: list[list[tuple[str, float]]], *, min_freq: int = 20
) -> set[str]:
    """docs: per-product list of (variant_title, price). Returns talent candidates."""
    counts: Counter[str] = Counter()
    for variants in docs:
        by_price: dict[float, list[str]] = defaultdict(list)
        for title, price in variants:
            residual = title.split(" / ", 1)[1].strip() if " / " in title else title.strip()
            by_price[price].append(residual)
        for residuals in by_price.values():
            if len(residuals) < 2:
                continue
            token_sets = [r.split() for r in residuals]
            common = set(token_sets[0])
            for ts in token_sets[1:]:
                common &= set(ts)
            for ts in token_sets:
                unique = [t for t in ts if t not in common]
                if len(unique) == 1:
                    counts[unique[0]] += 1
    return {
        tok for tok, freq in counts.items()
        if freq >= min_freq and "ver." not in tok and "限定" not in tok and not tok.isdigit()
    }


def _load_docs_from_chroma(path: str) -> list[list[tuple[str, float]]]:  # pragma: no cover
    import chromadb

    client = chromadb.PersistentClient(path=path)
    col = client.get_collection("products")
    res = col.get(include=["documents"])
    row_re = re.compile(r"^\|\s*(.+?)\s*\|\s*([\d.]+)\s*\|$")
    out: list[list[tuple[str, float]]] = []
    for doc in res["documents"]:
        variants: list[tuple[str, float]] = []
        for line in doc.splitlines():
            m = row_re.match(line)
            if m and m.group(1) != "Title" and set(m.group(1)) != {"-"}:
                variants.append((m.group(1).strip(), float(m.group(2))))
        if variants:
            out.append(variants)
    return out


def main() -> None:  # pragma: no cover
    path = sys.argv[1] if len(sys.argv) > 1 else "chroma"
    talents = sorted(mine_talents(_load_docs_from_chroma(path)))
    print("talents:")
    for t in talents:
        print(f"  - {t}")


if __name__ == "__main__":  # pragma: no cover
    main()
