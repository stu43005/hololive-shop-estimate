"""Talent-seed miner.

Default: fetch the authoritative talent display-name list from each store's
official collection pages (hololive /pages/talent, vspo /collections/members
and /collections/en-members), and print a YAML 'talents:' block for human
review before updating stores_config.yaml.

Legacy: `--chroma [PATH]` mines talent tokens heuristically from the live
ChromaDB 'products' collection (single differing token within same-price
variant groups).

Usage:
    .venv/bin/python -m scripts.mine_talents
    .venv/bin/python -m scripts.mine_talents --chroma [chroma_path]
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
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


@dataclass(frozen=True)
class StoreSource:
    store_id: str
    base_url: str  # no trailing slash
    listing_urls: tuple[str, ...]
    denylist_exact: frozenset[str]
    denylist_prefixes: tuple[str, ...]


STORE_SOURCES: tuple[StoreSource, ...] = (
    StoreSource(
        store_id="hololive",
        base_url="https://shop.hololivepro.com",
        listing_urls=("https://shop.hololivepro.com/pages/talent",),
        denylist_exact=frozenset({
            "all", "flow-glow", "friend-a", "uproar",
            "shi-wu-suo-sutatuhu", "zu-ye-sheng",
        }),
        denylist_prefixes=("hololive", "holostars"),
    ),
    StoreSource(
        store_id="vspo",
        base_url="https://store.vspo.jp",
        listing_urls=(
            "https://store.vspo.jp/collections/members",
            "https://store.vspo.jp/collections/en-members",
        ),
        denylist_exact=frozenset({
            "all", "members", "en-members", "apparel", "goods", "others",
            "digitalgoods", "event-goods", "goods-accessories",
            "tapestry-poster", "voice",
        }),
        denylist_prefixes=(),
    ),
)


def fetch_text(url: str) -> str:  # pragma: no cover
    import requests

    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def fetch_collection_title(base_url: str, handle: str) -> str | None:  # pragma: no cover
    import requests

    resp = requests.get(f"{base_url}/collections/{handle}.json", timeout=30)
    if resp.status_code != 200:
        return None
    try:
        payload = cast(object, resp.json())
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    payload_d = cast(dict[str, object], payload)
    collection = payload_d.get("collection")
    if not isinstance(collection, dict):
        return None
    collection_d = cast(dict[str, object], collection)
    title = collection_d.get("title")
    if not isinstance(title, str):
        return None
    return title


def mine_talents_from_stores(sources: tuple[StoreSource, ...]) -> set[str]:  # pragma: no cover
    names: set[str] = set()
    for source in sources:
        handles: set[str] = set()
        for url in source.listing_urls:
            handles |= extract_collection_handles(fetch_text(url))
        kept = filter_handles(
            handles, source.denylist_exact, source.denylist_prefixes
        )
        for handle in sorted(kept):
            title = fetch_collection_title(source.base_url, handle)
            if title is None:
                continue
            name = normalize_talent_name(title)
            if name:
                names.add(name)
    return names


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
    for doc in res["documents"] or []:
        variants: list[tuple[str, float]] = []
        for line in doc.splitlines():
            m = row_re.match(line)
            if m and m.group(1) != "Title" and set(m.group(1)) != {"-"}:
                variants.append((m.group(1).strip(), float(m.group(2))))
        if variants:
            out.append(variants)
    return out


def main() -> None:  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(
        description="Mine talent display names for stores_config.yaml."
    )
    _ = parser.add_argument(
        "--chroma",
        nargs="?",
        const="chroma",
        default=None,
        metavar="PATH",
        help=(
            "Use the legacy ChromaDB heuristic against the 'products' collection "
            "at PATH (default 'chroma') instead of the live collection pages."
        ),
    )
    args = parser.parse_args()
    chroma_path = cast("str | None", args.chroma)

    if chroma_path is not None:
        names = sorted(mine_talents(_load_docs_from_chroma(chroma_path)))
    else:
        names = sorted(mine_talents_from_stores(STORE_SOURCES))

    print("talents:")
    for name in names:
        print(f"  - {name}")


if __name__ == "__main__":  # pragma: no cover
    main()
