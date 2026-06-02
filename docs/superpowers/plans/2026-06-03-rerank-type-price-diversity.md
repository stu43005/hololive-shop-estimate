# 檢索 rerank 加入 item_type／price_jpy 多樣性 + 候選池加深 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `/estimate` 查詢端的 rerank 加入 `(item_type, price_jpy)` 貪婪 MMR 多樣性偏好，並新增候選池加深旋鈕，讓送進 chat model 的參考價格分佈更分散、recency 更有換進池外較新同類品的槓桿，而 recency 行為與送進 LLM 的參考數皆不變。

**Architecture:** 純查詢端改動，集中在 `estimator_king/bot/estimator.py`：`_rerank` 由一次性 `sorted` 改為逐筆貪婪挑選（`score = base − diversity_weight × dup_count`，key 為 exact `(item_type, price_jpy)`，遞增計數懲罰，平手取池序最前），base 完整沿用現有 recency；`_estimate_chunk` 每個 where-query 的 `n_results` 由 `top_k` 改為 `top_k × fetch_multiplier`，合併去重與 `[: top_k]` 不變。兩個旋鈕經 `AppConfig`／`load_config`／`Estimator.__init__`／`build_bot`／`stores_config.yaml` 接線，與既有 `recency_weight` 同模式。不改 metadata、不重嵌向量。

**Tech Stack:** Python 3, ChromaDB（cosine distance），pytest + duck-typed fakes，basedpyright（prod 0 error），ruff。

**驗證工具鏈**（每個 Task 收尾必跑，見 [CLAUDE.md](../../../CLAUDE.md)）：

- 型別：`.venv/bin/basedpyright estimator_king`（**prod code 0 error**；test 檔的 duck-typed fakes 既有 `reportArgumentType` 噪音屬慣例，非回歸）
- Lint：`uvx ruff check <paths>`
- 單檔測試：`.venv/bin/python -m pytest <path> -v -o addopts=""`（**勿用** `-p no:cov`）

---

## File Structure

| 檔案 | 責任 | 本計畫變更 |
| --- | --- | --- |
| `estimator_king/config_schema.py` | 結構性設定 dataclass + YAML/env 載入 | 新增 `estimator_diversity_weight`、`estimator_fetch_multiplier` 欄位與解析（Task 1） |
| `estimator_king/bot/estimator.py` | `/estimate` 檢索 + rerank + chat 呼叫 | `__init__` 新增兩旋鈕；`_rerank` 改貪婪 MMR；`_estimate_chunk` 查詢加深（Task 2、3） |
| `estimator_king/bot/runner.py` | 建構並接線 `Estimator` | `Estimator(...)` 傳入兩旋鈕（Task 4） |
| `stores_config.yaml` | 執行期可調設定 | `estimator:` 區塊新增兩行（Task 4） |
| `tests/test_config_schema.py` | `load_config` 解析測試 | 新增兩旋鈕解析 + 缺鍵回落斷言（Task 1） |
| `tests/test_estimator.py` | Estimator 行為測試 | `_estimator` helper 擴參；`RecordingVectorStore` 記錄 `n_results`；新增多樣性 / 加深測試（Task 2、3） |

執行順序依相依層級：Task 1（設定層，獨立）→ Task 2（rerank 行為）→ Task 3（檢索加深，沿用 Task 2 的 `__init__`）→ Task 4（接線 + yaml，把 Task 1 的設定接到 Task 2/3 的建構子）。

---

## Task 1: 設定層新增 `diversity_weight` 與 `fetch_multiplier`

**Files:**

- Modify: `estimator_king/config_schema.py`（`AppConfig` 欄位區 line 126 附近；`load_config` 建構區 line 287 附近）
- Test: `tests/test_config_schema.py`

- [ ] **Step 1: 寫失敗測試**

在 `tests/test_config_schema.py` 的 `test_load_config_parses_typing_and_estimator_sections` 做兩處 surgical 編輯（保留既有 `pc = cfg.build_provider_config()` 等尾端斷言，**勿整段替換**）。

編輯 A — `estimator:` YAML heredoc 補上兩鍵，將：

```python
        estimator:
          top_k: 7
          recency_weight: 0.1
    """)
```

改為：

```python
        estimator:
          top_k: 7
          recency_weight: 0.1
          diversity_weight: 0.2
          fetch_multiplier: 3
    """)
```

編輯 B — 在 `estimator_recency_weight` 斷言後、`pc =` 之前插入兩行，將：

```python
    assert cfg.estimator_top_k == 7
    assert cfg.estimator_recency_weight == 0.1
    pc = cfg.build_provider_config()
```

改為：

```python
    assert cfg.estimator_top_k == 7
    assert cfg.estimator_recency_weight == 0.1
    assert cfg.estimator_diversity_weight == 0.2
    assert cfg.estimator_fetch_multiplier == 3
    pc = cfg.build_provider_config()
```

並在 `test_load_config_defaults_when_sections_absent` 末尾（`assert cfg.estimator_recency_weight == 0.05` 之後）新增：

```python
    assert cfg.estimator_diversity_weight == 0.05
    assert cfg.estimator_fetch_multiplier == 2
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `.venv/bin/python -m pytest tests/test_config_schema.py -v -o addopts=""`
Expected: FAIL — `AttributeError: 'AppConfig' object has no attribute 'estimator_diversity_weight'`

- [ ] **Step 3: 實作 `AppConfig` 欄位**

在 `estimator_king/config_schema.py` 的 `AppConfig`，將：

```python
    estimator_top_k: int = 10
    estimator_recency_weight: float = 0.05
```

改為：

```python
    estimator_top_k: int = 10
    estimator_recency_weight: float = 0.05
    estimator_diversity_weight: float = 0.05
    estimator_fetch_multiplier: int = 2
```

- [ ] **Step 4: 實作 `load_config` 解析**

在 `estimator_king/config_schema.py` 的 `load_config`，將：

```python
        estimator_top_k=int(est.get("top_k", 10)),
        estimator_recency_weight=float(est.get("recency_weight", 0.05)),
```

改為：

```python
        estimator_top_k=int(est.get("top_k", 10)),
        estimator_recency_weight=float(est.get("recency_weight", 0.05)),
        estimator_diversity_weight=float(est.get("diversity_weight", 0.05)),
        estimator_fetch_multiplier=int(est.get("fetch_multiplier", 2)),
```

- [ ] **Step 5: 跑測試確認通過**

Run: `.venv/bin/python -m pytest tests/test_config_schema.py -v -o addopts=""`
Expected: PASS（2 passed）

- [ ] **Step 6: 型別 + lint**

Run: `.venv/bin/basedpyright estimator_king && uvx ruff check estimator_king tests/test_config_schema.py`
Expected: prod code 0 error；ruff All checks passed

- [ ] **Step 7: Commit**

```bash
git add estimator_king/config_schema.py tests/test_config_schema.py
git commit -m "feat(config): add estimator diversity_weight and fetch_multiplier"
```

---

## Task 2: `_rerank` 改為貪婪 MMR 多樣性

**Files:**

- Modify: `estimator_king/bot/estimator.py`（`Estimator.__init__` line 71-82；`_rerank` line 130-146）
- Test: `tests/test_estimator.py`

- [ ] **Step 1: 擴充測試 helper 並寫失敗測試**

在 `tests/test_estimator.py`，把 `_estimator` helper 改為可注入 `diversity`（預設 `0.0`，使既有 recency 測試維持純 base 行為）：

```python
def _estimator(vs, chat, typing=None, top_k=10, recency=0.05, diversity=0.0):
    return Estimator(FakeEmbedder(), chat, vs, typing_provider=(typing or FakeTypingProvider()),
                     item_types=["ぬいぐるみ"], item_types_version=1,
                     top_k=top_k, recency_weight=recency, diversity_weight=diversity)
```

並在檔案末尾新增以下測試（皆用預設 `FakeTypingProvider()`，其 `answer="その他"` 不在 `item_types`，故只發一次純 `None` 查詢，`RecordingVectorStore` 每次回傳完整 hit 清單、merge 後去重保留全部相異 id）：

```python
def test_diversity_promotes_distinct_keys_into_top_k():
    # 3 筆同 (ぬいぐるみ,500)（相似度遞減）+ 1 筆不同價 + 1 筆不同類型
    hits = [_hit("dup1", "ぬいぐるみ", 500, 0, 0.10),
            _hit("dup2", "ぬいぐるみ", 500, 0, 0.11),
            _hit("dup3", "ぬいぐるみ", 500, 0, 0.12),
            _hit("diffprice", "ぬいぐるみ", 900, 0, 0.13),
            _hit("difftype", "タオル", 500, 0, 0.14)]

    base_chat = FakeChat([_est("x")])
    _estimator(RecordingVectorStore(hits), base_chat, top_k=3, diversity=0.0).estimate_products(["x"], "u")
    p0 = base_chat.last_user_prompt
    # 無多樣性：純相似度序，top3 全是同鍵 dup1/dup2/dup3
    assert "dup2" in p0 and "dup3" in p0
    assert "diffprice" not in p0 and "difftype" not in p0

    div_chat = FakeChat([_est("x")])
    _estimator(RecordingVectorStore(hits), div_chat, top_k=3, diversity=0.05).estimate_products(["x"], "u")
    p1 = div_chat.last_user_prompt
    # 有多樣性：同鍵第 2/3 筆被推後，diffprice/difftype 進入 top3
    assert "diffprice" in p1 and "difftype" in p1
    assert "dup2" not in p1 and "dup3" not in p1


def test_same_type_different_price_not_penalized():
    # 同 item_type、價格各異 → 鍵不同 → 不互相懲罰，順序純由 base（相似度）決定
    hits = [_hit("hi", "ぬいぐるみ", 500, 0, 0.10),
            _hit("lo", "ぬいぐるみ", 900, 0, 0.20)]
    chat = FakeChat([_est("x")])
    _estimator(RecordingVectorStore(hits), chat, top_k=2, diversity=0.5).estimate_products(["x"], "u")
    p = chat.last_user_prompt
    assert p.index("hi") < p.index("lo")


def test_diversity_zero_degenerates_to_base_sort():
    # diversity=0 → 等同現有純 base 降冪排序（相似度序）
    # 用多字元 id（"a"/"b"/"c" 會與 prompt header 子字串碰撞，str.index 會誤抓 header）
    hits = [_hit("aaa", "ぬいぐるみ", 500, 0, 0.30),
            _hit("bbb", "ぬいぐるみ", 500, 0, 0.10),
            _hit("ccc", "ぬいぐるみ", 500, 0, 0.20)]
    chat = FakeChat([_est("x")])
    _estimator(RecordingVectorStore(hits), chat, top_k=3, diversity=0.0).estimate_products(["x"], "u")
    p = chat.last_user_prompt
    assert p.index("bbb") < p.index("ccc") < p.index("aaa")


def test_diversity_tie_breaks_by_pool_order():
    # base 相同、鍵不同 → 取候選池順序在前者（決定性）
    hits = [_hit("first", "ぬいぐるみ", 500, 0, 0.10),
            _hit("second", "タオル", 500, 0, 0.10)]
    chat = FakeChat([_est("x")])
    _estimator(RecordingVectorStore(hits), chat, top_k=2, diversity=0.5).estimate_products(["x"], "u")
    p = chat.last_user_prompt
    assert p.index("first") < p.index("second")
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `.venv/bin/python -m pytest tests/test_estimator.py -v -o addopts=""`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'diversity_weight'`

- [ ] **Step 3: `Estimator.__init__` 新增 `diversity_weight`**

在 `estimator_king/bot/estimator.py`，將 `__init__` 簽名與賦值：

```python
    def __init__(self, embedder: _Embedder, chat: _Chat, vector_store: _VectorStore,
                 typing_provider: _TypingProvider, *, item_types: list[str],
                 item_types_version: int, top_k: int = 10,
                 recency_weight: float = 0.05) -> None:
        self._embedder = embedder
        self._chat = chat
        self._vector_store = vector_store
        self._typing_provider = typing_provider
        self._item_types = item_types
        self._item_types_version = item_types_version
        self._top_k = top_k
        self._recency_weight = recency_weight
```

改為：

```python
    def __init__(self, embedder: _Embedder, chat: _Chat, vector_store: _VectorStore,
                 typing_provider: _TypingProvider, *, item_types: list[str],
                 item_types_version: int, top_k: int = 10,
                 recency_weight: float = 0.05,
                 diversity_weight: float = 0.05) -> None:
        self._embedder = embedder
        self._chat = chat
        self._vector_store = vector_store
        self._typing_provider = typing_provider
        self._item_types = item_types
        self._item_types_version = item_types_version
        self._top_k = top_k
        self._recency_weight = recency_weight
        self._diversity_weight = diversity_weight
```

- [ ] **Step 4: 改寫 `_rerank` 為貪婪 MMR**

在 `estimator_king/bot/estimator.py`，將整個 `_rerank`：

```python
    def _rerank(self, hits: list[_Hit]) -> list[_Hit]:
        pubs = [int(h.metadata.get("published_at", 0) or 0) for h in hits]
        positive = [p for p in pubs if p > 0]
        min_pub = min(positive) if positive else 0
        max_pub = max(positive) if positive else 0
        span = max_pub - min_pub

        def score(h: _Hit) -> float:
            similarity = 1.0 - h.distance
            pub = int(h.metadata.get("published_at", 0) or 0)
            if span > 0 and pub > 0:
                recency = (pub - min_pub) / span
            else:
                recency = 0.0
            return similarity + self._recency_weight * recency

        return sorted(hits, key=score, reverse=True)
```

改為：

```python
    def _rerank(self, hits: list[_Hit]) -> list[_Hit]:
        pubs = [int(h.metadata.get("published_at", 0) or 0) for h in hits]
        positive = [p for p in pubs if p > 0]
        min_pub = min(positive) if positive else 0
        max_pub = max(positive) if positive else 0
        span = max_pub - min_pub

        def base(h: _Hit) -> float:
            similarity = 1.0 - h.distance
            pub = int(h.metadata.get("published_at", 0) or 0)
            if span > 0 and pub > 0:
                recency = (pub - min_pub) / span
            else:
                recency = 0.0
            return similarity + self._recency_weight * recency

        def key_of(h: _Hit) -> tuple[str, int]:
            return (str(h.metadata.get("item_type", "") or ""),
                    int(h.metadata.get("price_jpy", 0) or 0))

        base_by_id = {h.id: base(h) for h in hits}
        selected: list[_Hit] = []
        selected_keys: list[tuple[str, int]] = []
        remaining = list(hits)
        while remaining:
            best_i = 0
            best_score: float | None = None
            for i, h in enumerate(remaining):
                dup = sum(1 for k in selected_keys if k == key_of(h))
                adjusted = base_by_id[h.id] - self._diversity_weight * dup
                if best_score is None or adjusted > best_score:
                    best_score = adjusted
                    best_i = i
            picked = remaining.pop(best_i)
            selected.append(picked)
            selected_keys.append(key_of(picked))
        return selected
```

說明（不寫進程式碼）：`diversity_weight = 0` 時 `adjusted == base_by_id[h.id]`，貪婪「取最大、平手取 remaining 最小索引」與 Python `sorted(reverse=True)` 的穩定降冪等價，故完全退化回現有行為；`base` 內 recency 計算逐字沿用，行為不變；候選池 id 由上游 `merged` dict 保證唯一，`base_by_id` 以 id 為鍵安全。

- [ ] **Step 5: 跑測試確認通過**

Run: `.venv/bin/python -m pytest tests/test_estimator.py -v -o addopts=""`
Expected: PASS（含既有 recency / 格式 / merge 測試與 4 個新測試）

- [ ] **Step 6: 型別 + lint**

Run: `.venv/bin/basedpyright estimator_king && uvx ruff check estimator_king tests/test_estimator.py`
Expected: prod code 0 error；ruff All checks passed

- [ ] **Step 7: Commit**

```bash
git add estimator_king/bot/estimator.py tests/test_estimator.py
git commit -m "feat(estimate): greedy-MMR rerank for type/price diversity"
```

---

## Task 3: 候選池加深 `fetch_multiplier`

**Files:**

- Modify: `estimator_king/bot/estimator.py`（`Estimator.__init__`；`_estimate_chunk` 檢索迴圈 line 115）
- Test: `tests/test_estimator.py`

- [ ] **Step 1: 擴充 fake + helper 並寫失敗測試**

在 `tests/test_estimator.py`，讓 `RecordingVectorStore` 記錄收到的 `n_results`（additive，不影響既有測試）：

```python
class RecordingVectorStore:
    def __init__(self, hits):
        self._hits = hits
        self.where_calls = []
        self.n_results_calls = []

    def query(self, embedding, n_results, where=None):
        self.where_calls.append(where)
        self.n_results_calls.append(n_results)
        return list(self._hits)
```

把 `_estimator` helper 再加一個 `fetch_mult`（預設 `1`，使既有測試維持現況池大小）：

```python
def _estimator(vs, chat, typing=None, top_k=10, recency=0.05, diversity=0.0, fetch_mult=1):
    return Estimator(FakeEmbedder(), chat, vs, typing_provider=(typing or FakeTypingProvider()),
                     item_types=["ぬいぐるみ"], item_types_version=1,
                     top_k=top_k, recency_weight=recency,
                     diversity_weight=diversity, fetch_multiplier=fetch_mult)
```

在檔案末尾新增：

```python
def test_fetch_multiplier_deepens_query_size():
    vs = RecordingVectorStore([_hit("a", "ぬいぐるみ", 500, 0, 0.1)])
    chat = FakeChat([_est("x")])
    _estimator(vs, chat, top_k=10, fetch_mult=2).estimate_products(["x"], "u")
    assert vs.n_results_calls and all(n == 20 for n in vs.n_results_calls)


def test_fetch_multiplier_one_matches_top_k():
    vs = RecordingVectorStore([_hit("a", "ぬいぐるみ", 500, 0, 0.1)])
    chat = FakeChat([_est("x")])
    _estimator(vs, chat, top_k=10, fetch_mult=1).estimate_products(["x"], "u")
    assert vs.n_results_calls and all(n == 10 for n in vs.n_results_calls)


def test_fetch_multiplier_still_sends_only_top_k_to_chat():
    # 20 筆相異價格（鍵全相異 → 多樣性不收斂），加深後送進 chat 仍限 top_k 筆
    hits = [_hit(f"h{i}", "ぬいぐるみ", 100 + i, 0, 0.10 + i * 0.01) for i in range(20)]
    vs = RecordingVectorStore(hits)
    chat = FakeChat([_est("x")])
    _estimator(vs, chat, top_k=5, fetch_mult=2).estimate_products(["x"], "u")
    assert chat.last_user_prompt.count("- h") == 5
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `.venv/bin/python -m pytest tests/test_estimator.py -v -o addopts=""`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'fetch_multiplier'`

- [ ] **Step 3: `Estimator.__init__` 新增 `fetch_multiplier`**

在 `estimator_king/bot/estimator.py`，將 `__init__` 簽名尾段與賦值（Task 2 後的版本）：

```python
                 recency_weight: float = 0.05,
                 diversity_weight: float = 0.05) -> None:
```

改為：

```python
                 recency_weight: float = 0.05,
                 diversity_weight: float = 0.05,
                 fetch_multiplier: int = 2) -> None:
```

並在 `self._diversity_weight = diversity_weight` 之後新增：

```python
        self._fetch_multiplier = fetch_multiplier
```

- [ ] **Step 4: `_estimate_chunk` 加深檢索**

在 `estimator_king/bot/estimator.py` 的 `_estimate_chunk`，將：

```python
            for where in queries:
                for hit in self._vector_store.query(embedding, self._top_k, where=where):
```

改為：

```python
            for where in queries:
                fetch_n = self._top_k * self._fetch_multiplier
                for hit in self._vector_store.query(embedding, fetch_n, where=where):
```

（合併去重與其後 `self._rerank(...)[: self._top_k]` 維持不變。）

- [ ] **Step 5: 跑測試確認通過**

Run: `.venv/bin/python -m pytest tests/test_estimator.py -v -o addopts=""`
Expected: PASS（含 Task 2 測試與 3 個新測試）

- [ ] **Step 6: 型別 + lint**

Run: `.venv/bin/basedpyright estimator_king && uvx ruff check estimator_king tests/test_estimator.py`
Expected: prod code 0 error；ruff All checks passed

- [ ] **Step 7: Commit**

```bash
git add estimator_king/bot/estimator.py tests/test_estimator.py
git commit -m "feat(estimate): deepen candidate pool via fetch_multiplier"
```

---

## Task 4: 接線 `build_bot` + `stores_config.yaml`

**Files:**

- Modify: `estimator_king/bot/runner.py`（`build_bot` 的 `Estimator(...)` line 47-53）
- Modify: `stores_config.yaml`（`estimator:` 區塊 line 274-276）

- [ ] **Step 1: `build_bot` 傳入兩旋鈕**

在 `estimator_king/bot/runner.py`，將：

```python
    estimator = Estimator(
        embedder, chat, vector_store, typing_provider,
        item_types=config.item_types,
        item_types_version=config.item_types_version,
        top_k=config.estimator_top_k,
        recency_weight=config.estimator_recency_weight,
    )
```

改為：

```python
    estimator = Estimator(
        embedder, chat, vector_store, typing_provider,
        item_types=config.item_types,
        item_types_version=config.item_types_version,
        top_k=config.estimator_top_k,
        recency_weight=config.estimator_recency_weight,
        diversity_weight=config.estimator_diversity_weight,
        fetch_multiplier=config.estimator_fetch_multiplier,
    )
```

- [ ] **Step 2: `stores_config.yaml` 新增兩行**

在 `stores_config.yaml` 的 `estimator:` 區塊，將：

```yaml
estimator:
  top_k: 10
  recency_weight: 0.05
```

改為：

```yaml
estimator:
  top_k: 10
  recency_weight: 0.05
  diversity_weight: 0.05
  fetch_multiplier: 2
```

- [ ] **Step 3: 型別 + lint**

Run: `.venv/bin/basedpyright estimator_king && uvx ruff check estimator_king`
Expected: prod code 0 error；ruff All checks passed

- [ ] **Step 4: 全測試套件**

Run: `.venv/bin/python -m pytest tests/test_estimator.py tests/test_config_schema.py -v -o addopts=""`
Expected: PASS（全部）

- [ ] **Step 5: Commit**

```bash
git add estimator_king/bot/runner.py stores_config.yaml
git commit -m "feat(estimate): wire diversity_weight and fetch_multiplier into bot"
```

---

## 完成後驗收

- [ ] `.venv/bin/basedpyright estimator_king` → prod code 0 error
- [ ] `uvx ruff check estimator_king tests` → All checks passed
- [ ] `.venv/bin/python -m pytest` → 全套件綠燈
- [ ] 工作樹乾淨，4 個 atomic commit 對應四個 Task

升級無資料遷移：不改 metadata、不重嵌向量；既有 `stores_config.yaml` 缺新鍵時程式回落（`diversity_weight` 0.05、`fetch_multiplier` 2）。
