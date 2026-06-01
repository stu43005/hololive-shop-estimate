# 階層化 crawl/sync 處理 log Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 crawl 入口輸出每 product → 每品項的階層化處理樹（拆解/排除、detail 擷取、typing 決策來源、embedding 狀態），run 入口維持靜默；同時兩入口的每-20 心跳與 done 行附加 store 級合計（items / excluded / detail hit / typing 三態 / embed indexed）。

**Architecture:** 三個內部分類/拆解函式改回傳富資訊型別（`TypeDecision`、`DecomposeResult`、`RebuildReport`），engine 在 `sync_products` 內把每 product 的決策累積成單一多行 log record（受 `log_item_trees` flag 控制是否輸出），並無條件把合計累加進 `SyncResult`；`async_process_queue` 跨 product 累加 `PipelineResult` 並把合計附加到既有 `progress:`／`done:` 行（單一多行 record）。flag 由 CLI crawl 入口設 `True`、scheduler（run 入口）沿用預設 `False`。

**Tech Stack:** Python 3.14、dataclasses、stdlib logging、pytest（caplog）、basedpyright、ruff（uvx）。

**驗證工具鏈（每個 Task 結尾執行）：**
- Type check：`.venv/bin/basedpyright estimator_king`（production code 0 error）
- Lint：`uvx ruff check estimator_king tests`
- Test：`.venv/bin/python -m pytest <path> -v -o addopts=""`

---

## File Structure

| 檔案 | 改動 | 責任 |
| --- | --- | --- |
| `estimator_king/sync/typing.py` | Modify | 新增 `TypeDecision`；`_llm_classify` 回 tuple；`classify_item` 回 `TypeDecision`；`classify_query` body 解構（對外仍 `list[str]`） |
| `estimator_king/sync/items.py` | Modify | 新增 `DecomposeResult`；`decompose_items` 回 `DecomposeResult`（含 SET / ¥0 排除計數） |
| `estimator_king/sync/engine.py` | Modify | 新增 `ItemRow`、`RebuildReport`、`_format_product_tree`、`_format_skipped`；`SyncResult` 新增 7 合計欄；`_rebuild_product_items` 回 `RebuildReport`；`sync_products` 新增 `log_item_trees` flag、累加合計、輸出樹/skipped |
| `estimator_king/crawler/async_pipeline.py` | Modify | `PipelineResult` 新增 7 合計欄；`_handle` 累加；心跳/done 附加合計（多行 record）；`async_process_queue` 新增 `log_item_trees` flag 並下傳 |
| `estimator_king/crawler/cycle.py` | Modify | `run_crawl_cycle` 新增 `log_item_trees` flag，轉傳給 `async_process_queue` |
| `estimator_king/__main__.py` | Modify | CLI `crawl` 呼叫 `run_crawl_cycle(..., log_item_trees=True)` |
| `estimator_king/crawler/scheduler.py` | 不改 | `run_once` 不傳 `log_item_trees`，沿用預設 `False`（run 入口無樹） |
| `tests/test_typing.py` | Modify | `classify_item` 斷言改 `.item_type`；新增 `.source` 三態斷言 |
| `tests/test_items.py` | Modify | `decompose_items(...)` 改 `.items`；新增排除計數斷言 |
| `tests/test_engine_items.py` | Modify | 新增 `SyncResult` 合計欄斷言（created/updated 有值、unchanged/failed 為 0） |
| `tests/test_engine_logging.py` | Create | crawl 輸出單一樹 record / skipped 單行 / run 靜默 |
| `tests/test_async_pipeline_logging.py` | Modify | 既有子字串斷言保留；新增合計附加斷言（單一多行 record） |

---

## Task 1: `typing.py` — `TypeDecision` 決策來源

**Files:**
- Modify: `estimator_king/sync/typing.py`
- Test: `tests/test_typing.py`

- [ ] **Step 1: 改寫 `tests/test_typing.py`（失敗測試）**

整檔取代為以下內容（`classify_item` 改回傳 `TypeDecision`，新增 `.source` 三態斷言；`classify_query` 對外語義不變）：

```python
from estimator_king.sync.typing import TypeDecision, classify_item, classify_query

ITEM_TYPES = ["ぬいぐるみ", "キーホルダー", "ポーチ", "タオル"]


class FakeTypingProvider:
    def __init__(self, answer="ぬいぐるみ"):
        self.answer = answer
        self.calls = 0

    def classify_via_llm(self, text, item_types):
        self.calls += 1
        return self.answer


class FakeRepo:
    def __init__(self):
        self.store = {}

    def get_cached_type(self, h):
        return self.store.get(h)

    def put_cached_type(self, h, t, v, text_sample):
        self.store[h] = t


def test_single_vocab_hit_no_llm():
    tp = FakeTypingProvider()
    out = classify_item("もちもちぬいぐるみ", item_types=ITEM_TYPES,
                        item_types_version=1, typing_provider=tp, repository=FakeRepo())
    assert isinstance(out, TypeDecision)
    assert out.item_type == "ぬいぐるみ"
    assert out.source == "vocab"
    assert tp.calls == 0


def test_classify_item_multi_hit_goes_to_llm():
    tp = FakeTypingProvider(answer="ぬいぐるみ")
    out = classify_item("ぬいぐるみポーチ", item_types=ITEM_TYPES,
                        item_types_version=1, typing_provider=tp, repository=FakeRepo())
    assert out.item_type == "ぬいぐるみ"
    assert out.source == "llm"  # multi-hit -> LLM picks one
    assert tp.calls == 1


def test_classify_item_zero_hit_llm_validates_to_sonota():
    tp = FakeTypingProvider(answer="存在しない型")
    out = classify_item("謎の物体", item_types=ITEM_TYPES,
                        item_types_version=1, typing_provider=tp, repository=FakeRepo())
    assert out.item_type == "その他"
    assert out.source == "llm"  # zero-hit path must reach the LLM
    assert tp.calls == 1


def test_cache_hit_returns_cache_source():
    repo = FakeRepo()
    tp = FakeTypingProvider(answer="ぬいぐるみ")
    first = classify_item("謎の物体", item_types=ITEM_TYPES, item_types_version=1,
                          typing_provider=tp, repository=repo)
    second = classify_item("謎の物体", item_types=ITEM_TYPES, item_types_version=1,
                           typing_provider=tp, repository=repo)
    assert tp.calls == 1  # second call served from cache
    assert first.source == "llm"
    assert second.source == "cache"
    assert second.item_type == "ぬいぐるみ"


def test_classify_query_multi_hit_keeps_all_no_llm():
    tp = FakeTypingProvider()
    out = classify_query("ぬいぐるみポーチ", item_types=ITEM_TYPES,
                         item_types_version=1, typing_provider=tp)
    assert set(out) == {"ぬいぐるみ", "ポーチ"}
    assert tp.calls == 0


def test_classify_query_sonota_returns_empty_list():
    tp = FakeTypingProvider(answer="その他")
    out = classify_query("謎の物体", item_types=ITEM_TYPES,
                         item_types_version=1, typing_provider=tp)
    assert out == []


def test_llm_exception_returns_sonota():
    class Boom:
        def classify_via_llm(self, text, item_types):
            raise RuntimeError("boom")

    out = classify_item("謎の物体", item_types=ITEM_TYPES, item_types_version=1,
                        typing_provider=Boom(), repository=None)
    assert out.item_type == "その他"
    assert out.source == "llm"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `.venv/bin/python -m pytest tests/test_typing.py -v -o addopts=""`
Expected: FAIL（`ImportError: cannot import name 'TypeDecision'`）

- [ ] **Step 3: 修改 `estimator_king/sync/typing.py`**

在檔頭 import 區（`from typing import Protocol` 那行附近）加入 dataclass import 並新增 `TypeDecision`；把 `_llm_classify` 改回 `tuple[str, str]`；`classify_item` 改回 `TypeDecision`；`classify_query` body 解構。

3a. 修改 import：把

```python
from typing import Protocol
```

改為

```python
from dataclasses import dataclass
from typing import Protocol
```

3b. 在 `OTHER = "その他"` 之後新增：

```python
@dataclass(frozen=True)
class TypeDecision:
    item_type: str
    source: str  # "vocab" | "cache" | "llm"
```

3c. 把 `_llm_classify` 整個函式取代為：

```python
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
```

3d. 把 `classify_item` 整個函式取代為：

```python
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
```

3e. 把 `classify_query` 的最後兩行（`result = _llm_classify(...)` 與 `return [] if result == OTHER else [result]`）取代為：

```python
    item_type, _ = _llm_classify(
        text, item_types, item_types_version, typing_provider, repository)
    return [] if item_type == OTHER else [item_type]
```

- [ ] **Step 4: 跑測試確認通過**

Run: `.venv/bin/python -m pytest tests/test_typing.py -v -o addopts=""`
Expected: PASS（8 passed）

- [ ] **Step 5: 工具鏈驗證**

Run: `.venv/bin/basedpyright estimator_king && uvx ruff check estimator_king tests`
Expected: basedpyright 0 error、ruff clean

- [ ] **Step 6: Commit**

```bash
git add estimator_king/sync/typing.py tests/test_typing.py
git commit -m "feat(typing): classify_item returns TypeDecision with source"
```

---

## Task 2: `items.py` — `DecomposeResult` 排除計數

**Files:**
- Modify: `estimator_king/sync/items.py`
- Test: `tests/test_items.py`

- [ ] **Step 1: 改寫 `tests/test_items.py`（失敗測試）**

整檔取代為以下內容（所有 `decompose_items(...)` 改用 `.items`；新增排除計數斷言）：

```python
from estimator_king.crawler.snapshot import ProductSnapshot, ProductVariant
from estimator_king.sync.items import DecomposeResult, decompose_items

TALENTS = frozenset({"さくらみこ", "白上フブキ", "博衣こより"})


def _snap(title, variants, html_details=None, pid=1):
    return ProductSnapshot(
        product_id=pid, title=title, description="",
        variants=[ProductVariant(variant_id=i + 1, title=t, price=p)
                  for i, (t, p) in enumerate(variants)],
        html_details=html_details or {},
    )


def test_excludes_set_and_zero_price():
    snap = _snap("P", [
        ("セット / フルセット", "2000"),
        ("グッズ / 特典ステッカー", "0"),
        ("グッズ / アクリルスタンド", "500"),
    ])
    result = decompose_items(snap, talents=TALENTS)
    assert isinstance(result, DecomposeResult)
    assert [i.item_name for i in result.items] == ["アクリルスタンド"]
    assert result.items[0].price_jpy == 500
    assert result.excluded_set == 1
    assert result.excluded_zero == 1


def test_unparseable_price_counts_as_excluded_zero():
    snap = _snap("P", [
        ("グッズ / 謎の値段", "N/A"),
        ("グッズ / アクリルスタンド", "500"),
    ])
    result = decompose_items(snap, talents=TALENTS)
    assert [i.item_name for i in result.items] == ["アクリルスタンド"]
    assert result.excluded_set == 0
    assert result.excluded_zero == 1  # "N/A" parses to None -> counted as ¥0


def test_talent_variants_merge_to_product_title():
    snap = _snap("3Dアクリルスタンド Blue Journey衣装ver.", [
        ("グッズ / さくらみこ Blue Journey衣装ver.", "330"),
        ("グッズ / 白上フブキ Blue Journey衣装ver.", "330"),
    ])
    result = decompose_items(snap, talents=TALENTS)
    items = result.items
    assert len(items) == 1
    assert items[0].item_name == "3Dアクリルスタンド Blue Journey衣装ver."
    assert items[0].price_jpy == 330
    assert len(items[0].source_variant_ids) == 2
    assert set(items[0].talents) == {"さくらみこ", "白上フブキ"}
    assert result.excluded_set == 0
    assert result.excluded_zero == 0


def test_themed_series_not_merged_even_at_same_price():
    snap = _snap("生日記念", [
        ("グッズ / Start your Journey ポーチ", "440"),
        ("グッズ / Start your Journey プレート", "440"),
    ])
    result = decompose_items(snap, talents=TALENTS)
    names = sorted(i.item_name for i in result.items)
    # Codepoint sort: プ (U+30D7) < ポ (U+30DD).
    assert names == ["Start your Journey プレート", "Start your Journey ポーチ"]


def test_short_option_value_prepends_product_title():
    snap = _snap("ぶいすぽっ！オリジナルTシャツ", [
        ("バリエーション / 黒　M", "5500"),
        ("バリエーション / 白　L", "5500"),
    ])
    result = decompose_items(snap, talents=TALENTS)
    names = sorted(i.item_name for i in result.items)
    assert names == ["ぶいすぽっ！オリジナルTシャツ 白 L", "ぶいすぽっ！オリジナルTシャツ 黒 M"]


def test_detail_snippet_substring_match():
    snap = _snap("誕生日記念", [
        ("グッズ / Eternity アクリルジオラマスタンド", "995"),
        ("グッズ / イオフィカラー ショルダーバッグ", "600"),
    ], html_details={
        "グッズ詳細": (
            "◇記念グッズ ・Eternity アクリルジオラマスタンド サイズ：約H250×W150×D60mm 素材：アクリル"
            " ・イオフィカラー ショルダーバッグ サイズ：約H18.5×W13×D5cm 素材：ポリエステル"
        )
    })
    items = {i.item_name: i for i in decompose_items(snap, talents=TALENTS).items}
    assert "H250" in items["Eternity アクリルジオラマスタンド"].detail_snippet
    assert "ポリエステル" in items["イオフィカラー ショルダーバッグ"].detail_snippet


def test_voice_item_has_no_snippet():
    snap = _snap("誕生日記念", [
        ("デジタルコンテンツ / シチュエーションボイス「君となら」", "140"),
    ], html_details={"グッズ詳細": "◇記念グッズ ・アクリルスタンド サイズ：H100"})
    result = decompose_items(snap, talents=TALENTS)
    assert result.items[0].detail_snippet == ""
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `.venv/bin/python -m pytest tests/test_items.py -v -o addopts=""`
Expected: FAIL（`ImportError: cannot import name 'DecomposeResult'`）

- [ ] **Step 3: 修改 `estimator_king/sync/items.py`**

3a. 在 `ProductItem` dataclass 定義之後（`published_at: int` 結尾的那個 class 之後、`_strip_prefix` 之前）新增：

```python
@dataclass(frozen=True)
class DecomposeResult:
    items: list[ProductItem]
    excluded_set: int
    excluded_zero: int
```

3b. 把 `decompose_items` 的簽名與 Step 1+2 過濾迴圈改為累計排除計數，並把結尾 `return items` 改為回傳 `DecomposeResult`。具體：

把函式簽名

```python
def decompose_items(snapshot: ProductSnapshot, *, talents: frozenset[str]) -> list[ProductItem]:
```

改為

```python
def decompose_items(snapshot: ProductSnapshot, *, talents: frozenset[str]) -> DecomposeResult:
```

把 Step 1+2 迴圈

```python
    kept: list[tuple[str, int, int]] = []
    for v in snapshot.variants:
        residual, prefix = _strip_prefix(v.title)
        if prefix is not None and prefix.startswith("セット"):
            continue
        price = _price_to_int(v.price)
        if price is None or price == 0:
            continue
        kept.append((residual, price, v.variant_id))
```

改為

```python
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
```

把結尾

```python
    return items
```

改為

```python
    return DecomposeResult(
        items=items, excluded_set=excluded_set, excluded_zero=excluded_zero)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `.venv/bin/python -m pytest tests/test_items.py -v -o addopts=""`
Expected: PASS（8 passed）

- [ ] **Step 5: 工具鏈驗證**

Run: `.venv/bin/basedpyright estimator_king && uvx ruff check estimator_king tests`
Expected: basedpyright 0 error、ruff clean
注意：此步 `estimator_king/sync/engine.py` 仍以 list 方式使用 `decompose_items` 的結果，basedpyright 會在 engine.py 報型別錯誤——這是預期的，將在 Task 3 修復。若要單獨確認本 task 檔案，可改跑 `.venv/bin/basedpyright estimator_king/sync/items.py`（0 error）。完整 `estimator_king` 0-error 門檻在 Task 3 結尾達成。

- [ ] **Step 6: Commit**

```bash
git add estimator_king/sync/items.py tests/test_items.py
git commit -m "feat(items): decompose_items returns DecomposeResult with exclusion counts"
```

---

## Task 3: `engine.py` — 樹累積、合計欄位、輸出

**Files:**
- Modify: `estimator_king/sync/engine.py`
- Test: `tests/test_engine_items.py`（新增合計斷言）
- Test: `tests/test_engine_logging.py`（新增）

- [ ] **Step 1: 新增 `tests/test_engine_logging.py`（失敗測試）**

建立新檔，內容如下：

```python
import logging

from estimator_king.crawler.snapshot import ProductSnapshot, ProductVariant
from estimator_king.database.repository import ProductStateRepository
from estimator_king.sync.engine import sync_products

TALENTS = frozenset({"さくらみこ"})
ITEM_TYPES = ["アクリルスタンド", "ポーチ"]


class FakeEmbedder:
    def embed_documents(self, texts):
        return [[float(len(t)), 0.0, 0.0] for t in texts]


class FakeVectorStore:
    def __init__(self):
        self.docs = {}

    def upsert(self, id, document, embedding, metadata):
        self.docs[id] = (document, dict(metadata))

    def delete(self, ids):
        for i in ids:
            self.docs.pop(i, None)

    def get_by_product(self, store_id, product_id):
        from estimator_king.vectorstore.store import QueryHit
        return [
            QueryHit(id=i, document=d, metadata=m, distance=0.0)
            for i, (d, m) in self.docs.items()
            if m.get("store_id") == store_id and m.get("product_id") == product_id
        ]


class FakeTypingProvider:
    def classify_via_llm(self, text, item_types):
        return "その他"


def _snap():
    # 2 priceable items (アクリルスタンド / ポーチ, both vocab hits),
    # 1 SET excluded, 1 ¥0 excluded.
    return ProductSnapshot(
        product_id=10, title="P", description="",
        variants=[
            ProductVariant(variant_id=1, title="グッズ / アクリルスタンド", price="500"),
            ProductVariant(variant_id=2, title="グッズ / 旅のポーチ", price="800"),
            ProductVariant(variant_id=3, title="セット / フルセット", price="2000"),
            ProductVariant(variant_id=4, title="グッズ / 特典", price="0"),
        ],
        html_details={},
    )


def _repo():
    repo = ProductStateRepository(":memory:")
    repo.open()
    return repo


def _sync(repo, vs, snap, *, log_item_trees):
    return sync_products(
        [("http://x/products/10", snap)], "hololive", repo,
        FakeEmbedder(), vs,
        typing_provider=FakeTypingProvider(), talents=TALENTS,
        item_types=ITEM_TYPES, item_types_version=1,
        log_item_trees=log_item_trees,
    )


def _engine_msgs(caplog):
    return [r.getMessage() for r in caplog.records
            if r.name == "estimator_king.sync.engine"]


def test_crawl_entry_emits_single_tree_record(caplog):
    repo, vs = _repo(), FakeVectorStore()
    with caplog.at_level(logging.INFO, logger="estimator_king.sync.engine"):
        _sync(repo, vs, _snap(), log_item_trees=True)
    trees = [r.getMessage() for r in caplog.records
             if r.name == "estimator_king.sync.engine"
             and r.getMessage().startswith("product ")]
    assert len(trees) == 1
    msg = trees[0]
    assert "\n" in msg  # single multi-line record, not split across records
    assert 'product hololive:10 "P" (created):' in msg
    assert "2 items" in msg
    assert "2 excluded (SET×1, ¥0×1)" in msg
    assert "typing=アクリルスタンド(vocab)" in msg
    assert "detail=miss" in msg
    assert "embed=indexed" in msg
    repo.close()


def test_crawl_entry_skipped_single_line(caplog):
    repo, vs = _repo(), FakeVectorStore()
    _sync(repo, vs, _snap(), log_item_trees=True)
    with caplog.at_level(logging.INFO, logger="estimator_king.sync.engine"):
        _sync(repo, vs, _snap(), log_item_trees=True)
    msgs = _engine_msgs(caplog)
    skipped = [m for m in msgs if "skipped (unchanged)" in m]
    assert any('product hololive:10 "P" skipped (unchanged)' in m for m in skipped)
    assert all("├─" not in m and "└─" not in m for m in skipped)
    repo.close()


def test_run_entry_emits_no_tree(caplog):
    repo, vs = _repo(), FakeVectorStore()
    with caplog.at_level(logging.INFO, logger="estimator_king.sync.engine"):
        _sync(repo, vs, _snap(), log_item_trees=False)  # created
        _sync(repo, vs, _snap(), log_item_trees=False)  # unchanged
    msgs = _engine_msgs(caplog)
    assert not any(m.startswith("product ") for m in msgs)
    repo.close()
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `.venv/bin/python -m pytest tests/test_engine_logging.py -v -o addopts=""`
Expected: FAIL（`TypeError: sync_products() got an unexpected keyword argument 'log_item_trees'`）

- [ ] **Step 3: 修改 `estimator_king/sync/engine.py` — import 與資料結構**

3a. 把 import 區的

```python
from estimator_king.sync.items import ProductItem, decompose_items
from estimator_king.sync.typing import classify_item
```

改為

```python
from estimator_king.sync.items import ProductItem, decompose_items
from estimator_king.sync.typing import TypeDecision, classify_item
```

3b. 把 `SyncResult` dataclass 取代為（新增 7 個合計欄，皆預設 0）：

```python
@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    failed_ids: list[str] = field(default_factory=list)
    items: int = 0
    excluded: int = 0
    detail_hits: int = 0
    typing_vocab: int = 0
    typing_cache: int = 0
    typing_llm: int = 0
    embed_indexed: int = 0
```

3c. 在 `SyncResult` 之後、`_item_slug` 之前新增 `ItemRow` 與 `RebuildReport`：

```python
@dataclass(frozen=True)
class ItemRow:
    item_name: str
    n_variants: int
    n_talents: int
    detail_hit: bool
    decision: TypeDecision
    embed_status: str  # "indexed" | "skipped(unchanged)"


@dataclass
class RebuildReport:
    item_rows: list[ItemRow]
    excluded_set: int
    excluded_zero: int
```

- [ ] **Step 4: 修改 `engine.py` — 新增格式化 helper**

在 `_item_hash` 函式之後、`sync_products` 之前新增兩個純格式化函式：

```python
def _format_skipped(store_id: str, product_id: str, title: str) -> str:
    return f'product {store_id}:{product_id} "{title}" skipped (unchanged)'


def _format_product_tree(
    store_id: str, product_id: str, title: str, verb: str,
    rows: list[ItemRow], excluded_set: int, excluded_zero: int,
) -> str:
    n = len(rows)
    excluded = excluded_set + excluded_zero
    head = f'product {store_id}:{product_id} "{title}" ({verb}): {n} items'
    if excluded:
        head += f", {excluded} excluded (SET×{excluded_set}, ¥0×{excluded_zero})"
    lines = [head]
    for i, row in enumerate(rows):
        last = i == n - 1
        connector = "  └─ " if last else "  ├─ "
        cont = "       " if last else "  │    "
        item_line = f'{connector}item "{row.item_name}" ×{row.n_variants}'
        if row.n_talents > 0:
            item_line += f" talents={row.n_talents}"
        lines.append(item_line)
        detail = "hit" if row.detail_hit else "miss"
        lines.append(
            f"{cont}detail={detail}  "
            f"typing={row.decision.item_type}({row.decision.source})  "
            f"embed={row.embed_status}"
        )
    return "\n".join(lines)
```

- [ ] **Step 5: 修改 `engine.py` — `_rebuild_product_items` 回傳 `RebuildReport`**

把整個 `_rebuild_product_items` 函式取代為（收集每 item 的 `ItemRow`、回傳 `RebuildReport`）：

```python
def _rebuild_product_items(
    snapshot: ProductSnapshot,
    store_id: str,
    product_url: str,
    repository: ProductStateRepository,
    embedder: _Embedder,
    vector_store: _VectorStore,
    typing_provider: _TypingProvider,
    talents: frozenset[str],
    item_types: list[str],
    item_types_version: int,
) -> RebuildReport:
    product_id = str(snapshot.product_id)
    existing = {h.id: str(h.metadata.get("item_hash", "")) for h in
                vector_store.get_by_product(store_id, product_id)}

    decomposed = decompose_items(snapshot, talents=talents)
    rows: list[ItemRow] = []
    desired_ids: set[str] = set()
    for item in decomposed.items:
        decision = classify_item(
            f"{item.item_name} {item.product_title}", item_types=item_types,
            item_types_version=item_types_version, typing_provider=typing_provider,
            repository=repository,
        )
        item_type = decision.item_type
        document = _format_item_document(item, item_type)
        item_hash = _item_hash(document, item.price_jpy, item_type)
        item_id = f"{store_id}:{product_id}:{_item_slug(item.item_name, item.price_jpy)}"
        desired_ids.add(item_id)
        if existing.get(item_id) == item_hash:
            embed_status = "skipped(unchanged)"
        else:
            embedding = embedder.embed_documents([document])[0]
            metadata: dict[str, object] = {
                "store_id": store_id,
                "product_id": product_id,
                "product_url": product_url,
                "product_title": item.product_title,
                "item_name": item.item_name,
                "item_type": item_type,
                "price_jpy": item.price_jpy,
                "published_at": item.published_at,
                "detail_snippet": item.detail_snippet,
                "item_hash": item_hash,
            }
            vector_store.upsert(item_id, document, embedding, metadata)
            embed_status = "indexed"
        rows.append(ItemRow(
            item_name=item.item_name,
            n_variants=len(item.source_variant_ids),
            n_talents=len(item.talents),
            detail_hit=bool(item.detail_snippet.strip()),
            decision=decision,
            embed_status=embed_status,
        ))

    stale = [vid for vid in existing if vid not in desired_ids]
    if stale:
        vector_store.delete(stale)
    return RebuildReport(
        item_rows=rows,
        excluded_set=decomposed.excluded_set,
        excluded_zero=decomposed.excluded_zero,
    )
```

- [ ] **Step 6: 修改 `engine.py` — `sync_products` 簽名、累加、輸出**

6a. 把 `sync_products` 簽名的 keyword-only 區結尾

```python
    typing_provider: _TypingProvider,
    talents: frozenset[str],
    item_types: list[str],
    item_types_version: int,
) -> SyncResult:
```

改為

```python
    typing_provider: _TypingProvider,
    talents: frozenset[str],
    item_types: list[str],
    item_types_version: int,
    log_item_trees: bool = False,
) -> SyncResult:
```

6b. 把 `try` 區塊（從 `try:` 到 `except Exception:` 之前）取代為：

```python
        try:
            if unchanged:
                result.skipped += 1
                if log_item_trees:
                    logger.info(_format_skipped(
                        store_id, str(snapshot.product_id), snapshot.title))
            else:
                report = _rebuild_product_items(
                    snapshot, store_id, product_url, repository, embedder,
                    vector_store, typing_provider, talents, item_types, item_types_version,
                )
                last_indexed_at = now
                verb = "created" if state is None else "updated"
                if state is None:
                    result.created += 1
                else:
                    result.updated += 1
                rows = report.item_rows
                result.items += len(rows)
                result.excluded += report.excluded_set + report.excluded_zero
                result.detail_hits += sum(1 for r in rows if r.detail_hit)
                result.typing_vocab += sum(1 for r in rows if r.decision.source == "vocab")
                result.typing_cache += sum(1 for r in rows if r.decision.source == "cache")
                result.typing_llm += sum(1 for r in rows if r.decision.source == "llm")
                result.embed_indexed += sum(1 for r in rows if r.embed_status == "indexed")
                if log_item_trees:
                    logger.info(_format_product_tree(
                        store_id, str(snapshot.product_id), snapshot.title, verb,
                        rows, report.excluded_set, report.excluded_zero))
```

（`except Exception:` 區塊維持原樣不動：`logger.exception("Sync failed for %s", external_key)` + `result.failed += 1` + `result.failed_ids.append(external_key)`。）

- [ ] **Step 7: 跑新測試確認通過**

Run: `.venv/bin/python -m pytest tests/test_engine_logging.py -v -o addopts=""`
Expected: PASS（3 passed）

- [ ] **Step 8: 在 `tests/test_engine_items.py` 末尾新增合計斷言測試**

在檔案最後（`test_stale_item_vector_deleted_when_variant_removed` 之後）append：

```python
def test_sync_result_aggregates_on_create():
    repo, vs = _repo(), FakeVectorStore()
    res = _sync(repo, vs, _snap())
    assert res.items == 2
    assert res.excluded == 0
    assert res.detail_hits == 0  # _snap has no html_details
    assert res.typing_vocab == 2  # both names are unique vocab hits
    assert res.typing_cache == 0
    assert res.typing_llm == 0
    assert res.embed_indexed == 2
    repo.close()


def test_unchanged_sync_has_zero_aggregates():
    repo, vs = _repo(), FakeVectorStore()
    _sync(repo, vs, _snap())
    res = _sync(repo, vs, _snap())
    assert res.skipped == 1
    assert res.items == 0
    assert res.excluded == 0
    assert res.detail_hits == 0
    assert res.typing_vocab == 0
    assert res.embed_indexed == 0
    repo.close()


def test_failed_sync_has_zero_aggregates():
    class BoomEmbedder:
        def embed_documents(self, texts):
            raise RuntimeError("boom")

    repo, vs = _repo(), FakeVectorStore()
    res = sync_products(
        [("http://x/products/10", _snap())], "hololive", repo,
        BoomEmbedder(), vs, typing_provider=FakeTypingProvider(), talents=TALENTS,
        item_types=ITEM_TYPES, item_types_version=1)
    assert res.failed == 1
    assert res.items == 0
    assert res.embed_indexed == 0
    assert res.typing_vocab == 0
    repo.close()
```

- [ ] **Step 9: 跑 `test_engine_items.py` 全檔確認通過**

Run: `.venv/bin/python -m pytest tests/test_engine_items.py -v -o addopts=""`
Expected: PASS（既有 5 個 + 新增 3 個 = 8 passed）

- [ ] **Step 10: 工具鏈驗證（完整 `estimator_king` 0-error 門檻）**

Run: `.venv/bin/basedpyright estimator_king && uvx ruff check estimator_king tests`
Expected: basedpyright 0 error、ruff clean

- [ ] **Step 11: Commit**

```bash
git add estimator_king/sync/engine.py tests/test_engine_logging.py tests/test_engine_items.py
git commit -m "feat(engine): per-product processing tree + SyncResult aggregates"
```

---

## Task 4: `async_pipeline.py` — store 級合計心跳

**Files:**
- Modify: `estimator_king/crawler/async_pipeline.py`
- Test: `tests/test_async_pipeline_logging.py`

- [ ] **Step 1: 改寫 `tests/test_async_pipeline_logging.py`（失敗測試）**

整檔取代為以下內容（保留既有 `queue:`／`progress:`／`done:` 子字串斷言，新增合計附加與單一多行 record 斷言）：

```python
import asyncio
import logging
from unittest.mock import patch

import pytest

from estimator_king.config_schema import CrawlerPolicy
from estimator_king.crawler import async_pipeline
from estimator_king.crawler.async_pipeline import async_process_queue
from estimator_king.crawler.snapshot import ProductSnapshot, ProductVariant
from estimator_king.database.repository import ProductStateRepository


class FakeTypingProvider:
    def classify_via_llm(self, text, item_types):
        return "その他"


class FakeEmbedder:
    def embed_documents(self, texts):
        return [[0.1, 0.2] for _ in texts]


class FakeVectorStore:
    def __init__(self):
        self._meta = {}

    def upsert(self, id, document, embedding, metadata):
        self._meta[id] = dict(metadata)

    def delete(self, ids):
        pass

    def get_by_product(self, store_id, product_id):
        from estimator_king.vectorstore.store import QueryHit
        return [QueryHit(id=i, document="", metadata=m, distance=0.0)
                for i, m in self._meta.items()
                if m.get("store_id") == store_id and m.get("product_id") == product_id]


@pytest.fixture
def repo():
    with ProductStateRepository(":memory:") as r:
        yield r


def _snap(pid):
    return ProductSnapshot(
        product_id=pid, title=f"T{pid}", description="d",
        variants=[ProductVariant(1, "S", "2000")], html_details={},
    )


def _run(repo, caplog, n):
    for pid in range(1, n + 1):
        repo.enqueue_url("hololive", f"https://x/products/{pid}")

    def fake_fetch(url, client):
        pid = int(url.rsplit("/", 1)[1])
        return _snap(pid)

    with caplog.at_level(logging.INFO, logger="estimator_king.crawler.async_pipeline"):
        with patch(
            "estimator_king.crawler.async_pipeline.fetch_product",
            side_effect=fake_fetch,
        ):
            result = asyncio.run(async_process_queue(
                "hololive", CrawlerPolicy(), repo,
                FakeEmbedder(), FakeVectorStore(),
                typing_provider=FakeTypingProvider(), talents=frozenset(),
                item_types=[], item_types_version=0))
    msgs = [
        r.getMessage() for r in caplog.records
        if r.name == "estimator_king.crawler.async_pipeline" and r.levelno == logging.INFO
    ]
    return result, msgs


def test_queue_start_heartbeat_and_done_logged(repo, caplog):
    n = async_pipeline._PROGRESS_LOG_EVERY + 5
    result, msgs = _run(repo, caplog, n)

    assert result.processed == n
    assert any(f"queue: {n} entries to process" in m for m in msgs)
    assert any("progress:" in m and f"/{n} processed" in m for m in msgs)
    assert any("done: created=" in m for m in msgs)


def test_heartbeat_and_done_append_aggregates(repo, caplog):
    n = async_pipeline._PROGRESS_LOG_EVERY + 5
    result, msgs = _run(repo, caplog, n)

    # each product yields 1 item, item_types=[] -> LLM source on every item.
    assert result.items == n
    assert result.typing_llm == n
    assert result.embed_indexed == n

    heartbeat = [m for m in msgs if "progress:" in m]
    assert heartbeat
    hb = heartbeat[0]
    assert "\n" in hb  # single multi-line record
    assert "items" in hb
    assert "detail hit:" in hb
    assert "typing:" in hb and "(vocab)" in hb and "(cache)" in hb and "(llm)" in hb
    assert "embed indexed:" in hb

    done = [m for m in msgs if "done: created=" in m]
    assert done
    dn = done[0]
    assert f"{n} items" in dn  # final cumulative total
    assert "embed indexed:" in dn
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `.venv/bin/python -m pytest tests/test_async_pipeline_logging.py -v -o addopts=""`
Expected: `test_heartbeat_and_done_append_aggregates` FAIL（`AttributeError: 'PipelineResult' object has no attribute 'items'`）

- [ ] **Step 3: 修改 `async_pipeline.py` — `PipelineResult` 新增合計欄**

把 `PipelineResult` dataclass 取代為：

```python
@dataclass
class PipelineResult:
    processed: int = 0
    failed: int = 0
    skipped: int = 0
    created: int = 0
    updated: int = 0
    sync_skipped: int = 0
    items: int = 0
    excluded: int = 0
    detail_hits: int = 0
    typing_vocab: int = 0
    typing_cache: int = 0
    typing_llm: int = 0
    embed_indexed: int = 0
```

- [ ] **Step 4: 修改 `async_pipeline.py` — 新增合計格式 helper**

在 `_PROGRESS_LOG_EVERY = 20` 之後、`@dataclass` 之前新增：

```python
def _aggregate_lines(result: "PipelineResult") -> str:
    return (
        f"\n  {result.items} items"
        f"\n  {result.excluded} excluded"
        f"\n  detail hit: {result.detail_hits}"
        f"\n  typing: {result.typing_vocab}(vocab) "
        f"{result.typing_cache}(cache) {result.typing_llm}(llm)"
        f"\n  embed indexed: {result.embed_indexed}"
    )
```

- [ ] **Step 5: 修改 `async_pipeline.py` — 簽名、累加、心跳、done**

5a. 把 `async_process_queue` 簽名的 keyword-only 區

```python
    *,
    typing_provider: TypingProvider,
    talents: frozenset[str],
    item_types: list[str],
    item_types_version: int,
    proxy: ProxyConfig | None = None,
) -> PipelineResult:
```

改為

```python
    *,
    typing_provider: TypingProvider,
    talents: frozenset[str],
    item_types: list[str],
    item_types_version: int,
    log_item_trees: bool = False,
    proxy: ProxyConfig | None = None,
) -> PipelineResult:
```

5b. 把 `_handle` 內的 `sync_products` 呼叫與後續累加區塊

```python
                sync_result = await asyncio.to_thread(
                    sync_products, [(product_url, snapshot)], store_id,
                    state_repo, embedder, vector_store,
                    typing_provider=typing_provider, talents=talents,
                    item_types=item_types, item_types_version=item_types_version,
                )
                state_repo.delete_queue_entry(entry_id)
                result.created += sync_result.created
                result.updated += sync_result.updated
                result.sync_skipped += sync_result.skipped
                result.processed += 1
```

取代為

```python
                sync_result = await asyncio.to_thread(
                    sync_products, [(product_url, snapshot)], store_id,
                    state_repo, embedder, vector_store,
                    typing_provider=typing_provider, talents=talents,
                    item_types=item_types, item_types_version=item_types_version,
                    log_item_trees=log_item_trees,
                )
                state_repo.delete_queue_entry(entry_id)
                result.created += sync_result.created
                result.updated += sync_result.updated
                result.sync_skipped += sync_result.skipped
                result.items += sync_result.items
                result.excluded += sync_result.excluded
                result.detail_hits += sync_result.detail_hits
                result.typing_vocab += sync_result.typing_vocab
                result.typing_cache += sync_result.typing_cache
                result.typing_llm += sync_result.typing_llm
                result.embed_indexed += sync_result.embed_indexed
                result.processed += 1
```

5c. 把心跳區塊

```python
                if result.processed % _PROGRESS_LOG_EVERY == 0:
                    logger.info(
                        "store=%s progress: %d/%d processed",
                        store_id, result.processed, len(entries),
                    )
```

取代為

```python
                if result.processed % _PROGRESS_LOG_EVERY == 0:
                    logger.info(
                        "store=%s progress: %d/%d processed%s",
                        store_id, result.processed, len(entries),
                        _aggregate_lines(result),
                    )
```

5d. 把結尾 done 區塊

```python
    logger.info(
        "store=%s done: created=%d updated=%d skipped=%d failed=%d",
        store_id, result.created, result.updated, result.sync_skipped, result.failed,
    )
    return result
```

取代為

```python
    logger.info(
        "store=%s done: created=%d updated=%d skipped=%d failed=%d%s",
        store_id, result.created, result.updated, result.sync_skipped, result.failed,
        _aggregate_lines(result),
    )
    return result
```

- [ ] **Step 6: 跑測試確認通過**

Run: `.venv/bin/python -m pytest tests/test_async_pipeline_logging.py -v -o addopts=""`
Expected: PASS（2 passed）

- [ ] **Step 7: 工具鏈驗證**

Run: `.venv/bin/basedpyright estimator_king && uvx ruff check estimator_king tests`
Expected: basedpyright 0 error、ruff clean

- [ ] **Step 8: Commit**

```bash
git add estimator_king/crawler/async_pipeline.py tests/test_async_pipeline_logging.py
git commit -m "feat(pipeline): append store-level aggregates to progress/done heartbeat"
```

---

## Task 5: flag 串接（cycle + CLI）與既有測試回歸

**Files:**
- Modify: `estimator_king/crawler/cycle.py`
- Modify: `estimator_king/__main__.py`

- [ ] **Step 1: 修改 `estimator_king/crawler/cycle.py`**

1a. 把 `run_crawl_cycle` 簽名

```python
    typing_provider: "TypingProvider",
    *,
    force_refetch: bool = False,
) -> dict[str, int]:
```

改為

```python
    typing_provider: "TypingProvider",
    *,
    force_refetch: bool = False,
    log_item_trees: bool = False,
) -> dict[str, int]:
```

1b. 把 `async_process_queue` 呼叫

```python
                    result = await async_process_queue(
                        store.id, config.crawler, repo, embedder, vector_store,
                        typing_provider=typing_provider, talents=config.talents,
                        item_types=config.item_types,
                        item_types_version=config.item_types_version,
                        proxy=config.proxy)
```

改為

```python
                    result = await async_process_queue(
                        store.id, config.crawler, repo, embedder, vector_store,
                        typing_provider=typing_provider, talents=config.talents,
                        item_types=config.item_types,
                        item_types_version=config.item_types_version,
                        log_item_trees=log_item_trees, proxy=config.proxy)
```

- [ ] **Step 2: 修改 `estimator_king/__main__.py`（CLI crawl 入口開樹）**

把 `run_crawl` 內的呼叫

```python
            run_crawl_cycle(config, config.database_path,
                            providers.embedder, providers.vector_store,
                            providers.typing_provider,
                            force_refetch=args.force_refetch))
```

改為

```python
            run_crawl_cycle(config, config.database_path,
                            providers.embedder, providers.vector_store,
                            providers.typing_provider,
                            force_refetch=args.force_refetch,
                            log_item_trees=True))
```

（`estimator_king/crawler/scheduler.py` 不改：`run_once` 的 `run_crawl_cycle(...)` 呼叫不傳 `log_item_trees`，沿用預設 `False`，run 入口因此無樹。）

- [ ] **Step 3: 工具鏈驗證**

Run: `.venv/bin/basedpyright estimator_king && uvx ruff check estimator_king tests`
Expected: basedpyright 0 error、ruff clean

- [ ] **Step 4: 回跑全部 5 個既有 crawl-path 測試**

Run: `.venv/bin/python -m pytest tests/test_async_pipeline.py tests/test_async_pipeline_logging.py tests/test_integration_async_pipeline.py tests/test_crawl_cycle.py tests/test_scheduler.py -v -o addopts=""`
Expected: 全部 PASS（這些呼叫端不傳 `log_item_trees`，沿用預設 `False`，行為不變）

- [ ] **Step 5: 跑完整測試套件（含 coverage）**

Run: `.venv/bin/python -m pytest`
Expected: 全部 PASS

- [ ] **Step 6: Commit**

```bash
git add estimator_king/crawler/cycle.py estimator_king/__main__.py
git commit -m "feat(crawl): wire log_item_trees flag from CLI crawl entry"
```

---

## Acceptance Criteria（對照驗收）

1. crawl 入口（`log_item_trees=True`）：含合併品項 / SET / ¥0 / 各 typing 來源的 product 跑 sync，INFO log 出現單筆多行樹，節點含 item ×N、talents、detail hit/miss、typing 三態來源、embed indexed/skipped、排除計數。→ Task 3 `test_crawl_entry_emits_single_tree_record`
2. crawl 入口第二次同內容 sync → 單行 `skipped (unchanged)`，無樹。→ Task 3 `test_crawl_entry_skipped_single_line`
3. run 入口（預設 `log_item_trees=False`）：同樣 sync 不輸出任何 `product ...` 樹或 skipped 行。→ Task 3 `test_run_entry_emits_no_tree`
4. 整棵樹為單一 log record（含換行）。→ Task 3 樹測試 `"\n" in msg` + 單筆 record 計數
5. 無業務行為改變：created/updated/skipped 計數、向量結果、查詢端類型清單不變。→ Task 3 既有 `test_engine_items.py` 斷言 + Task 1 `classify_query` 測試 + Task 5 回歸
6. `queue:` 行與 stdout JSON counters 不變；`progress:`／`done:` 保留既有欄位文字並附加合計。→ Task 4 `test_queue_start_heartbeat_and_done_logged`（既有子字串）+ `test_heartbeat_and_done_append_aggregates`
7. store 級合計心跳（兩入口皆適用）：每-20 心跳與 done 附加合計五行，數值為自 store 起算累計總和；unchanged/failed 不計入；心跳＋合計為單一 record。→ Task 4 合計測試 + Task 3 unchanged/failed 零合計測試
8. 工具鏈全綠（basedpyright 0 error、ruff、pytest）。→ 各 Task 結尾 + Task 5 完整套件
