# clean_crawl_queue

清空 `crawl_queue` 待辦佇列的維護腳本。

## 用途

`crawl_queue` 是「待抓取」工作佇列，不是權威狀態。清空它**不會遺失資料**：product
狀態列保存在 `products` 表，下次正常 crawl 會重新從 sitemap 填回佇列，並讓
`product_url` 等欄位自然 self-heal。

典型使用時機：

- 佇列被異常灌爆（例如 sitemap 語系過濾 bug，把大量非預設語系 URL 塞進佇列）。
- 修正 sitemap 過濾後，要清掉殘留的舊語系 URL，避免它們在下次 drain 被抓取。

## 前置條件

**先停止 bot**。資料庫為單一寫入者（WAL，但並發寫入會 `database is locked`），
腳本執行期間不能有其他程序在寫 DB。

## 用法

```bash
.venv/bin/python -m scripts.clean_crawl_queue [--db PATH] [--store STORE_ID] [--dry-run]
# 或
.venv/bin/python scripts/clean_crawl_queue.py [--db PATH] [--store STORE_ID] [--dry-run]
```

### 參數

| 參數 | 說明 |
| --- | --- |
| `--db PATH` | SQLite 路徑。省略時依序取 `$DATABASE_PATH`、再 fallback `./estimator_king.db`。 |
| `--store STORE_ID` | 只清指定 store 的佇列列。省略則清空所有 store。 |
| `--dry-run` | 只回報將被刪除的列數，不實際刪除。 |

## 範例

預覽（不刪除）：

```bash
.venv/bin/python -m scripts.clean_crawl_queue --dry-run
# crawl_queue rows (all stores): 1234 (dry-run, nothing deleted)
```

全部清空：

```bash
.venv/bin/python -m scripts.clean_crawl_queue
# crawl_queue rows (all stores) before: 1234
# crawl_queue rows deleted: 1234
```

只清單一 store：

```bash
.venv/bin/python -m scripts.clean_crawl_queue --store vspo
# crawl_queue rows (store=vspo) before: 1200
# crawl_queue rows deleted: 1200
```

## 與 sitemap 語系過濾的關係

此腳本只負責清佇列，**不會**改變 sitemap 過濾行為。正確順序是：先部署單一語系過濾
修正（`stores_config.yaml` 的 `locale` 設定 + enumerator 過濾），再執行本腳本清掉
舊佇列；否則下次 crawl 會用舊邏輯把佇列再次灌爆。
