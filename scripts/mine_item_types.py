"""Item-type vocabulary miner.

Surface candidate new item types from the live `item_type_cache` table. Every
item name the two-tier classifier (estimator_king/sync/typing.py) could not map
to a known type lands in the 'その他' bucket; recurring product-type words hide
in there. `repository.list_other_typed` dumps those raw samples, but the result
is dominated by noise (talent names, event/campaign words, version markers), so
a human or model has to wade through thousands of strings.

This script does the coarse pass instead: pull the distinct 'その他' samples,
take each sample's trailing token (Japanese product names put the item type
last), drop the obvious noise, aggregate by frequency, and attach a few example
samples. The output is a compact ranked JSON list meant to be handed to a small
model for semantic clustering (merging variants, deduping against existing
types) before a human picks what to add. It is strictly read-only — it opens the
SQLite DB in read-only mode and never writes or migrates.

Usage:
    .venv/bin/python -m scripts.mine_item_types
    .venv/bin/python -m scripts.mine_item_types --min-freq 5 --examples 4
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import cast

# Substrings that mark a token as an event / campaign / release-batch label
# rather than a product type. Tokens containing any of these are dropped before
# aggregation — they recur often but are never the thing being sold.
_NOISE_SUBSTRINGS: tuple[str, ...] = (
    "記念", "周年", "通販", "再販", "販売", "予約", "衣装", "セット",
    "期生", "アニバ", "謹賀", "新年", "活動", "祭", "フェア", "フェス",
    "キャンペーン", "くじ", "ガチャ", "AGF", "コミケ", "コミックマーケット",
    "HoneyWorks", "ぶいすぽ", "クリスマス", "ハロウィン", "バレンタイン",
    "ゲーマーズ", "vol", "Vol", "ver", "Ver", "VOL", "VER",
)
# Brackets / quotes wrap title-specific names (song titles, event names), never
# a bare product type.
_BRACKET_CHARS = "「」『』【】〔〕（）()\"'"


def trailing_token(sample: str) -> str:
    """Return the last whitespace-delimited token of a sample (the type slot).

    Samples are stored already normalized (single ASCII spaces between tokens),
    so a plain whitespace split is enough. Empty samples yield ''.
    """
    parts = sample.split()
    return parts[-1] if parts else ""


def is_noise_phrase(phrase: str, talents_nospace: frozenset[str]) -> bool:
    """True if a candidate token is noise and should not be aggregated.

    Filters the categories that reliably are not product types: too-short
    tokens, talent names, anything carrying a digit (years, vol.N, batch
    numbers), bracketed/quoted title fragments, and the event/campaign
    substrings above. Semantic dedup against existing item types is left to the
    downstream model, which has the full known-type list and can judge nuance a
    substring check cannot.
    """
    if len(phrase) < 2:
        return True
    if phrase.replace(" ", "") in talents_nospace:
        return True
    if any(ch.isdigit() for ch in phrase):
        return True
    if any(ch in phrase for ch in _BRACKET_CHARS):
        return True
    if any(sub in phrase for sub in _NOISE_SUBSTRINGS):
        return True
    return False


@dataclass(frozen=True)
class Candidate:
    phrase: str
    frequency: int
    examples: tuple[str, ...]


def mine_candidates(
    samples: list[str],
    *,
    talents: frozenset[str],
    min_freq: int = 3,
    max_examples: int = 3,
) -> list[Candidate]:
    """Aggregate 'その他' samples into ranked candidate type tokens.

    Counts how many distinct samples end with each trailing token, keeps tokens
    seen at least `min_freq` times, and attaches up to `max_examples` example
    samples per token for context. Sorted by frequency desc, then phrase for a
    stable order.
    """
    talents_nospace = frozenset(t.replace(" ", "") for t in talents)
    counts: Counter[str] = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    for sample in samples:
        phrase = trailing_token(sample)
        if is_noise_phrase(phrase, talents_nospace):
            continue
        counts[phrase] += 1
        if len(examples[phrase]) < max_examples:
            examples[phrase].append(sample)
    candidates = [
        Candidate(phrase=phrase, frequency=freq, examples=tuple(examples[phrase]))
        for phrase, freq in counts.items()
        if freq >= min_freq
    ]
    candidates.sort(key=lambda c: (-c.frequency, c.phrase))
    return candidates


def load_other_samples(db_path: str) -> list[str]:  # pragma: no cover
    """Read distinct 'その他' item names from item_type_cache, read-only.

    Opens the DB with `mode=ro` (URI) so the miner can never write or trigger a
    schema migration — it must not perturb the single-writer crawler's DB.
    """
    uri = f"file:{db_path}?mode=ro" if not db_path.startswith("file:") else db_path
    conn = sqlite3.connect(uri, uri=True)
    try:
        rows = conn.execute(
            "SELECT DISTINCT text_sample FROM item_type_cache"
            " WHERE item_type = 'その他'"
        ).fetchall()
    finally:
        conn.close()
    return [str(r[0]) for r in rows]


def main() -> None:  # pragma: no cover
    from estimator_king.config_schema import AppConfig

    parser = argparse.ArgumentParser(
        description="Mine candidate item types from the 'その他' bucket."
    )
    _ = parser.add_argument(
        "--config", default="stores_config.yaml", help="path to stores_config.yaml"
    )
    _ = parser.add_argument(
        "--db", default=None, help="SQLite path (default: config database_path)"
    )
    _ = parser.add_argument(
        "--min-freq", type=int, default=3, help="minimum trailing-token frequency"
    )
    _ = parser.add_argument(
        "--examples", type=int, default=3, help="example samples per candidate"
    )
    args = parser.parse_args()

    config = AppConfig.from_yaml(cast(str, args.config))
    db_path = cast("str | None", args.db) or config.database_path
    samples = load_other_samples(db_path)
    candidates = mine_candidates(
        samples,
        talents=frozenset(config.talents),
        min_freq=cast(int, args.min_freq),
        max_examples=cast(int, args.examples),
    )

    output = {
        "db_path": db_path,
        "total_other_samples": len(samples),
        "known_item_types": sorted(config.item_types),
        "candidates": [
            {"phrase": c.phrase, "frequency": c.frequency, "examples": list(c.examples)}
            for c in candidates
        ],
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":  # pragma: no cover
    main()
