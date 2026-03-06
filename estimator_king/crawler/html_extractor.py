"""HTML extraction utilities."""

# pyright: reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false
# pyright: reportUnknownParameterType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable

from bs4 import BeautifulSoup  # pyright: ignore[reportMissingImports]
from bs4.element import NavigableString, Tag  # pyright: ignore[reportMissingImports]

HEADING_TAGS: tuple[str, ...] = ("h2", "h3", "h4")
SKIP_TEXT_IN_PARENTS: tuple[str, ...] = ("script", "style", "noscript")

DETAIL_SECTION_KEYS: tuple[str, ...] = (
    "セット詳細",
    "グッズ詳細",
    "Set Details",
    "Merch details",
)


def _normalize_spaces(text: str) -> str:
    text = text.replace("\\n", " ")
    text = text.replace("\\r", " ")
    text = text.replace("\\t", " ")
    text = re.sub(r"\\u00a0", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\\u3000", " ", text, flags=re.IGNORECASE)

    text = text.replace("\u00a0", " ")  # nbsp
    text = text.replace("\u3000", " ")  # Japanese full-width space
    text = re.sub(r"\s+", " ", text, flags=re.UNICODE)
    return text.strip()


def _normalize_for_match(text: str) -> str:
    return _normalize_spaces(text).casefold()


def _heading_level(tag: Tag) -> int:
    try:
        return int(str(tag.name)[1:])
    except Exception:
        return 99


def _iter_next_headings(tag: Tag) -> Iterable[Tag]:
    for nxt in tag.find_all_next(HEADING_TAGS):
        if isinstance(nxt, Tag):
            yield nxt


def _find_boundary_heading(start_heading: Tag) -> Tag | None:
    start_level = _heading_level(start_heading)
    for nxt in _iter_next_headings(start_heading):
        if _heading_level(nxt) <= start_level:
            return nxt
    return None


def _extract_text_between(start_heading: Tag, boundary_heading: Tag | None) -> str:
    parts: list[str] = []

    for el in start_heading.next_elements:
        if boundary_heading is not None and el is boundary_heading:
            break

        if not isinstance(el, NavigableString):
            continue

        parents = tuple(getattr(el, "parents", ()))

        if start_heading in parents:
            continue

        if any((getattr(p, "name", None) in SKIP_TEXT_IN_PARENTS) for p in parents):
            continue

        txt = _normalize_spaces(str(el))
        if txt:
            parts.append(txt)

    return _normalize_spaces(" ".join(parts))


def _extract_details_content(details_tag: Tag) -> str:
    """Extract text content from a <details> block, excluding its <summary>."""
    parts: list[str] = []
    for child in details_tag.children:
        if isinstance(child, Tag) and child.name == "summary":
            continue
        if isinstance(child, Tag):
            txt = _normalize_spaces(child.get_text(" ", strip=True))
            if txt:
                parts.append(txt)
        elif isinstance(child, NavigableString):
            txt = _normalize_spaces(str(child))
            if txt:
                parts.append(txt)
    return _normalize_spaces(" ".join(parts))


def extract_detail_sections(html: str) -> dict[str, str]:
    soup = BeautifulSoup(html or "", "lxml")

    key_matchers = {key: _normalize_for_match(key) for key in DETAIL_SECTION_KEYS}

    blocks_by_key: dict[str, list[str]] = defaultdict(list)

    # --- Pass 1: heading-based extraction (h2/h3/h4) ---
    for heading in soup.find_all(HEADING_TAGS):
        if not isinstance(heading, Tag):
            continue
        heading_text = _normalize_for_match(heading.get_text(" ", strip=True))
        if not heading_text:
            continue

        matched_keys = [
            key for key, matcher in key_matchers.items() if matcher in heading_text
        ]
        if not matched_keys:
            continue

        boundary = _find_boundary_heading(heading)
        section_text = _extract_text_between(heading, boundary)

        for key in matched_keys:
            blocks_by_key[key].append(section_text)

    # --- Pass 2: <details>/<summary> extraction ---
    for details_tag in soup.find_all("details"):
        if not isinstance(details_tag, Tag):
            continue
        summary_tag = details_tag.find("summary", recursive=False)
        if not isinstance(summary_tag, Tag):
            continue
        summary_text = _normalize_for_match(summary_tag.get_text(" ", strip=True))
        if not summary_text:
            continue

        matched_keys = [
            key for key, matcher in key_matchers.items() if matcher in summary_text
        ]
        if not matched_keys:
            continue

        section_text = _extract_details_content(details_tag)

        for key in matched_keys:
            if key not in blocks_by_key:
                blocks_by_key[key].append(section_text)

    if not blocks_by_key:
        import logging

        logging.debug(
            f"extract_detail_sections: No blocks found in HTML (len={len(html or '')})"
        )
        return {}

    out: dict[str, str] = {}
    for key in DETAIL_SECTION_KEYS:
        if key not in blocks_by_key:
            continue
        raw_blocks = blocks_by_key[key]
        non_empty = [b for b in raw_blocks if b]
        if non_empty:
            out[key] = "\n\n".join(non_empty)
        else:
            out[key] = ""
    return out
