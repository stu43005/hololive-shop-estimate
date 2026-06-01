"""Two-tier item-type classification orchestration.

Tier 1: controlled-vocabulary longest-substring match (zero LLM, deterministic).
Tier 2: small-model fallback (TypingProvider.classify_via_llm), with a SQLite
cache keyed on (normalized text, item_types_version). classify_item always
returns one type ('その他' floor); classify_query may return 0..N types.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Protocol

from estimator_king.crawler.snapshot import normalize_text

logger = logging.getLogger(__name__)

OTHER = "その他"


@dataclass(frozen=True)
class TypeDecision:
    item_type: str
    source: str  # "vocab" | "cache" | "llm"


class _TypingProvider(Protocol):
    def classify_via_llm(self, text: str, item_types: list[str]) -> str: ...


class _Cache(Protocol):
    def get_cached_type(self, text_hash: str) -> str | None: ...
    def put_cached_type(self, text_hash: str, item_type: str, version: int,
                        text_sample: str) -> None: ...


def _vocab_hits(text: str, item_types: list[str]) -> list[str]:
    """Controlled-vocab substring matches, longest first."""
    norm = normalize_text(text)
    hits = [t for t in item_types if t and t in norm]
    hits.sort(key=len, reverse=True)
    return hits


def _cache_key(text: str, version: int) -> str:
    return hashlib.sha256(f"{normalize_text(text)}:{version}".encode("utf-8")).hexdigest()


def _llm_classify(
    text: str, item_types: list[str], version: int,
    typing_provider: _TypingProvider, repository: _Cache | None,
) -> tuple[str, str]:
    key = _cache_key(text, version)
    if repository is not None:
        cached = repository.get_cached_type(key)
        if cached is not None:
            return cached, "cache"
    try:
        result = typing_provider.classify_via_llm(text, item_types)
    except Exception:
        logger.exception("typing LLM classify failed; defaulting to %s", OTHER)
        result = OTHER
    if result not in item_types:
        result = OTHER
    if repository is not None:
        repository.put_cached_type(key, result, version, normalize_text(text))
    return result, "llm"


def classify_item(
    text: str, *, item_types: list[str], item_types_version: int,
    typing_provider: _TypingProvider, repository: _Cache | None,
) -> TypeDecision:
    hits = _vocab_hits(text, item_types)
    if len(hits) == 1:
        return TypeDecision(hits[0], "vocab")
    # zero or multiple hits -> LLM picks exactly one (cached on the index side).
    item_type, source = _llm_classify(
        text, item_types, item_types_version, typing_provider, repository)
    return TypeDecision(item_type, source)


def classify_query(
    text: str, *, item_types: list[str], item_types_version: int,
    typing_provider: _TypingProvider, repository: _Cache | None = None,
) -> list[str]:
    hits = _vocab_hits(text, item_types)
    if hits:
        return hits  # one or many -> query each; no LLM
    item_type, _ = _llm_classify(
        text, item_types, item_types_version, typing_provider, repository)
    return [] if item_type == OTHER else [item_type]
