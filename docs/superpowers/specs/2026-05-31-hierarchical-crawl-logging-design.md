# 階層化 crawl/sync 處理 log — 設計規格

日期：2026-05-31

## 1. 目標

讓 crawl/sync 路徑在處理每個 product 時，輸出一棵**階層化、人眼可快速分辨**的處理樹，涵蓋本次 item 級索引新增的每個決策點：品項拆解（合併/排除）、`detail_snippet` 擷取、typing 分類（決策來源）、embedding 狀態。階層化是為了在大量 log 中快速定位「哪個 product 的哪個品項發生什麼」。

## 2. 非目標（Out of Scope）

- 不區分 `run` 與 `crawl` 入口——兩者共用 `sync_products`，行為一致（同樣輸出處理樹）。
- 不改變任何業務行為、回傳值「**數值**」、控制流或估價結果；只新增 logging 與**三個內部函式的回傳型別**（`classify_item`、`decompose_items`、`_rebuild_product_items`——純資訊承載，數值不變）。
- 不引入結構化（JSON）log、不加結構化欄位（`store=..` `pid=..`）——純文字樹。
- 不改 `classify_query` 的**對外回傳型別與輸出語義**（仍 `list[str]`、`その他→[]`）；其**內部 body** 需配合 `_llm_classify` 改回 tuple 而解構（§7.1，純機械式、輸出不變）。`classify_query` 不在 crawl 樹內。
- 不改既有 store 級 log（queue 大小、每-20 心跳、done 統計）與 stdout 的 crawl JSON counters。

## 3. 現況

- crawl 路徑：`async_process_queue`（[async_pipeline.py](../../../estimator_king/crawler/async_pipeline.py)，`concurrency_per_domain` 個 worker 併發 drain queue）→ 每 entry `fetch_product` → `asyncio.to_thread(sync_products, [一個 product], ...)`。
- 既有 log：store 級 `queue: N entries`、每 `_PROGRESS_LOG_EVERY=20` 心跳、`done: created/.../failed`；`sync_products` 只在失敗時 `logger.exception("Sync failed for %s")`；typing 只在 LLM 例外時 log。**逐 product/逐品項的決策完全沒有可見性**。
- 訊息語言：既有 log 全為英文，格式 `%(asctime)s [%(levelname)s] %(name)s: %(message)s`。

## 4. 架構

### 4.1 樹在 `sync_products` 累積、單一 record 原子輸出

每個 worker 對單一 product 呼叫 `sync_products`（於 `asyncio.to_thread`，多執行緒）。在該呼叫內把整棵樹**累積成一個多行字串**，於該 product 處理完時用**單一 `logger.info(tree)`** 輸出。Python `logging` 對單筆 record 的寫出是原子的 → 併發下每棵樹完整不被其他 product 切斷。

- **代價（可接受）**：樹在 product 處理**完成後**才一次出現（非逐行即時），且以**完成順序**排列（非 enqueue 順序）。
- 因為走共用的 `sync_products`，**run 與 crawl 自動一致**。
- **單一 message 設計**：整棵樹是**一個** `message`（含 `\n`）。在 `%(asctime)s [%(levelname)s] %(name)s: %(message)s` 格式下，只有**首行**帶 `asctime/level/name` 前綴，其餘樹行為裸行——此為刻意設計（樹更易讀）；**不得**為了補前綴而把樹拆成多筆 record（會重新引入交錯）。原子性成立的前提：`asyncio.to_thread` 為真執行緒 + stdlib `StreamHandler.emit` 持鎖逐筆寫出。

### 4.2 log level 與 skipped 收斂

- 整棵樹在 **INFO**（預設可見）。
- **未變動（`unchanged`/skipped）product** → 不展開整棵樹，只出**一行** INFO（見 §5）。每輪只處理當日預算（`max_products_per_run`）+ 新品，量可控；`crawl --force-refetch` 才全量展開。
- embedding／typing 失敗使整個 product 進入既有 `except`（[engine.py:116](../../../estimator_king/sync/engine.py)）：維持現行 `logger.exception("Sync failed for %s")`，**該 product 不輸出樹**（已知取捨：失敗 product 看 exception log + traceback，而非樹）。
- **部分失敗的已知取捨**：若某 product 中後段 item 的 embed/upsert 拋例外，整個 product 走 `except`、不輸出樹；該 product 內**先前已成功 upsert 的 item 也不會有任何樹節點可見性**（與 §1 的逐品項可見性目標相違，但屬可接受取捨——失敗時以 exception log 定位，逐 item 失敗可見性留待日後）。因此 §6 的 `embed` 二態僅涵蓋**整 product 成功**路徑。

### 4.3 保留既有外層 log

`async_process_queue` 的 store 級 log（`queue: N entries` / 每-20 心跳 / `done:` 統計）與 `cycle.py` 的 store 處理 log **全部保留不動**，作為樹的外層脈絡（最小 blast radius、不動既有 logging 測試）。

## 5. 樹格式（英文、純文字、INFO）

成功處理（created 或 updated）的 product 輸出單一多行 record：

```text
product hololive:8069581111516 "アイラニ・イオフィフティーン 誕生日記念2023" (updated): 5 items, 2 excluded (SET×1, ¥0×1)
  ├─ item "Eternity アクリルジオラマスタンド" ×1
  │    detail=hit  typing=アクリルスタンド(vocab)  embed=indexed
  ├─ item "3Dアクリルスタンド Blue Journey衣装ver." ×23 talents=23
  │    detail=miss  typing=アクリルスタンド(cache)  embed=skipped(unchanged)
  └─ item "謎のグッズ" ×1
       detail=miss  typing=その他(llm)  embed=indexed
```

未變動 product（單行）：

```text
product hololive:8069581111516 "アイラニ・イオフィフティーン 誕生日記念2023" skipped (unchanged)
```

格式規則（縮排前綴以**確切字串**定義，避免實作者各自猜空白）：
- 根行：`product {store_id}:{product_id} "{title}" ({created|updated}): {N} items, {E} excluded (SET×{s}, ¥0×{z})`。`E==0` 時省略 `, {E} excluded (...)`，只到 `{N} items`。
- 每 item 兩行：
  - 第一行（連接行）前綴：非最後一筆用 `"  ├─ "`、最後一筆（含**單一 item**）用 `"  └─ "`，接 `item "{item_name}" ×{合併 variant 數}`，`talents` 數 >0 時再接 ` talents={n}`。
  - 第二行（續行）前綴：非最後一筆用 `"  │    "`（2 空白 + `│` + 4 空白）、最後一筆用 `"       "`（7 空白），接 `detail={hit|miss}  typing={item_type}({source})  embed={indexed|skipped(unchanged)}`（欄位間 2 空白）。
- 標籤一律英文；`item_name`／`item_type` 值本身保留原始日文。
- `¥0×{z}` 的 `z` 為「**無有效正價**」被丟棄的 variant 數——涵蓋價格字面為 `0` 與**無法解析**（如 `"N/A"`）兩種；`¥0` 為此語義的簡寫標籤（非僅字面 0）。
- §9 測試對樹**斷言子字串**（如 `detail=`, `typing=アクリルスタンド(vocab)`, `×23`, `SET×1`, `embed=skipped(unchanged)`）與節點計數，**不**對逐字空白做精確比對（縮排前綴雖已確切定義，但測試以子字串為準以降低脆性）。

## 6. 各節點欄位語義

- **detail**：`hit`（`ProductItem.detail_snippet` 非空）/ `miss`（空）。
- **typing**：`{item_type}({source})`，`source`：
  - `vocab` — 第一層受控詞彙**唯一命中**（零 LLM）；此路徑不可能產生 `その他`。
  - `cache` — 第二層命中快取。
  - `llm` — 第二層**任何非快取路徑**：新呼叫 LLM（含多重命中、零命中）、LLM 回傳**非詞彙值**的後驗證 fallback `その他`、以及 `classify_via_llm` **例外** fallback `その他`，全部歸 `llm`。
  - 註：`source` 表示「答案從哪裡來」，與 `item_type` 值無關。`その他` 可搭配 `cache` 或 `llm`（快取曾存過 `その他` 時 → `その他(cache)`），故 `その他(cache)` 為合法狀態，**不可**假設 `その他` 必為 `llm`。
- **embed**：`indexed`（新/變動，已呼叫 embedder upsert）/ `skipped(unchanged)`（`item_hash` 與既有相同，跳過重嵌）。
- **item ×N / talents**：`N = len(ProductItem.source_variant_ids)`、`talents = len(ProductItem.talents)`。
- **排除計數**：`SET×s`（option 前綴以「セット」開頭被丟棄的 variant 數）、`¥0×z`（價格解析為 0 或無法解析被丟棄的 variant 數）。

## 7. 資料流改動（為了取得決策資料）

僅改三個內部函式的回傳型別（`classify_item`、`decompose_items`、`_rebuild_product_items`）並新增純格式化 helper `_format_product_tree`；數值行為不變。

### 7.1 `classify_item` 回傳決策來源

`estimator_king/sync/typing.py`：

- 新增 `@dataclass(frozen=True) class TypeDecision: item_type: str; source: str`（`source ∈ {"vocab","cache","llm"}`）。
- `_llm_classify(...)` 改回傳 `tuple[str, str]`（`(item_type, source)`）：cache 命中 → `("...","cache")`；否則呼叫 LLM（含後驗證／例外 fallback `その他`）→ `("...","llm")`。
- `classify_item(...) -> TypeDecision`：第一層唯一命中 → `TypeDecision(hit, "vocab")`；否則用 `_llm_classify` 的 `(item_type, source)` 組 `TypeDecision`。engine 呼叫點改動見 §7.3。
- `classify_query(...)`：**對外回傳型別不變（仍 `list[str]`）**，但**內部 body 必須改**以解構 `_llm_classify` 的新 tuple——現況為 `result = _llm_classify(...); return [] if result == OTHER else [result]`，改為 `item_type, _ = _llm_classify(...); return [] if item_type == OTHER else [item_type]`。**若不改會造成迴歸**：`tuple == "その他"` 恆為 False，零命中時會錯誤回傳 `[("その他","llm")]` 而非 `[]`。
- 連動：
  - engine（§7.3）改用 `classify_item(...).item_type` 寫 `_format_item_document`／`_item_hash`、`.source` 進報告。
  - `test_typing.py` 既有對 `classify_item` 回傳 `str` 的斷言改為 `.item_type`；新增對 `.source` 三態（vocab/cache/llm）的斷言；**新增守護測試**：`classify_query` 在零命中且 LLM 回 `その他` 時仍回 `[]`（驗證 Issue-1 不迴歸）。

### 7.2 `decompose_items` 回傳排除計數

`estimator_king/sync/items.py`：

- 新增 `@dataclass(frozen=True) class DecomposeResult: items: list[ProductItem]; excluded_set: int; excluded_zero: int`。
- `decompose_items(...) -> DecomposeResult`：在既有 step 1+2 過濾迴圈累計 `excluded_set`（option 前綴以「セット」開頭）與 `excluded_zero`（價格 None 或 0）計數；回傳 `DecomposeResult(items=..., excluded_set=..., excluded_zero=...)`。
- 每品項合併資訊**不需新增**（已在 `ProductItem.source_variant_ids`／`talents`）。
- 連動：`test_items.py`（7 個測試函式）中將 `decompose_items(...)` 結果當 list 使用之處改為 `.items`（機械式）。`classify_via_llm` 及其測試 fakes **不變**（只有 `classify_item`／`classify_query`／`_llm_classify` 改動；`test_engine_items.py` 的 `FakeTypingProvider.classify_via_llm` 仍回 `str`，不受影響）。

### 7.3 engine 累積並輸出樹

`estimator_king/sync/engine.py`：

- 新增純函式 `_format_product_tree(...)`：輸入 product 根資訊 + 每 item 的（name, n_variants, n_talents, detail_hit, item_type, typing_source, embed_status）清單 + 排除計數，回傳多行樹字串（§5 格式）。
- `_rebuild_product_items` 改為**收集**每 item 的決策列（`detail_hit`、`TypeDecision`、`embed_status ∈ {"indexed","skipped(unchanged)"}`）並回傳一個 `RebuildReport`（`item_rows: list[...]`, `excluded_set`, `excluded_zero`）供 `sync_products` 組樹。**`RebuildReport` 只承載 item 列與排除計數**；`created`/`updated` 動詞**由 `sync_products` 依 `state is None` 決定**並傳入 `_format_product_tree`（報告本身不含 created/updated）。`classify_item` 改用 `.item_type` 寫 `_format_item_document`／`_item_hash`，`.source` 進報告。embed skip 分支記 `"skipped(unchanged)"`，否則 upsert 後記 `"indexed"`。
- `sync_products`：
  - `unchanged` 分支 → `logger.info` 單行 skipped（§5）。
  - 成功重建分支 → 以 `RebuildReport` + 根資訊（created/updated 由 `state is None` 決定）呼叫 `_format_product_tree` → 單一 `logger.info(tree)`。
  - `except` 分支 → 維持既有 `logger.exception("Sync failed for %s", external_key)`（不輸出樹）。

## 8. 錯誤處理

- typing LLM 例外：維持既有 `_llm_classify` catch → `その他`，source 記 `llm`（樹顯示 `typing=その他(llm)`），不阻斷。
- embedding／vector 例外：整個 product 走既有 `except`，輸出既有 exception log，不輸出樹（§4.2 已知取捨）。
- 樹字串組裝本身不得拋例外（純格式化既有資料）。

## 9. 測試（pytest，沿用 `*_logging.py` + caplog 慣例）

- `tests/test_engine_logging.py`（新增，caplog at INFO）：
  - created/updated product 輸出**單筆** record 含完整樹：根行含 item 數 + 排除計數（`SET×N, ¥0×M`）；item 行含 `×N`／`talents=`；續行含 `detail=hit|miss`、`typing=<type>(vocab|cache|llm)` 三態、`embed=indexed|skipped(unchanged)`。用 fake embedder/vectorstore/typing-provider + in-memory repo 驅動（沿用 `test_engine_items.py` 的 fakes）。
  - skipped（unchanged，第二次同內容 sync）→ 單行 `skipped (unchanged)`，**不**含樹節點字元。
  - 樹原子性：整棵樹在**同一筆** `caplog.records`（單一 message 含換行），而非多筆。
- `tests/test_typing.py`（更新）：`classify_item` 回傳 `TypeDecision`；`.source` == `vocab`（唯一命中）/`cache`（快取命中）/`llm`（新呼叫、多重命中、零命中）；`classify_query` 仍回 `list[str]` 不變。
- `tests/test_items.py`（更新）：改用 `decompose_items(...).items`；新增 `DecomposeResult.excluded_set`/`excluded_zero` 計數斷言（SET 與 ¥0 各被計數）。
- `tests/test_engine_items.py`（更新）：既有對 `sync_products` 行為的斷言不變（行為未改）；若內部改用 `RebuildReport`／`DecomposeResult`，確保 created/updated/skipped 計數與向量結果一致。
- 工具鏈：`.venv/bin/basedpyright estimator_king`（prod 0 error）、`uvx ruff check estimator_king tests`、相關 `pytest -o addopts=""`。

## 10. 驗收標準

1. 對一個含「合併品項 / 混合品項 / SET / ¥0 / 各種 typing 來源」的 product 跑一次 sync，INFO log 出現**單筆**多行樹，節點與 §5/§6 相符（item ×N、talents、detail hit/miss、typing 三態來源、embed indexed/skipped、排除計數）。
2. 第二次同內容 sync → 單行 `skipped (unchanged)`，無樹。
3. 併發（≥2 worker）下，不同 product 的樹不互相交錯（各自單筆 record）。
4. 無業務行為改變：`sync_products`/`decompose_items`/`classify_item` 的數值結果（向量、價格、類型、created/updated/skipped 計數）與改動前一致。
5. 既有 store 級 log（queue/heartbeat/done）與 crawl stdout JSON counters 不變。
6. 工具鏈全綠（basedpyright 0 error、ruff、pytest）。
