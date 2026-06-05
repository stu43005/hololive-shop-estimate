# Talent 比對 token 化（黏著修飾詞、內部空白）、合併命名與參考行 product 名 — 設計規格

日期：2026-06-05

## 1. 目標

讓 talent 列舉型商品在兩種 talent 名寫法下都能正確合併：(a) talent 名後**緊黏無空白修飾詞**（全形括號語言標記，如 `（日本語）`）、(b) talent 名**含內部空白**（`姓 名` 形式，如 `小雀 とと`，而字典存無空白形 `小雀とと`）。合併後 item 以**共同部分**命名（空則 product 標題）；並讓 `/estimate` 送進 chat model 的參考行帶上 **product 名**，使每筆參考都有 product 脈絡。

外部介面（`/estimate`、`crawl`/`run`）不變；改變的是 `decompose_items` 的 token 化與命名，以及 estimator 參考行格式。

## 2. 現況與根因（已實證）

`_canonical_key`（[items.py:60-69](../../../estimator_king/sync/items.py)）只以**空白**切 token（`normalize_text(residual).split()`）並**逐單一 token** 比對字典。這對兩種真實 talent 寫法失效：

**(a) talent 名緊黏括號修飾詞** — `ホロライブ 秘密の雨の日ボイス ～紫陽花の見える温泉宿で～`（product 9384273215708）的 variant `商品名` 為 `<talent>（日本語）`：

- `ときのそら（日本語）` → `.split()` 得單一 token `ときのそら（日本語）`，≠ 字典裸 `ときのそら` → 不移除 → 唯一 key → **不合併**（45 筆一般 voice 各自獨立）。
- SP voice `ときのそら SPボイス（日本語）` 因 talent 與 `SPボイス` 間有空白，`ときのそら` 被切出移除、剩 `SPボイス（日本語）` → 意外合併（×12「SPボイス（日本語）」、×3「SPボイス（英語）」）。

**(b) talent 名含內部空白** — `ぶいすぽっ！ジャージ`（product vspo:7866043990203）單選項 `バリエーション` = talent 名、全 7500 円，**部分寫成 `姓 名`**（含空白），而字典存無空白形：

- `小雀 とと` → `.split()` 得 `小雀`、`とと` 兩 token，皆 ≠ 字典 `小雀とと` → 不移除 → 唯一 key → **不合併**。
- 無內部空白者（`花芽すみれ`）單 token 命中字典 → 合併（×10），與含空白者（15 筆）混雜；且每筆未合併品項各別經 LLM 分類得到不一致 type（ジャケット／その他／スウェット…）。

實證（1,385 真實 product）：修正後共 759 合併群組，分佈見 §7.2。根因為「**逐單一 token 比對**無法處理黏著修飾詞（需先拆 token）與內部空白（需跨 token、空白不敏感比對）」，與先前移除 `key.strip()` 無關。

## 3. 變更

### 3.1 Token 化 + greedy n-gram 空白不敏感 talent 比對（`_canonical_key`，[items.py](../../../estimator_king/sync/items.py)）

兩項合一：(1) token 切法改為「空白 **+ 全/半形括號**」；(2) talent 比對改為「**貪婪最長 n-gram、空白不敏感**」——把連續 token 串接（無空白）後比對**去空白字典**，命中即整段移除。

```python
_TOKEN_SPLIT = re.compile(r"[\s（）()]+")
_MAX_TALENT_TOKENS = 4   # n-gram 上限（姓 名 spaced 名為 2 token；上限涵蓋罕見更長者）


def _talents_nospace(talents: frozenset[str]) -> dict[str, str]:
    """去空白正規化形 → 原字典形，供空白不敏感比對。"""
    return {normalize_text(t).replace(" ", ""): t for t in talents}


def _canonical_key(residual: str, talents_nospace: dict[str, str]) -> tuple[str, list[str]]:
    """Drop talent tokens (greedy longest n-gram, whitespace-insensitive);
    return (canonical_key, removed_talent_originals)."""
    toks = [t for t in _TOKEN_SPLIT.split(normalize_text(residual)) if t]
    kept: list[str] = []
    removed: list[str] = []
    i = 0
    while i < len(toks):
        matched = False
        for j in range(min(len(toks), i + _MAX_TALENT_TOKENS), i, -1):  # 最長優先
            cand = "".join(toks[i:j])                  # 串接（無空白）
            if cand in talents_nospace:
                removed.append(talents_nospace[cand])  # 記原字典形
                i = j
                matched = True
                break
        if not matched:
            kept.append(toks[i])
            i += 1
    return " ".join(kept), removed
```

- **呼叫端調整**：唯一呼叫點在 `decompose_items`（[items.py:140](../../../estimator_king/sync/items.py)）。於分組迴圈**外**先 `talents_nospace = _talents_nospace(talents)`（每次 decompose 算一次），改呼叫 `_canonical_key(residual, talents_nospace)`。簽名由 `(residual, talents)` 改為 `(residual, talents_nospace)`。
- **分隔符** = 空白 + `（）()`；括號內容（`日本語`）成為保留 token、併入 key。
- **空白不敏感 + 貪婪最長 n-gram**：`小雀 とと` → `[小雀, とと]` → `小雀`+`とと`=`小雀とと` 命中 → 移除。單 token（`花芽すみれ`）、middot 名（`アユンダ・リス`，`・` 非分隔符 → 單 token）皆仍命中。`normalize_text` 已把全形空白 U+3000 收斂為半形，再 `.replace(" ","")` 去除 → 串接比對對全/半形空白皆不敏感。
- **`removed` 記原字典形**（非 variant 寫法）：`_Item.talents` 因此收齊一致原形（`小雀とと`），與既有 `talents` 欄位語義一致。
- 與兩根因例組合正確：`小雀 とと（日本語）` → `[小雀,とと,日本語]` → 移除 `小雀とと`、留 `日本語` → key `日本語`；`ときのそら SPボイス（日本語）` → 移除 `ときのそら`、留 `SPボイス 日本語`。
- **僅變更 `_canonical_key`（與其簽名/呼叫端）**；`_meaningful_tokens`（[items.py:56-57](../../../estimator_king/sync/items.py)）與 `_extract_snippet` 內以原 `talents` 做的 best-effort core 去 talent（[items.py:82](../../../estimator_king/sync/items.py)）**不動**（best-effort、不影響合併）。
- 範圍限定括號（YAGNI）：不處理 `「」`『』【】。

結果：`秘密の雨の日ボイス` 一般 voice 依語言分組（`日本語`×27、`英語`×12、`インドネシア語`×6）、SP（`SPボイス 日本語`×12、`SPボイス 英語`×3）；`ぶいすぽっ！ジャージ` 25 個變體（含 `小雀 とと` 等含空白者）全部 → key 空 → **合併成 1 筆**（fallback product 標題 `ぶいすぽっ！ジャージ`）。

### 3.2 合併命名（`item_name`，[items.py](../../../estimator_king/sync/items.py)）

合併 item（去重分群後 `residual=None`）的名稱改為**共同部分（該組 canonical key）**，key 為空（整串皆 talent）時 fallback product 標題：

- `_Item`（[items.py:129-134](../../../estimator_king/sync/items.py)）新增欄位 `key: str`：合併分支（[items.py:150-153](../../../estimator_king/sync/items.py)）填入該組 canonical key；非合併分支（[items.py:154-157](../../../estimator_king/sync/items.py)）填 `""`（未使用）。
- 命名分支（[items.py:165-171](../../../estimator_king/sync/items.py)）**簡化為兩分支**：

  ```python
  for ri in raw_items:
      if ri.residual is None:
          name = ri.key.strip() or snapshot.title   # 合併：共同部分；空才用 product 標題
      else:
          name = normalize_text(ri.residual)          # 非合併：normalize 後的殘餘（收斂全形空白/entity，與合併 key 一致；如「黒　M」→「黒 M」；空殘餘為 ""）
      items.append(ProductItem(... item_name=name ...))
  ```

- **移除 `whole_product_single`**（[items.py:161-163](../../../estimator_king/sync/items.py)）：其唯一效果是「整 product 合併時強制 product 標題命名」，現由上式「key 空 → product 標題」涵蓋；key 非空時改用共同部分（如 `Blue Journey衣装ver.`）。此為刻意的命名語義變更。
- **移除 `_is_option_value`（[items.py:72-74](../../../estimator_king/sync/items.py)）與 `_SIZE_RE`（[items.py:16-18](../../../estimator_king/sync/items.py)）**（兩者僅用於原短選項值前綴命名，[items.py:168](../../../estimator_king/sync/items.py) 是 `_is_option_value` 的唯一呼叫點）：原把短選項值殘餘（如「黒 M」）前置 product 標題命名為 `{product} {殘餘}`。product 脈絡現由 §3.3 參考行欄位、embedding 文件第 3 行 `# {product_title}`、log tree 父層提供，前置已多餘、且會在 embedding 文件中重複 product。移除後短選項值直接以 **`normalize_text` 後的殘餘**命名（與合併分支的 canonical key 一致皆 normalize；variant 中的全形空白收斂，如 `黒　M` → `黒 M`）。注意：此使既有非合併「一般殘餘」分支也一併套用 `normalize_text`（先前為原樣）；既有測試殘餘本已乾淨故 normalize 為 no-op、不受影響。**連帶**：空殘餘（`""`）非合併項的 item_name 由 product 標題改為 `""`（罕見退化變體；product 仍由參考行/metadata 提供）。
- **不採用** `{product} / {key}` 形式：product 脈絡改由 §3.3 的參考行欄位提供，使 item_name 維持為純共同部分／純殘餘、避免 embedding 文件中 product 標題重複（[engine.py:84-88](../../../estimator_king/sync/engine.py) 的文件第 3 行已含 `# {product_title}`）。移除格式 #3 後，此「item_name 不扛 product」原則於所有分支一致（唯一例外為 key 空 fallback，因無共同部分可用）。

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
    if product_title and product_title != item_name:
        fields.append(product_title)   # 去重：item_name 已 fallback 為 product 標題（key 空）時不重複插入
    fields += [f"¥{m.get('price_jpy')}", date, str(m.get("store_id") or "")]
    line = "- " + " | ".join(fields)
    snippet = str(m.get("detail_snippet", "") or "")
    ...  # 既有 snippet 接行邏輯不變
```

- 欄位：`item_name | item_type | [product_title] | ¥price | date | store_id`；**product_title 僅在非空且 ≠ item_name 時插入**（置於 item_type 之後）。格式 #3 移除（§3.2）後，item_name 唯一會等於 product 標題的情況是 key 空 fallback；以相等判斷即精確去除該重複，無子字串過度省略之虞。
- 去重後每筆參考仍帶 product 脈絡：item_name ≠ product 標題者（合併共同部分、非合併殘餘）由 product_title 欄位提供；item_name == product 標題者（key 空 fallback）脈絡已在 item_name 內。
- **system prompt 不改**：新增欄位為加性、self-descriptive；現有 prompt（[estimator.py](../../../estimator_king/bot/estimator.py) `SYSTEM_PROMPT`）關於以 item_name/detail 對齊的指示仍適用。

## 4. 不變式與 slug 唯一性

- **item_name 必須帶共同部分以避免 slug 撞號**：item_id 為 `{store_id}:{product_id}:{slug(item_name, price)}`（[engine.py:77-80](../../../estimator_king/sync/engine.py)、[engine.py:240](../../../estimator_king/sync/engine.py)）。token 化修正後，同一 product 同價會有多個合併組（如 voice 的 `日本語`/`英語`/`インドネシア語` 同為 1000 円）。若全 fallback 成 product 標題則 slug 相同 → 互相覆寫。§3.2 以共同部分命名確保同 product 同價的不同合併組得到**相異 item_name → 相異 slug**，不撞號。
- **key 空時的 fallback 安全**：同 product 內 key 空的合併組至多一個（key 空且同價即同組、已合併為一），fallback product 標題不致與其他 key 空組同價撞號。
- **既有邊界（非本次引入、不處理）**：移除 `_is_option_value` 後（§3.2），空殘餘無 talent 的非合併項 item_name 為 `""`（不再是 product 標題），與 key 空 fallback（product 標題）不同 → 兩者不再交叉撞號。剩餘理論邊界為「同 product 同價有 ≥2 個空殘餘非合併項」→ 皆 `""` 而撞號；此與既有行為等價（先前皆命名 product 標題亦撞號）、需空殘餘 variant 方觸發，且真實資料抽樣 **0 例**，本案不處理。
- **同價前提仍成立**：合併仍先按 price 分組（[items.py:125-127](../../../estimator_king/sync/items.py)），命名變更不影響分組。

## 5. 範圍

- **變更檔案**：`estimator_king/sync/items.py`（token 化 + n-gram 空白不敏感比對 + `_talents_nospace` helper + `_canonical_key` 簽名與其唯一呼叫端 + 命名兩分支 + 移除 `_is_option_value`/`_SIZE_RE`/`whole_product_single`）、`estimator_king/bot/estimator.py`（參考行加 product 欄位）。
- **不改** `stores_config.yaml`（無補 talents、無改 `item_types_version`）。後果同前次：本變更為程式邏輯 + 文件格式變更、不影響 `content_hash`，**現有已索引 product 不會立即重新拆解**；僅新 product 或下次 `content_hash` 變動 / 自然重抓者套用新邏輯。屬可接受的漸進生效（如需立即生效可另案 bump `item_types_version`）。
- **無 `engine.py` 邏輯變更**（其 `_format_item_document`/metadata 既有結構不變；item_name 內容改變是 items.py 的產出差異）。item_name 全使用點（slug、文件、classify 輸入、metadata、log、參考行）對改變後的值（含空字串）皆安全降級，無破壞。
- **`_extract_snippet` 與 item_name 的相依**：`_extract_snippet`（[items.py:77](../../../estimator_king/sync/items.py)）以算好的 item_name 為比對 core（呼叫於 ProductItem 建構處）。合併命名改為共同部分後，snippet 比對 core 隨之改變（例如改以 `アクリルスタンド` 而非 product 標題比對條列規格行）。snippet 為 best-effort、缺失安全降級、**不影響價格/類型/檢索**；其命中率變化以 §7.3 operational verification 於實作後用真實 `html_details` 驗證。
- **無新增第三方相依**。

## 6. 不受影響的既有行為（迴歸保護）

- **themed series 不誤併**（同主題不同品項，無 talent token、無括號）：key 非空且互異 → 不合併、各自以殘餘命名。
- **SET / ¥0 排除**：不受 token 化變更影響。
- **detail_snippet 的 token 化（`_meaningful_tokens`）不變**；惟其比對 core（item_name）對合併品項隨命名改變（§5、§7.3），屬 best-effort、安全降級。
- **純-talent 列舉**（裸 talent 名、key 空，如 `隣人ボイス2026`）：key 空 → fallback product 標題（命名行為不變）。
- （**短選項值命名已改變**，不再前綴 product 標題 → 列於 §3.2/§8.1，非迴歸保護項。）

## 7. 真實資料驗收（operational verification）

### 7.1 目標商品

- **(a) 黏著修飾詞** `秘密の雨の日ボイス`（product 9384273215708）重跑 `decompose_items`，預期 5 個合併 item，item_name 分別為共同部分：`日本語`(×27)、`英語`(×12)、`インドネシア語`(×6)、`SPボイス 日本語`(×12)、`SPボイス 英語`(×3)，5 者 item_name 相異 → slug 不撞號。
- **(b) 內部空白** `ぶいすぽっ！ジャージ`（product vspo:7866043990203）重跑，預期 25 個變體（含 `小雀 とと`、`一ノ瀬 うるは` 等含空白 talent）全部合併成 **1 筆**，key 空 → item_name fallback product 標題 `ぶいすぽっ！ジャージ`、`talents` 收齊 25 原形、price 7500。

### 7.2 整體分佈（1,385 product 樣本，759 合併 item）

- 共同部分命名（key 非空）：**552**（如 `アクリルスタンド`、`缶バッジセット`、`コレクションカード`、`日本語`、`Blue Journey衣装ver.`…）。
- fallback product 標題（key 空）：**207**。
- 空 item_name（空殘餘非合併項）：**0**（理論邊界，真實資料不發生；§8.1 仍保留 guard 測試）。
- **n-gram 不過度合併**：全樣本合併群組數，單 token 比對 vs n-gram 空白不敏感比對皆為 **759（相同）**——n-gram 不製造新（虛假）合併群組，只把本該同組的含空白 talent 變體正確吸收進既有群組（如 jersey 由「10 合併 + 15 singleton」變「25 合 1」）。

### 7.3 snippet 命中率（實作後 operational verification）

`_extract_snippet` 以 item_name 為比對 core（§5），合併命名改變會影響其命中。實作後對**真實 crawl 的 `html_details`**（非 products.json 的 body_html）比對合併品項 snippet 命中率，確認未顯著劣化（best-effort、缺失安全降級，非阻斷項；若顯著劣化再評估是否將 snippet core 與顯示名解耦）。

## 8. 測試

### 8.1 `tests/test_items.py`（沿用 `_snap`/`TALENTS`）

- **更新** `test_talent_variants_merge_to_product_title`：Blue Journey（殘餘 `さくらみこ Blue Journey衣装ver.`）合併後 item_name 改為共同部分 **`Blue Journey衣装ver.`**（非 product 標題）；改名為 `test_talent_variants_merge_named_by_common_part`，斷言 `item_name == "Blue Journey衣装ver."`、`source_variant_ids` 長度、talents 收齊。
- **新增** 黏著括號修飾詞合併：product 標題如 `秘密ボイス`，variant `カテゴリ / さくらみこ（日本語）`、`カテゴリ / 白上フブキ（日本語）`（同價）→ 合併 1 筆，item_name == `日本語`；另加 `カテゴリ / さくらみこ（英語）`、`カテゴリ / 白上フブキ（英語）`（同價）→ 另合併 1 筆 item_name == `英語`；驗證同價兩組 item_name 相異（不撞號）。
- **新增** 含空白 + 括號（SP 型）：variant `カテゴリ / さくらみこ SPボイス（日本語）`、`カテゴリ / 白上フブキ SPボイス（日本語）` → 合併，item_name == `SPボイス 日本語`。
- **新增** 內部空白 talent（n-gram 跨 token 比對）：product 標題如 `ぶいすぽっ！ジャージ`，variant `バリエーション / さくら みこ`、`バリエーション / 白上 フブキ`（內部含空白）、`バリエーション / 博衣こより`（無空白形）同價 → 三者皆命中字典被移除 → key 空 → 合併 1 筆，`item_name == "ぶいすぽっ！ジャージ"`（fallback product 標題）、`set(talents) == {"さくらみこ","白上フブキ","博衣こより"}`（**原字典形**）、`source_variant_ids` 長度 3。
- **更新** `test_short_option_value_prepends_product_title` → 短選項值不再前綴 product 標題；改名 `test_short_option_value_named_by_residual`，斷言 item_name == `黒 M` / `白 L`（`normalize_text` 後值——輸入 `黒　M`/`白　L` 的全形空白被收斂為半形）。
- **更新** `test_empty_residual_without_talent_not_merged` → 空殘餘非合併項 item_name 改為 `""`（不再 product 標題）；以 item 數 / `source_variant_ids` 長度驗證未合併、且 `item_name == ""`；移除原註解對 `_is_option_value("")` 的引用（該函式已刪）。
- **維持綠** `test_pure_talent_enumeration_merges_to_product_title`（裸 talent、key 空 → item_name == product 標題）。
- **維持綠** `test_themed_series_not_merged_even_at_same_price`、`test_pure_talent_enumeration_coexists_with_distinct_item`、`test_excludes_set_and_zero_price`、`test_unparseable_price_counts_as_excluded_zero`、`test_detail_snippet_substring_match`、`test_voice_item_has_no_snippet`。

### 8.2 `tests/test_estimator.py`

- **更新** `_hit`（[test_estimator.py:31-34](../../../tests/test_estimator.py)）：新增可選 `product_title` 參數並寫入 metadata（第一個位置參數 `id` 既有作為 `item_name`，見 test_estimator.py:32-33）：

  ```python
  def _hit(id, item_type, price, pub, dist, product_title="P"):
      return QueryHit(id=id, document="", distance=dist, metadata={
          "item_name": id, "item_type": item_type, "price_jpy": price,
          "published_at": pub, "store_id": "s", "detail_snippet": "",
          "product_title": product_title})
  ```

- 去重以 **item_name（即傳入的 `id`）是否等於 product_title** 決定欄位是否插入（格式 #3 移除後，唯一相等情況為 key 空 fallback）。
- **更新** 參考行斷言（[test_estimator.py:116](../../../tests/test_estimator.py)）：插入欄位案例——既有 `_hit("itemX", "ぬいぐるみ", 500, …)` 的 `item_name="itemX" ≠ "P"` → 斷言改為 `"- itemX | ぬいぐるみ | P | ¥500 | ? | s"`。
- **新增** 去重斷言（item_name == product_title → 省略欄位）：`_hit("P", "ぬいぐるみ", 500, 0, 0.1, product_title="P")` → 行 `"- P | ぬいぐるみ | ¥500 | ? | s"`，且 `last_user_prompt.count("| P |") == 0`。
- 其餘 estimator 測試（rerank、對帳、多類型查詢等）：既有 id 皆不等於 `"P"`，故插入 product 欄位不影響其排序/存在性斷言，維持綠。

## 9. 驗證工具鏈（[CLAUDE.md](../../../CLAUDE.md)）

- 型別：`.venv/bin/basedpyright estimator_king`（production code 0 error）。
- Lint：`uvx ruff check estimator_king tests`。
- 測試：`.venv/bin/python -m pytest tests/test_items.py tests/test_estimator.py -v -o addopts=""`；必要時全套 `.venv/bin/python -m pytest`。

## 10. 驗收標準

1. talent 列舉商品在兩種寫法下正確合併：(a) 黏著括號語言標記（`<talent>（日本語）`）→ 拆 token 後依共同部分（含語言）合併、同 product 同價不同語言組 item_name 相異不撞號；(b) talent 名含內部空白（`小雀 とと`）→ greedy n-gram 空白不敏感比對命中無空白字典、與無空白形一同合併（jersey 25 → 1）。n-gram **不製造虛假合併**（全樣本合併群組數與單 token 比對相同，§7.2）。
2. 合併 item 以共同部分命名；共同部分為空時 fallback product 標題。
3. `whole_product_single` 移除後，整 product 合併且共同部分非空者以共同部分命名（如 `Blue Journey衣装ver.`）。
4. `/estimate` 參考行在 product_title **≠** item_name 時帶入 product_title 欄位；product_title == item_name（key 空 fallback）時不重複顯示。
5. 移除 `_is_option_value`/`_SIZE_RE`：非合併項以 `normalize_text` 後殘餘命名（短選項 `黒　M` → `黒 M`）；空殘餘非合併項 item_name 為 `""`。
6. 既有不受影響行為（themed series 不誤併、純-talent 命名、SET/¥0 排除）維持不變。
7. 真實 `秘密の雨の日ボイス` 重跑得 §7.1 預期 5 合併 item。
8. snippet：合併品項 snippet 命中率經 §7.3 operational verification 確認未顯著劣化（best-effort、非阻斷）。
9. 驗證工具鏈全綠（型別 0 error、ruff、相關測試）。
