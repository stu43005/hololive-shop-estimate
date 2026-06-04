# 純-talent 列舉品項合併（放寬空 canonical key 防呆）— 設計規格

日期：2026-06-04

## 1. 目標

讓「一個 product 底下、多個 variant 的殘餘標題整串只是 talent 名」這類純-talent 列舉商品（典型如語音包「ぶいすぽっ！隣人ボイス2026」，17 個 variant 各為一位 talent 的 voice）能合併成**單一 item 向量**，命名用 product 標題，而非為每位 talent 各產生一筆 talent 名主導的向量。

外部介面（`/estimate`、`crawl`/`run`）不變；改變的只是 `decompose_items` 的去重判定。

## 2. 現況與根因

`decompose_items`（[items.py](../../../estimator_king/sync/items.py)）的 talent-gated 去重在同價分組內、以 canonical key（殘餘標題移除所有 talent token 後的剩餘 token）分群，合併條件為 [items.py:144](../../../estimator_king/sync/items.py)：

```python
if len(group) >= 2 and key.strip() and removed_any:
```

當 variant 殘餘**整串就是一個 talent 名**（如「花芽すみれ」這種無空白單 token）時，移除 talent token 後 `kept == []`，canonical key 為空字串。`key.strip()` 為 falsy → 合併被擋 → 每個 variant 各自成一筆 item，命名為該 talent 名。

實證 log（`ぶいすぽっ！隣人ボイス2026`，product_id `vspo:8396995363003`）：17 個 voice variant 各自獨立成 17 筆 item，名稱各為一位 talent。這違反本專案「索引單位對齊品項、避免 talent 名主導向量」的設計目標。

`key.strip()` 防呆原意是「避免整串都被當成 talent 而誤併」，但對「product 標題即品項身分、variant 僅以 talent 區分」的列舉型商品造成盲點——而這正是最該合併的情況。

## 3. 變更

### 3.1 放寬合併條件（[items.py:144](../../../estimator_king/sync/items.py)）

移除 `key.strip()` 這一項，條件改為：

```python
if len(group) >= 2 and removed_any:
```

- `len(group) >= 2`：仍要求群組至少 2 筆。
- `removed_any`：**保留**。`removed_any = any(r for _, _, r in group)`（[items.py:143](../../../estimator_king/sync/items.py)），即群組中至少一筆移除過 talent token。此為核心防呆：完全沒有 talent 的群組（含全空白殘餘群組）不會被合併。

命中時走既有的 `_Item(residual=None, ...)` 分支（[items.py:150-153](../../../estimator_king/sync/items.py)），收集全組 `source_variant_ids` 與去重後的 `talents`，`price_jpy` 為該組共同價格。

### 3.2 命名（無需修改）

命名分支為 [items.py:166](../../../estimator_king/sync/items.py) 的 `if ri.residual is None or whole_product_single:`。空-key 合併產生的 item 即 `residual=None`，**僅憑 `residual is None`（該條件第一項）即落入 `snapshot.title`（product 標題）命名分支，與 `whole_product_single` 無關**，故 **不需新增或修改命名邏輯**。`whole_product_single`（[items.py:161-163](../../../estimator_king/sync/items.py)）只影響「單一 `residual` 非空、但需以 product 標題覆寫」的情況，本變更不觸及該路徑，因此 `whole_product_single` 判定亦無需改動。

## 4. 安全性論證（不變式）

放寬後仍安全，依據對 [items.py](../../../estimator_king/sync/items.py) 的三項結構保證：

1. **同價是合併前提**：分組第一層按 price（[items.py:125-127](../../../estimator_king/sync/items.py)），canonical key 分群只在同一 price 的成員內進行（[items.py:137-141](../../../estimator_king/sync/items.py)）。不同價格不可能進同一群組；合併 item 的 `price_jpy` 為該群組共同價格，對組內每筆皆正確。即使發生非預期合併，價格不失真。

2. **空-key 群組只收得到「全 talent 或全空白」殘餘**：`_canonical_key`（[items.py:60-69](../../../estimator_king/sync/items.py)）的 key 為空 ⟺ `kept == []` ⟺ 殘餘的每個 token 皆為 talent，或殘餘無 token（空字串）。因此**任何含非 talent token 的殘餘（如「アクリルスタンド」「ポーチ」）必得非空 key、被分到自己的群組，結構上不可能進入空-key 群組被誤併**。注意 `_canonical_key` 對殘餘以原始 `.split()` 切 token（**非** `_meaningful_tokens` 的長度 ≥2 過濾，[items.py:64](../../../estimator_king/sync/items.py)），故連單字元的非 talent token 也會進入 `kept` 使 key 非空——空-key 群組更不可能混入任何非 talent 內容。

3. **`removed_any` 守住純空白群組**：唯一能混入空-key 群組的「非 talent」成員為「殘餘整串空白」的退化 variant；其要被合併，群組仍須 `removed_any == True`（即至少一筆為純 talent）。全空白且無任何 talent 的群組 `removed_any == False`，仍走 else 分支不合併。

綜上，放寬後唯一可能的非預期合併為「空白標題 variant 與純-talent variant 同價並存」，其危害趨近於零：空白殘餘 variant 本無可檢索識別、同價不影響估價、語義無損失。此為對所有資料成立的邏輯界限，不依賴資料分布。

## 5. 範圍排除（明確不做）

- **不修改 `talents` 字典**：`stores_config.yaml` 的 `talents` 不新增任何名稱。後果：實證案例中不在字典的 talent（如 `銀城サイネ`、`龍巻ちせ`）仍各自獨立成 item；該 product 會收斂為「1 筆合併 voice item（字典內 talent）＋ 數筆字典外 talent 的獨立 item」，而非單一 item。此為可接受結果，補字典屬另案。
- **不 bump `item_types_version`**：`stores_config.yaml` 的 `item_types_version` 維持現值。後果：本變更為純程式邏輯調整、不影響 `content_hash`，現有已索引 product 不會立即重新拆解；僅新 product、或下次 `content_hash` 變動 / 自然重抓的 product 套用新邏輯。屬可接受的漸進生效。
- **無簽名變更、無 `engine.py` 變更**：變更僅限 `decompose_items` 內一個布林條件。

## 6. 不受影響的既有行為（迴歸保護）

- **「品項描述 + talent」型合併**（如 Blue Journey 衣裝 ver. ×N）：殘餘含非 talent 描述 token → key 非空 → 走原合併路徑，照常合併、命名沿用既有規則。
- **themed series 不誤併**（如同主題下「ポーチ」vs「プレート」，無 talent token）：key 非空且互異 → 各自獨立，照常不合併。
- **SET / ¥0 排除**、**短選項值併入 product 標題**、**detail_snippet 擷取** 等其餘 `decompose_items` 行為不受此條件變更影響。

## 7. 測試（`tests/test_items.py`，沿用既有 `_snap` / `TALENTS` 慣例）

新增：

1. **純-talent 列舉合併**：product 標題如「隣人ボイス」，多個 variant 殘餘為裸 talent 名（皆在 `TALENTS`，同價），驗證合併為 1 筆 item、`item_name == product 標題`、`talents` 收齊全部、`source_variant_ids` 長度等於 variant 數、`price_jpy` 為共同價。
2. **空-key 但無 talent 不合併**：建構 ≥2 個「殘餘為空字串且無 talent」的同價 variant——variant 標題形如 `"グッズ / "`（`" / "` 後為空，`_strip_prefix` 後 `residual == ""`，故 `_canonical_key` 回傳 `("", [])`、落入空-key 群組但 `removed_any == False`）。驗證它們**不**被合併：斷言以 **item 數**為準——`len(result.items) == 2`（或 variant 數）、每筆 `len(source_variant_ids) == 1`。**不可用 `item_name` 區分**：空殘餘走 else 分支後，命名經 `_is_option_value("")`（`len("") < 4` → True）→ `f"{snapshot.title} ".strip()` → 兩筆 `item_name` 皆等於 product 標題，故只能以 item 數 / `source_variant_ids` 長度驗證未合併。
3. **混合：純-talent 列舉 + 一筆獨立非 talent 品項同存**：驗證純-talent 群組合併為 1 筆（product 標題命名）、非 talent 品項（如「アクリルスタンド」，非空 key）獨立成另一筆、兩者互不干擾。

既有測試須維持綠（驗證非空 key 路徑不受影響）：

- `test_talent_variants_merge_to_product_title`（Blue Journey ×N 仍合併）。
- `test_themed_series_not_merged_even_at_same_price`（themed series 仍不合併）。
- `test_excludes_set_and_zero_price`、`test_short_option_value_prepends_product_title`、`test_detail_snippet_substring_match`、`test_voice_item_has_no_snippet` 等其餘案例不受影響。

## 8. 驗證工具鏈（[CLAUDE.md](../../../CLAUDE.md)）

- 型別：`.venv/bin/basedpyright estimator_king`（production code 0 error）。
- Lint：`uvx ruff check estimator_king tests`。
- 測試：`.venv/bin/python -m pytest tests/test_items.py -v -o addopts=""`（單檔），必要時跑全套 `.venv/bin/python -m pytest`。

## 9. 驗收標準

1. 純-talent 列舉商品（多 variant 殘餘皆為字典內 talent 名、同價）合併為單一 item，命名為 product 標題，`talents` 收齊、`price_jpy` 正確。
2. 含非 talent 實體 token 的品項（如「アクリルスタンド」）永不被併入空-key 群組（結構保證，由測試案例 3 佐證）。
3. 全空白且無 talent 的 variant 群組不被合併（`removed_any` 守住）——以 item 數 / `source_variant_ids` 長度驗證，非以 `item_name` 區分（空殘餘兩筆名稱皆等於 product 標題）。
4. 既有合併 / 不合併行為（Blue Journey 合併、themed series 不合併）不變。
5. 驗證工具鏈全綠（型別 0 error、ruff、相關測試）。
