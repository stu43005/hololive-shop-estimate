# Talent 清單探勘（從官方 collection 頁面）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 重構 `scripts/mine_talents.py`，新增「從 hololive / vspo 官方 collection 頁面抓取權威 talent 顯示名清單」為預設主路徑，輸出 `talents:` YAML 供套用到 `stores_config.yaml`。

**Architecture:** 列表頁 HTML → regex 抽 `/collections/<handle>` → denylist 過濾團體/分類 → 逐一打 `collections/<handle>.json` 取 `title`（即日文顯示名）→ 去空白正規化為單一 token → 兩站合併去重排序 → 印出 YAML。純函式（抽取 / 過濾 / 正規化）與 IO 函式（HTTP）分離；舊 ChromaDB 啟發式路徑保留，改由 `--chroma` 旗標觸發。

**Tech Stack:** Python 3.14、`requests`（已在 requirements，lazy import）、`argparse`、`re`、`pytest`。

---

## File Structure

- **Modify** `scripts/mine_talents.py`：
  - 新增純函式 `extract_collection_handles`、`filter_handles`、`normalize_talent_name`
  - 新增 `StoreSource` dataclass 與 `STORE_SOURCES` 常數
  - 新增 IO 函式 `fetch_text`、`fetch_collection_title`、`mine_talents_from_stores`
  - 重寫 `main()` 為 argparse（預設走新路徑，`--chroma [PATH]` 走舊路徑）
  - 修掉既有 `reportOptionalIterable`（line 51）、移除重構後不再使用的 `import sys`
  - 保留 `mine_talents(docs, *, min_freq=20)` 與 `_load_docs_from_chroma(path)` 行為不變
- **Modify** `tests/test_mine_talents.py`：新增三個純函式的單元測試（既有兩個測試不動）
- **Modify** `stores_config.yaml`：以實跑結果更新 `talents:` 區塊（Task 7）

---

## Task 1: extract_collection_handles 純函式

**Files:**
- Modify: `scripts/mine_talents.py`
- Test: `tests/test_mine_talents.py`

- [ ] **Step 1: Write the failing test**

在 `tests/test_mine_talents.py` 結尾追加（同時更新檔首 import）：

```python
from scripts.mine_talents import extract_collection_handles, mine_talents
```

> 註：檔首原本是 `from scripts.mine_talents import mine_talents`，請替換為上面這行（只加入本 Task 引入的名稱；後續 Task 2/3 會再漸進擴充此 import，確保每個 Task 的 Step 4 都能獨立通過）。

追加測試：

```python
def test_extract_collection_handles_picks_anchors_and_skips_images():
    html = (
        '<a href="/collections/azki">AZKi</a>'
        '<a href="/collections/gawrgura">Gawr Gura</a>'
        '<img src="/collections/azki_thumb_abc.png">'
        '<a href="/collections/foo.jpg">x</a>'
    )
    handles = extract_collection_handles(html)
    assert handles == {"azki", "gawrgura"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_mine_talents.py::test_extract_collection_handles_picks_anchors_and_skips_images -v -o addopts=""`
Expected: FAIL with `ImportError: cannot import name 'extract_collection_handles'`

- [ ] **Step 3: Write minimal implementation**

在 `scripts/mine_talents.py` 中，於 `mine_talents()` 函式定義之前（`re` import 之後）新增：

```python
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")
_HANDLE_RE = re.compile(r'href="/collections/([a-z0-9._-]+)"')


def extract_collection_handles(html: str) -> set[str]:
    """Extract collection handles from anchor hrefs, skipping CDN image paths."""
    handles: set[str] = set()
    for handle in _HANDLE_RE.findall(html):
        if handle.endswith(_IMAGE_SUFFIXES):
            continue
        handles.add(handle)
    return handles
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_mine_talents.py::test_extract_collection_handles_picks_anchors_and_skips_images -v -o addopts=""`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/mine_talents.py tests/test_mine_talents.py
git commit -m "feat(mine-talents): add extract_collection_handles"
```

---

## Task 2: filter_handles 純函式

**Files:**
- Modify: `scripts/mine_talents.py`
- Test: `tests/test_mine_talents.py`

- [ ] **Step 1: Write the failing test**

先把檔首 import 擴充為（加入 `filter_handles`）：

```python
from scripts.mine_talents import (
    extract_collection_handles,
    filter_handles,
    mine_talents,
)
```

再追加測試：

```python
def test_filter_handles_drops_exact_and_prefix_matches():
    handles = {"azki", "hololive_gen0", "holostarsen", "all", "uproar"}
    kept = filter_handles(
        handles,
        frozenset({"all", "uproar"}),
        ("hololive", "holostars"),
    )
    assert kept == {"azki"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_mine_talents.py::test_filter_handles_drops_exact_and_prefix_matches -v -o addopts=""`
Expected: FAIL with `ImportError: cannot import name 'filter_handles'`

- [ ] **Step 3: Write minimal implementation**

在 `extract_collection_handles` 之後新增：

```python
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
        if any(handle.startswith(prefix) for prefix in denylist_prefixes):
            continue
        kept.add(handle)
    return kept
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_mine_talents.py::test_filter_handles_drops_exact_and_prefix_matches -v -o addopts=""`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/mine_talents.py tests/test_mine_talents.py
git commit -m "feat(mine-talents): add filter_handles denylist"
```

---

## Task 3: normalize_talent_name 純函式

**Files:**
- Modify: `scripts/mine_talents.py`
- Test: `tests/test_mine_talents.py`

- [ ] **Step 1: Write the failing test**

先把檔首 import 擴充為（加入 `normalize_talent_name`）：

```python
from scripts.mine_talents import (
    extract_collection_handles,
    filter_handles,
    mine_talents,
    normalize_talent_name,
)
```

再追加測試（注意 `如月　れん` 中間是全形空白 U+3000）：

```python
def test_normalize_talent_name_strips_all_whitespace():
    assert normalize_talent_name("八雲 べに") == "八雲べに"
    assert normalize_talent_name("如月　れん") == "如月れん"
    assert normalize_talent_name("がうる・ぐら") == "がうる・ぐら"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_mine_talents.py::test_normalize_talent_name_strips_all_whitespace -v -o addopts=""`
Expected: FAIL with `ImportError: cannot import name 'normalize_talent_name'`

- [ ] **Step 3: Write minimal implementation**

在 `filter_handles` 之後新增：

```python
def normalize_talent_name(title: str) -> str:
    """Collapse a collection title into a single whitespace-free token.

    No-arg str.split() splits on all Unicode whitespace (ASCII space, U+3000
    full-width space, tab, newline), so joining removes every kind of space.
    """
    return "".join(title.split())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_mine_talents.py::test_normalize_talent_name_strips_all_whitespace -v -o addopts=""`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/mine_talents.py tests/test_mine_talents.py
git commit -m "feat(mine-talents): add normalize_talent_name"
```

---

## Task 4: StoreSource、STORE_SOURCES 與 IO 函式

**Files:**
- Modify: `scripts/mine_talents.py`

無單元測試：這些是設定資料與網路 IO，標 `# pragma: no cover`（與既有 `_load_docs_from_chroma`、`main` 一致）。

- [ ] **Step 1: 在檔首補上 dataclass import**

把檔首的：

```python
from collections import Counter, defaultdict
```

改為（在其上方加入 dataclasses import）：

```python
from collections import Counter, defaultdict
from dataclasses import dataclass
```

- [ ] **Step 2: 新增 StoreSource 與 STORE_SOURCES**

在 `normalize_talent_name` 之後新增：

```python
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
```

- [ ] **Step 3: 新增 IO 函式**

在 `STORE_SOURCES` 之後、`mine_talents` 之前（或檔案稍後處皆可，但需在 `main` 之前）新增：

```python
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
        payload = resp.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    collection = payload.get("collection")
    if not isinstance(collection, dict):
        return None
    title = collection.get("title")
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
```

- [ ] **Step 4: 驗證可匯入（無語法/型別錯）**

Run: `.venv/bin/python -c "import scripts.mine_talents"`
Expected: 無輸出、exit 0

Run: `.venv/bin/basedpyright scripts/mine_talents.py 2>&1 | tail -5`
Expected: 仍只有既有的 1 個 `reportOptionalIterable`（line 51 附近，Task 5 會修掉）；不得新增其他錯誤

- [ ] **Step 5: Commit**

```bash
git add scripts/mine_talents.py
git commit -m "feat(mine-talents): add store sources and collection fetchers"
```

---

## Task 5: 重寫 main() 為 argparse 並清掉既有型別錯

**Files:**
- Modify: `scripts/mine_talents.py`

- [ ] **Step 1: 移除不再使用的 import sys**

刪除檔首的 `import sys` 一行（重寫後的 `main()` 不再使用 `sys.argv`；`argparse` 內部自行處理）。

- [ ] **Step 2: 修掉既有 reportOptionalIterable**

在 `_load_docs_from_chroma` 中，把：

```python
    for doc in res["documents"]:
```

改為：

```python
    for doc in res["documents"] or []:
```

- [ ] **Step 3: 重寫 main()**

把現有 `main()`：

```python
def main() -> None:  # pragma: no cover
    path = sys.argv[1] if len(sys.argv) > 1 else "chroma"
    talents = sorted(mine_talents(_load_docs_from_chroma(path)))
    print("talents:")
    for t in talents:
        print(f"  - {t}")
```

整段替換為：

```python
def main() -> None:  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(
        description="Mine talent display names for stores_config.yaml."
    )
    parser.add_argument(
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

    if args.chroma is not None:
        names = sorted(mine_talents(_load_docs_from_chroma(args.chroma)))
    else:
        names = sorted(mine_talents_from_stores(STORE_SOURCES))

    print("talents:")
    for name in names:
        print(f"  - {name}")
```

同時更新檔首 docstring。把現有 docstring（整段 old_string）：

```python
"""One-time talent-seed miner. Reads the live ChromaDB 'products' collection,
finds tokens that vary as the single differing token within same-price variant
groups (these are reliably talent names), and prints a YAML 'talents:' list for
human review before adding to stores_config.yaml.

Usage: .venv/bin/python -m scripts.mine_talents [chroma_path]
"""
```

替換為：

```python
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
```

- [ ] **Step 4: 型別檢查（整檔應為 0 錯）**

Run: `.venv/bin/basedpyright scripts/mine_talents.py`
Expected: `0 errors, 0 warnings, 0 notes`

- [ ] **Step 5: Lint**

Run: `uvx ruff check scripts/mine_talents.py tests/test_mine_talents.py`
Expected: `All checks passed!`

- [ ] **Step 6: 跑全部 mine_talents 測試**

Run: `.venv/bin/python -m pytest tests/test_mine_talents.py -v -o addopts=""`
Expected: 5 passed（既有 2 + 新增 3）

- [ ] **Step 7: Commit**

```bash
git add scripts/mine_talents.py
git commit -m "feat(mine-talents): default to live collection mining via argparse"
```

---

## Task 6: 驗證舊路徑仍可用（回歸）

**Files:** 無（純驗證）

- [ ] **Step 1: 確認 --chroma 旗標解析正確（不需真資料庫）**

Run: `.venv/bin/python -m scripts.mine_talents --help`
Expected: 輸出含 `--chroma` 選項說明，exit 0

- [ ] **Step 2: 確認 --chroma 會走舊路徑（指向不存在路徑應由 chromadb 報錯，而非 argparse 報錯）**

Run: `.venv/bin/python -m scripts.mine_talents --chroma /tmp/nonexistent_chroma_xyz 2>&1 | tail -3`
Expected: 由 chromadb 拋出的錯誤（例如找不到 collection / 路徑），證明確實進入舊路徑；**不應**是 argparse 的 usage 錯誤

---

## Task 7: 實跑產生清單並更新 stores_config.yaml

**Files:**
- Modify: `stores_config.yaml`

此為 operational verification：需連網實跑，依真實輸出更新設定。

- [ ] **Step 1: 實跑新路徑，輸出存檔供檢視**

Run: `.venv/bin/python -m scripts.mine_talents > /tmp/talents_mined.yaml 2>/tmp/talents_mined.err; echo "exit=$?"; head -5 /tmp/talents_mined.yaml; wc -l /tmp/talents_mined.yaml`
Expected: exit=0；`/tmp/talents_mined.yaml` 首行為 `talents:`，其後每行 `  - <name>`；行數約 180–230（兩站合併去重後的個人成員數量級）

- [ ] **Step 2: 人工審視輸出，抓漏網的團體/分類**

審視 `/tmp/talents_mined.yaml`，逐一確認沒有非個人項目漏進來（例如英數字分類名、團體名、`ボイス`/`グッズ` 類）。若發現漏網 handle：
- 回到 `scripts/mine_talents.py` 的對應 `StoreSource.denylist_exact` 補上該 handle
- 重跑 Step 1
- 重新審視，直到清單只剩個人成員顯示名
- 補 denylist 後需 commit：`git add scripts/mine_talents.py && git commit -m "fix(mine-talents): deny <handle> leaking into talents"`

- [ ] **Step 3: 用輸出取代 stores_config.yaml 的 talents: 區塊**

把 `stores_config.yaml` 中現有 `talents:`（含其下所有 `  - …` 行，到下一個頂層鍵 `estimator:` 之前；保留 `# Talent names …` 與 `# Bump when…`／`item_types_version:` 等非 talents 的註解與鍵不動）整段，替換為 `/tmp/talents_mined.yaml` 的內容。

> 注意：`talents:` 上方的註解（`# Talent names (data-mined: …)`）描述的是舊挖掘法，請一併更新為新來源說明，例如：
> ```yaml
> # Talent display names (mined from official store collection pages via
> # scripts/mine_talents.py). Used for talent-gated dedup. Re-run the miner
> # and replace this block when rosters change.
> talents:
> ```

- [ ] **Step 4: 驗證 YAML 可被設定載入器解析**

Run: `.venv/bin/python -c "from estimator_king.config_schema import AppConfig; c = AppConfig.from_yaml('stores_config.yaml'); print('talents:', len(c.talents))"`
Expected: 印出 `talents: <N>`（N 與 Step 1 行數−1 相符），無例外

- [ ] **Step 5: 確認舊清單中的關鍵成員仍在新清單**

Run: `.venv/bin/python -c "from estimator_king.config_schema import AppConfig; c = AppConfig.from_yaml('stores_config.yaml'); missing = [t for t in ['兎田ぺこら','がうる・ぐら','星街すいせい','八雲べに','如月れん'] if t not in c.talents]; print('missing:', missing)"`
Expected: `missing: []`（核心成員都在；若有缺，回頭檢查 denylist 是否誤剔或來源頁是否變動）

- [ ] **Step 6: Commit**

```bash
git add stores_config.yaml
git commit -m "chore(config): refresh talents from collection-page miner"
```

---

## Verification（全部任務完成後）

- [ ] Type check：`.venv/bin/basedpyright scripts/mine_talents.py` → 0 errors
- [ ] Lint：`uvx ruff check scripts/mine_talents.py tests/test_mine_talents.py` → All checks passed
- [ ] Test：`.venv/bin/python -m pytest tests/test_mine_talents.py -v -o addopts=""` → 5 passed
- [ ] Config 載入：Task 7 Step 4/5 通過
