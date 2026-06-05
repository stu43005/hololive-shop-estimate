"""Run the collection-page talent miner and add any newly-found talents to
stores_config.yaml.

Add-only: existing entries are never removed, so graduated talents (no live
collection but still appearing in product names) and any hand-added names are
preserved. The `talents:` block is rewritten at the string level — only the
`talents:` line and its `  - ` entries are replaced, so surrounding comments,
indentation, and other keys keep their exact formatting (no `yaml.dump`).

Backs the `update-talents` skill.

Usage: .venv/bin/python -m scripts.update_talents_config
"""

from __future__ import annotations

import asyncio
import subprocess
from typing import cast

import yaml

from scripts.mine_talents import mine_from_stores

_CONFIG_PATH = "stores_config.yaml"


def merge_talents(existing: set[str], mined: set[str]) -> tuple[list[str], list[str]]:
    """Return (union_sorted, added_sorted).

    Add-only: the union always keeps every existing name; `added` is the names
    present in `mined` but not already in `existing`.
    """
    union = sorted(existing | mined)
    added = sorted(mined - existing)
    return union, added


def splice_talents_block(lines: list[str], union: list[str]) -> list[str]:
    """Replace the `talents:` line and its `  - ` entries with `union`.

    Everything else (comments above the block, the next top-level key, blank
    lines) is left byte-for-byte unchanged.
    """
    try:
        start = next(i for i, ln in enumerate(lines) if ln.rstrip("\n") == "talents:")
    except StopIteration:
        raise ValueError("no top-level 'talents:' line found in config") from None
    end = start + 1
    while end < len(lines) and lines[end].startswith("  - "):
        end += 1
    block = ["talents:\n"] + [f"  - {name}\n" for name in union]
    return lines[:start] + block + lines[end:]


def _read_existing_talents(path: str) -> set[str]:  # pragma: no cover
    with open(path, encoding="utf-8") as f:
        data = cast(object, yaml.safe_load(f))
    if not isinstance(data, dict):
        return set()
    talents = cast("dict[str, object]", data).get("talents")
    if not isinstance(talents, list):
        return set()
    return {t for t in cast("list[object]", talents) if isinstance(t, str)}


def _write_config(path: str, union: list[str]) -> None:  # pragma: no cover
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(splice_talents_block(lines, union))


def _commit_config(path: str, added: list[str]) -> None:  # pragma: no cover
    # Commit only this path so unrelated staged/working changes are untouched.
    message = f"chore(config): add {len(added)} mined talent(s) to dedup list"
    _ = subprocess.run(["git", "commit", "-m", message, "--", path], check=True)


def main() -> None:  # pragma: no cover
    mined = asyncio.run(mine_from_stores())
    existing = _read_existing_talents(_CONFIG_PATH)
    union, added = merge_talents(existing, mined)

    if not added:
        print(f"talents up to date ({len(existing)} entries); nothing to add.")
        return

    _write_config(_CONFIG_PATH, union)
    _commit_config(_CONFIG_PATH, added)
    print(f"added {len(added)} talent(s) (total {len(union)}):")
    for name in added:
        print(f"  + {name}")


if __name__ == "__main__":  # pragma: no cover
    main()
