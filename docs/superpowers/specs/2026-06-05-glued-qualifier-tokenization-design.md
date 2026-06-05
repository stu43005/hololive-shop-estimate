# 黏著修飾詞的 token 化、合併命名與參考行 product 名 — 設計規格

日期：2026-06-05

## 1. 目標

讓 talent 名後**緊黏無空白修飾詞**（真實 hololive 資料中的全形括號語言標記，如 `（日本語）`/`（英語）`/`（インドネシア語）`）的列舉型商品能正確合併；合併後 item 以**共同部分**命名（空則 product 標題）；並讓 `/estimate` 送進 chat model 的參考行帶上 **product 名**，使每筆參考都有 product 脈絡。

外部介面（`/estimate`、`crawl`/`run`）不變；改變的是 `decompose_items` 的 token 化與命名，以及 estimator 參考行格式。

## 2. 現況與根因（已實證）

`_canonical_key`（[items.py:60-69](../../../estimator_king/sync/items.py)）只以**空白**切 token（`normalize_text(residual).split()`）。真實商品 `ホロライブ 秘密の雨の日ボイス ～紫陽花の見える温泉宿で～`（product 9384273215708）的 variant `商品名` 欄為 `<talent>（日本語）` 形式，talent 與括號語言標記**無空白相連**：

- `ときのそら（日本語）` → `.split()` 得單一 token `ときのそら（日本語）`，不等於字典裡的裸 `ときのそら` → talent 不被移除 → canonical key 唯一非空 → **不合併**（45 筆一般 voice 各自獨立）。
- SP voice `ときのそら SPボイス（日本語）` 因 talent 與 `SPボイス` 間**有空白**，`ときのそら` 被切出移除、剩 `SPボイス（日本語）` 為共同 key → 意外合併（×12「SPボイス（日本語）」、×3「SPボイス（英語）」）。

實證（以 1,385 個真實 product 跑修正後邏輯）：修正 token 化後共 759 個合併 item，分佈見 §7.2。根因為「token 化無法處理黏著修飾詞」，與先前移除 `key.strip()` 的變更無關。

## 3. 變更

### 3.1 Token 化（`_canonical_key`，[items.py](../../../estimator_king/sync/items.py)）

把 token 切法從「只切空白」改為「切空白 **+ 全形/半形括號**」，括號內容保留為獨立 token：

```python
_TOKEN_SPLIT = re.compile(r"[\s（）()]+")

def _canonical_key(residual: str, talents: frozenset[str]) -> tuple[str, list[str]]:
    """Drop talent tokens; return (canonical_key, removed_talent_tokens)."""
    kept: list[str] = []
    removed: list[str] = []
    for tok in _TOKEN_SPLIT.split(normalize_text(residual)):
        if not tok:
            continue
        if tok in talents:
            removed.append(tok)
        else:
            kept.append(tok)
    return " ".join(kept), removed
```

- 分隔符為空白與括號 `（）()`。括號內容（如 `日本語`）成為保留 token，併入 canonical key。
- talent 名內的 `・`（如 `アユンダ・リス`、`ハコス・ベールズ`）**不是**分隔符 → talent 仍以完整單 token 比對命中字典、被正確移除。
- **僅變更 `_canonical_key` 的 token 化**；`_meaningful_tokens`（[items.py:56-57](../../../estimator_king/sync/items.py)，供 `_extract_snippet` 使用）**不動**，避免波及 snippet 擷取。
- 範圍限定括號（YAGNI）：不處理 `「」`『』【】等其他括號（真實資料用不到）。

結果（`秘密の雨の日ボイス`）：一般 voice 依語言分組——`日本語`×27、`英語`×12、`インドネシア語`×6；SP voice——`SPボイス 日本語`×12、`SPボイス 英語`×3。

### 3.2 合併命名（`item_name`，[items.py](../../../estimator_king/sync/items.py)）

合併 item（去重分群後 `residual=None`）的名稱改為**共同部分（該組 canonical key）**，key 為空（整串皆 talent）時 fallback product 標題：

- `_Item`（[items.py:129-134](../../../estimator_king/sync/items.py)）新增欄位 `key: str`：合併分支（[items.py:150-153](../../../estimator_king/sync/items.py)）填入該組 canonical key；非合併分支（[items.py:154-157](../../../estimator_king/sync/items.py)）填 `""`（未使用）。
- 命名分支（[items.py:165-171](../../../estimator_king/sync/items.py)）改為：

  ```python
  for ri in raw_items:
      if ri.residual is None:
          name = ri.key.strip() or snapshot.title   # 共同部分；空才用 product 標題
      elif _is_option_value(ri.residual):
          name = f"{snapshot.title} {normalize_text(ri.residual)}".strip()
      else:
          name = ri.residual
      items.append(ProductItem(... item_name=name ...))
  ```

- **移除 `whole_product_single`**（[items.py:161-163](../../../estimator_king/sync/items.py)）：其唯一效果是「整 product 合併時強制 product 標題命名」，現由上式「key 空 → product 標題」涵蓋；key 非空時改用共同部分（如 `Blue Journey衣装ver.`）。此為刻意的命名語義變更。
- **不採用** `{product} / {key}` 形式，也不做子字串抑制：product 脈絡改由 §3.3 的參考行欄位提供，使 item_name 維持為純共同部分、避免 embedding 文件中 product 標題重複（[engine.py:84-88](../../../estimator_king/sync/engine.py) 的文件第 3 行已含 `# {product_title}`）。

### 3.3 Estimator 參考行加 product 名（[estimator.py:173-178](../../../estimator_king/bot/estimator.py)）

`_format_reference` 的參考行加入 `product_title` 欄位（metadata 已存，見 [engine.py:250](../../../estimator_king/sync/engine.py)）：

```python
def _format_reference(self, hit: _Hit) -> str:
    m = hit.metadata
    pub = int(m.get("published_at", 0) or 0)
    date = ...  # 既有日期格式化邏輯不變
    item_name = str(m.get("item_name") or "")
    product_title = str(m.get("product_title") or "")
    fields = [item_name, str(m.get("item_type") or "")]
    if product_title and product_title not in item_name:
        fields.append(product_title)   # 去重：item_name 已含 product 標題（key 空 fallback、或短選項值前綴）時不重複插入
    fields += [f"¥{m.get('price_jpy')}", date, str(m.get("store_id") or "")]
    line = "- " + " | ".join(fields)
    snippet = str(m.get("detail_snippet", "") or "")
    ...  # 既有 snippet 接行邏輯不變
```

- 欄位：`item_name | item_type | [product_title] | ¥price | date | store_id`；**product_title 僅在非空且不是 item_name 子字串時插入**（置於 item_type 之後）。以子字串（非相等）判斷可統一去重兩種 item_name 已含 product 標題的情況：(#2) key 空 fallback（item_name == product 標題）、(#3) 短選項值前綴（item_name == `{product 標題} {殘餘}`）。
- 去重後每筆參考仍帶 product 脈絡：item_name 未含 product 標題者（key 非空合併 #1、一般殘餘 #4）由 product_title 欄位提供；item_name 已含 product 標題者（#2/#3）脈絡已在 item_name 內。
- item_name 命名（[items.py](../../../estimator_king/sync/items.py)）不因此變更，`_is_option_value` 前綴分支保留（item_name 在 log 仍自描述）。
- **子字串（非相等）判斷的取捨**：當 product_title 巧合成為 #1（key 非空合併）或 #4（一般殘餘）item_name 的子字串時也會省略欄位。此為刻意取捨——此類巧合罕見，且省略僅損失冗餘 product 脈絡（item_name 已自含該字串），不致誤導 chat model；不另做 #2/#3 結構性前綴 vs 巧合子字串的區分（YAGNI）。
- **system prompt 不改**：新增欄位為加性、self-descriptive；現有 prompt（[estimator.py](../../../estimator_king/bot/estimator.py) `SYSTEM_PROMPT`）關於以 item_name/detail 對齊的指示仍適用。

## 4. 不變式與 slug 唯一性

- **item_name 必須帶共同部分以避免 slug 撞號**：item_id 為 `{store_id}:{product_id}:{slug(item_name, price)}`（[engine.py:77-80](../../../estimator_king/sync/engine.py)、[engine.py:240](../../../estimator_king/sync/engine.py)）。token 化修正後，同一 product 同價會有多個合併組（如 voice 的 `日本語`/`英語`/`インドネシア語` 同為 1000 円）。若全 fallback 成 product 標題則 slug 相同 → 互相覆寫。§3.2 以共同部分命名確保同 product 同價的不同合併組得到**相異 item_name → 相異 slug**，不撞號。
- **key 空時的 fallback 安全**：同 product 內 key 空的合併組至多一個（key 空且同價即同組、已合併為一），fallback product 標題不致與其他 key 空組同價撞號。
- **既有邊界（非本次引入、不處理）**：key 空的 fallback 與「空殘餘無 talent 的非合併項」（殘餘為空字串時 `_is_option_value("")` 為真、亦命名為 product 標題）理論上可在同 product 同價撞號。此為既有行為（現行碼的純-talent 合併與空殘餘非合併皆已命名為 product 標題），**非本次變更引入**，且需空殘餘 variant 方觸發，屬可接受的既有邊界，本案不處理。
- **同價前提仍成立**：合併仍先按 price 分組（[items.py:125-127](../../../estimator_king/sync/items.py)），命名變更不影響分組。

## 5. 範圍

- **變更檔案**：`estimator_king/sync/items.py`（token 化 + 命名）、`estimator_king/bot/estimator.py`（參考行）。
- **不改** `stores_config.yaml`（無補 talents、無改 `item_types_version`）。後果同前次：本變更為程式邏輯 + 文件格式變更、不影響 `content_hash`，**現有已索引 product 不會立即重新拆解**；僅新 product 或下次 `content_hash` 變動 / 自然重抓者套用新邏輯。屬可接受的漸進生效（如需立即生效可另案 bump `item_types_version`）。
- **無 `engine.py` 邏輯變更**（其 `_format_item_document`/metadata 既有結構不變；item_name 內容改變是 items.py 的產出差異）。
- **無新增第三方相依**。

## 6. 不受影響的既有行為（迴歸保護）

- **themed series 不誤併**（同主題不同品項，無 talent token、無括號）：key 非空且互異 → 不合併、各自以殘餘命名。
- **短選項值併入 product 標題**（`黒 M` 等）：`_is_option_value` 分支不變。
- **SET / ¥0 排除**、**detail_snippet 擷取**：不受 token 化變更影響（snippet 用 `_meaningful_tokens`，未改）。
- **純-talent 列舉**（裸 talent 名、key 空，如 `隣人ボイス2026`）：key 空 → fallback product 標題（行為不變）。

## 7. 真實資料驗收（operational verification）

### 7.1 目標商品

對 `秘密の雨の日ボイス`（product 9384273215708）重跑 `decompose_items`，預期 5 個合併 item，item_name 分別為共同部分：`日本語`(×27)、`英語`(×12)、`インドネシア語`(×6)、`SPボイス 日本語`(×12)、`SPボイス 英語`(×3)，5 者 item_name 相異 → slug 不撞號。

### 7.2 整體分佈（1,385 product 樣本，759 合併 item）

- 共同部分命名（key 非空）：539（如 `アクリルスタンド`、`缶バッジセット`、`コレクションカード`、`日本語`…）。
- fallback product 標題（key 空）：207。
- （先前 `{product}/{key}` 的子字串抑制案例 13 個，在本設計改為純共同部分命名 → 該 13 個 item_name 為其共同部分，如 `Blue Journey衣装ver.`；product 脈絡由 §3.3 參考行欄位提供。）

## 8. 測試

### 8.1 `tests/test_items.py`（沿用 `_snap`/`TALENTS`）

- **更新** `test_talent_variants_merge_to_product_title`：Blue Journey（殘餘 `さくらみこ Blue Journey衣装ver.`）合併後 item_name 改為共同部分 **`Blue Journey衣装ver.`**（非 product 標題）；改名為 `test_talent_variants_merge_named_by_common_part`，斷言 `item_name == "Blue Journey衣装ver."`、`source_variant_ids` 長度、talents 收齊。
- **新增** 黏著括號修飾詞合併：product 標題如 `秘密ボイス`，variant `カテゴリ / さくらみこ（日本語）`、`カテゴリ / 白上フブキ（日本語）`（同價）→ 合併 1 筆，item_name == `日本語`；另加 `カテゴリ / さくらみこ（英語）`、`カテゴリ / 白上フブキ（英語）`（同價）→ 另合併 1 筆 item_name == `英語`；驗證同價兩組 item_name 相異（不撞號）。
- **新增** 含空白 + 括號（SP 型）：variant `カテゴリ / さくらみこ SPボイス（日本語）`、`カテゴリ / 白上フブキ SPボイス（日本語）` → 合併，item_name == `SPボイス 日本語`。
- **維持綠** `test_pure_talent_enumeration_merges_to_product_title`（裸 talent、key 空 → item_name == product 標題）。
- **維持綠** `test_themed_series_not_merged_even_at_same_price`、`test_short_option_value_prepends_product_title`、`test_empty_residual_without_talent_not_merged`、`test_pure_talent_enumeration_coexists_with_distinct_item`、`test_excludes_set_and_zero_price`、`test_unparseable_price_counts_as_excluded_zero`、`test_detail_snippet_substring_match`、`test_voice_item_has_no_snippet`。

### 8.2 `tests/test_estimator.py`

- **更新** `_hit`（[test_estimator.py:31-34](../../../tests/test_estimator.py)）：新增可選 `product_title` 參數並寫入 metadata（第一個位置參數 `id` 既有作為 `item_name`，見 test_estimator.py:32-33）：

  ```python
  def _hit(id, item_type, price, pub, dist, product_title="P"):
      return QueryHit(id=id, document="", distance=dist, metadata={
          "item_name": id, "item_type": item_type, "price_jpy": price,
          "published_at": pub, "store_id": "s", "detail_snippet": "",
          "product_title": product_title})
  ```

- 去重以 **item_name（即傳入的 `id`）是否含 product_title 子字串**決定欄位是否插入，與 `product_title` 參數值本身無關。
- **更新** 參考行斷言（[test_estimator.py:116](../../../tests/test_estimator.py)）：插入欄位案例——既有 `_hit("itemX", "ぬいぐるみ", 500, …)` 的 `item_name="itemX"` 不含 `"P"` → 斷言改為 `"- itemX | ぬいぐるみ | P | ¥500 | ? | s"`。
- **新增** 去重斷言兩例（item_name 含 product_title → 省略欄位）：
  - exact：`_hit("P", "ぬいぐるみ", 500, 0, 0.1, product_title="P")` → 行 `"- P | ぬいぐるみ | ¥500 | ? | s"`，且 `last_user_prompt.count("| P |") == 0`。
  - 前綴：`_hit("P 黒 M", "ぬいぐるみ", 500, 0, 0.1, product_title="P")` → 行 `"- P 黒 M | ぬいぐるみ | ¥500 | ? | s"`，且 `last_user_prompt.count("| P |") == 0`。
- 其餘 estimator 測試（rerank、對帳、多類型查詢等）：既有 id 皆不含大寫 `P` 子字串，故插入 product 欄位不影響其排序/存在性斷言，維持綠。

## 9. 驗證工具鏈（[CLAUDE.md](../../../CLAUDE.md)）

- 型別：`.venv/bin/basedpyright estimator_king`（production code 0 error）。
- Lint：`uvx ruff check estimator_king tests`。
- 測試：`.venv/bin/python -m pytest tests/test_items.py tests/test_estimator.py -v -o addopts=""`；必要時全套 `.venv/bin/python -m pytest`。

## 10. 驗收標準

1. talent 名黏著括號語言標記的列舉商品，token 化後依共同部分（含語言）正確合併，同 product 同價的不同語言組得到相異 item_name（不撞號）。
2. 合併 item 以共同部分命名；共同部分為空時 fallback product 標題。
3. `whole_product_single` 移除後，整 product 合併且共同部分非空者以共同部分命名（如 `Blue Journey衣装ver.`）。
4. `/estimate` 參考行在 product_title **不是** item_name 子字串時帶入 product_title 欄位；product_title 是 item_name 子字串（含 key 空 fallback 的相等、短選項值前綴）時不重複顯示。
5. 既有不受影響行為（themed series、短選項、純-talent、SET/¥0、snippet）維持不變。
6. 真實 `秘密の雨の日ボイス` 重跑得 §7.1 預期 5 合併 item。
7. 驗證工具鏈全綠（型別 0 error、ruff、相關測試）。
