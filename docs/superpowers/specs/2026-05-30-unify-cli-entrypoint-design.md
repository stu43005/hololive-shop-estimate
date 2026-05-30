# 統一 CLI 入口點（子命令制）— 設計規格

日期：2026-05-30

## 1. 目標

把目前兩個獨立的 `__main__` 入口點合併為**單一頂層入口點**
`python -m estimator_king`，以子命令區分用途：

- `python -m estimator_king run` — 啟動 Discord bot + 程序內排程器（常駐主程序）
- `python -m estimator_king crawl` — 跑一次爬蟲循環後結束（手動 / backfill）

採**硬切換**：移除舊的呼叫方式（bare `python -m estimator_king` 與
`python -m estimator_king.bot`），所有引用點同步更新。

## 2. 現況

| 入口點 | 行為 |
|--------|------|
| `python -m estimator_king`（[estimator_king/__main__.py](../../../estimator_king/__main__.py)） | 跑一次 `run_crawl_cycle` 後 `sys.exit`，JSON counters 印到 stdout |
| `python -m estimator_king.bot`（[estimator_king/bot/__main__.py](../../../estimator_king/bot/__main__.py)） | 啟動 Discord bot + `CrawlScheduler`（每日自動爬一次）+ 服務 `/estimate` |

兩者都呼叫同一份
[estimator_king/crawler/cycle.py](../../../estimator_king/crawler/cycle.py) 的
`run_crawl_cycle()`，邏輯已共用；本任務只統一 module 入口點。

引用點（已全 repo 掃描）：

- [Dockerfile](../../../Dockerfile)：多階段，含一個已壞掉的 `crawler` 階段
  （ENTRYPOINT 指向不存在的 `estimator_king.crawler`）與 `bot` 階段
- [deploy/bot-deployment.yaml](../../../deploy/bot-deployment.yaml)：用
  `command:` 呼叫 `python -m estimator_king.bot ...`
- [README.md](../../../README.md)、
  [docs/local-runbook.md](../../local-runbook.md)、
  [docs/ops-runbook.md](../../ops-runbook.md)

## 3. CLI 介面（新）

```
python -m estimator_king run   [--config PATH] [--log-level LVL] [--token TOKEN] [--guild-id ID]
python -m estimator_king crawl [--config PATH] [--db PATH] [--log-level LVL] [--force-refetch]
```

- 未提供子命令時，argparse 印出 usage 並以非零碼結束（`required=True`）。
- `--config` 預設 `stores_config.yaml`，`--log-level` 預設 `INFO`，兩者為共用參數。
- `--db` 只在 `crawl`（沿用現況；`run` 仍用 `config.database_path`）。
- `--token`、`--guild-id` 只在 `run`。

### 行為保留約束

- **`crawl` 的 stdout 必須維持純 JSON**（runbook 有
  `... | python -m json.tool` 管線）。因此所有 logging 一律走
  `stream=sys.stderr`。
- **`crawl` 的 exit code 維持現況**：config 載入失敗 → 1；缺 embedding key →
  2；循環擲出例外 → 1；成功印 JSON → 0。
- **`run` 的 exit 行為維持現況**：config 載入失敗 → 1；缺 discord token → 1；
  缺 embedding key → 1；`KeyboardInterrupt` 安靜結束。
- logging 格式統一為 `%(asctime)s [%(levelname)s] %(message)s`（採原 bot 風格）。

### ChromaDB 單寫入者約束（決定 ops 文件改寫方向）

生產環境只跑單一 `run` 進程（bot + 程序內 `CrawlScheduler` 同程序，
`run_on_start=True` 故啟動即爬一次）。本地持久化的 ChromaDB 為**單寫入者**——
任一時刻只能有一個進程持有同一個 ChromaDB 路徑。因此：

- `crawl` 子命令**不得**與正在運行的 `run` 進程並存於同一 ChromaDB 路徑；它定位為
  **本地開發 / 離線 backfill** 工具，不是生產 ops 工具。
- 生產的 re-index / 全量重建採**重啟重建**模式（見 §8）：scale bot 到 0 釋放
  ChromaDB → 清空 `chroma/` 與 SQLite 狀態 DB → scale 回 1，bot 啟動時的
  on-start 爬取因 SQLite 已空而把所有產品當新發現，一個 cycle 全量重建。

## 4. 參數結構（argparse subparsers）

`parse_args()` 用 subparser，共用參數放在 parent parser 經 `parents=[common]`
掛到各子命令，使其可寫在子命令之後（`estimator_king crawl --config x`）。

```python
def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="estimator_king",
        description="Estimator King — Discord bot and product crawler",
    )

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default="stores_config.yaml",
                        help="Path to stores configuration YAML (default: stores_config.yaml)")
    common.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
                        help="Set logging level (default: INFO)")

    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", parents=[common],
                           help="Start the Discord bot with the in-process crawl scheduler")
    p_run.add_argument("--token", type=str, default=None,
                       help="Discord bot token (overrides DISCORD_TOKEN / DISCORD_BOT_TOKEN env)")
    p_run.add_argument("--guild-id", type=int, default=None,
                       help="Guild ID for command sync (optional, omit for global sync)")

    p_crawl = sub.add_parser("crawl", parents=[common],
                             help="Run one crawl cycle and exit")
    p_crawl.add_argument("--db", default=None,
                         help="Override database path from config")
    p_crawl.add_argument("--force-refetch", action="store_true", default=False,
                         help="Re-fetch all active products regardless of age")

    return parser.parse_args(argv)
```

`parse_args([])` 因 `required=True` 會 `SystemExit`（這是預期的硬切換行為）。

## 5. 程式碼結構與重構

### 5.1 [estimator_king/__main__.py](../../../estimator_king/__main__.py)：統一 dispatcher

頂層維持匯入 `AppConfig`、`run_crawl_cycle`、`EmbeddingProvider`、`VectorStore`、
`asyncio`、`json`，並新增 `from estimator_king.bot import runner as bot_runner`
（讓測試以 patch `estimator_king.__main__.X` / `estimator_king.__main__.bot_runner.run_bot`
攔截各分派路徑）。

- `parse_args(argv=None)`：如 §4。
- `run_crawl(args) -> None`：等同現有 `main()` body —— 以 `AppConfig.from_yaml(args.config)`
  載入 config、套用 `--db` 覆寫、驗證 embedding key（缺則 `sys.exit(2)`）、建
  embedder + vector_store、
  `asyncio.run(run_crawl_cycle(..., force_refetch=args.force_refetch))`、把
  counters 以 `json.dumps(..., indent=2)` `print` 到 stdout、`sys.exit(0)`；
  config 載入失敗或循環例外 → `sys.exit(1)`。
- `run_bot(args) -> None`：以 `AppConfig.from_yaml(args.config)` 載入 config
  （與 `run_crawl` 共用同一頂層匯入符號，使兩條分派路徑共享單一 patch 點）、
  套用 `--token` 覆寫、驗證 discord token（缺則 `sys.exit(1)`），再
  `asyncio.run(bot_runner.run_bot(config, guild_id=args.guild_id))`，
  並以 `try/except KeyboardInterrupt` 安靜結束。
- `_main() -> None`：`args = parse_args()`；設定
  `logging.basicConfig(level=getattr(logging, args.log_level),
  format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stderr)`；
  依 `args.command` 分派到 `run_crawl(args)` 或 `run_bot(args)`。
- `if __name__ == "__main__": _main()`。

> `run_bot(args)` 與 `bot.runner` 模組的 `run_bot(config, *, guild_id)` 同名但不同
> 命名空間（前者在 `estimator_king.__main__`、後者在 `estimator_king.bot.runner`），
> 各自負責 CLI 層與 async 層，互不衝突。

### 5.2 estimator_king/bot/runner.py（新增）

把現 [estimator_king/bot/__main__.py](../../../estimator_king/bot/__main__.py) 的
bot bootstrap 邏輯搬來：

- 模組層 `_background_tasks: set["asyncio.Task[None]"] = set()`
- `create_bot() -> discord.Client`（intents 設定，原樣搬移）
- `async def run_bot(config: AppConfig, *, guild_id: Optional[int]) -> None`：
  涵蓋現有 async `main()` 中**config 載入與 token 覆寫之後**的全部步驟——
  建 `provider_config`、驗證 embedding key（缺則 `sys.exit(1)`）、建 embedder /
  chat / vector_store / estimator、`create_bot()`、`setup_commands()`、建立
  `CrawlScheduler` 並以強引用 `create_task`、註冊 `on_ready`（沿用 guild vs
  global sync 邏輯，改讀 `guild_id` 參數）、註冊 SIGINT/SIGTERM graceful
  shutdown、`await bot.start(config.discord_token)`。

`config` 與 token 覆寫由呼叫端（`__main__.run_bot(args)`）先處理；本函式只收已就緒
的 `config` 與 `guild_id`。

### 5.3 移除 estimator_king/bot/__main__.py

硬切換：`python -m estimator_king.bot` 不再是入口點，刪除該檔。bot 邏輯改由
§5.2 的 `bot/runner.py` 承載，§5.1 的 dispatcher 呼叫。

## 6. Docker 變更

[Dockerfile](../../../Dockerfile) 合併為**單一 app image**：

- **移除整個 `crawler` 階段**（現 16–20 行，含 `pip install gunicorn` 與指向不存在
  module 的 `ENTRYPOINT ["python","-m","estimator_king.crawler"]`）——死階段。
- 把原 `bot` 階段改為單一最終階段（命名為 `app`），保留
  `pip install --no-cache-dir python-dotenv`（疑似未使用，但屬獨立清理，本任務
  保留不動；見 §8.2）。
- ENTRYPOINT 固定為 module、子命令交給 CMD：

```dockerfile
# Stage 2: App (unified entry point for run + crawl)
FROM base AS app

RUN pip install --no-cache-dir python-dotenv

ENTRYPOINT ["python", "-m", "estimator_king"]
CMD ["run"]
```

`docker build -t estimator-king .` 不帶 `--target`，會 build 最後一個階段
（全 repo 無任何 `--target` 引用，合併安全）。`docker run estimator-king` 預設跑
`run`；`docker run estimator-king crawl --force-refetch` 以 CMD 覆寫切到爬蟲。

## 7. 部署變更

[deploy/bot-deployment.yaml](../../../deploy/bot-deployment.yaml) 由 `command:`
（全覆蓋 ENTRYPOINT+CMD）改為 `args:`（沿用 image ENTRYPOINT，覆寫預設 CMD）：

```yaml
      containers:
        - name: bot
          image: estimator-king-bot:latest
          args:
            - run
            - --token
            - $(DISCORD_TOKEN)
            - --config
            - /config/stores_config.yaml
```

其餘 env / volumeMounts / labels 不變。image tag 名稱 `estimator-king-bot` 與
deployment / PVC label 維持不動（屬既有命名，與本任務無關）。

## 8. 文件更新

### 8.1 範圍內（入口點呼叫 + 直接受影響的程序）

- [README.md:80](../../../README.md)：
  `python -m estimator_king --force-refetch` →
  `python -m estimator_king crawl --force-refetch`
- [docs/local-runbook.md](../../local-runbook.md)：所有
  `python -m estimator_king [OPTIONS]` 的爬蟲呼叫 → `crawl` 子命令；所有
  `python -m estimator_king.bot [OPTIONS]` → `python -m estimator_king run`
  （含第 122、128、145、170、198、204、215–216、242、252、284、320 行附近的
  指令與包裝腳本）

#### ops-runbook：移除獨立 crawler / CronJob 模型（整份貫穿改寫）

[docs/ops-runbook.md](../../ops-runbook.md) 目前深度綁定「crawler 是獨立 CronJob
進程」的舊架構。生產環境只跑 `run`（§3 ChromaDB 單寫入者約束），故整份移除獨立
crawler 內容、把爬取重新定位為 `run` 程序內的階段。逐節改寫如下：

| 節 | 現況（行附近） | 改寫 |
| --- | --- | --- |
| 開頭簡介 | L3「the Estimator King crawler and Discord bot」 | 改述為單一 bot 程序內含 crawl 排程器 |
| §1 Initial Deployment | L57–60 `kubectl apply -f deploy/crawler-cronjob.yaml`（檔已刪除） | 移除 CronJob 套用步驟，只保留 bot Deployment 套用；L21 的 `crawler-pvc.yaml`（共用 PVC）維持 |
| §2 Secret Rotation | L86–88「Verify Crawler / 下次排程取得新 secret」 | 移除；改述 secret 輪替後重啟 bot Deployment（連帶排程器）即生效 |
| §3 Ad-hoc Crawl Commands | L92–123 整節（manual crawl / force-refetch / debug store，全部 `--from=cronjob` 或第二個 crawl 進程） | **整節移除**——生產不支援並存第二個爬取進程；本地一次性爬取改由 local-runbook 的 `python -m estimator_king crawl` 涵蓋 |
| §4 Log Inspection | L135–141「Crawler Logs (Latest Job)」、L152–154「(Crawler only)」欄位 | 移除 crawler job log 區塊（爬取 log 現於 bot log 內）；欄位註記 `(Crawler only)` → `(crawl phase only)` |
| §5 Recovery | L175–183「Crawler Failure / hung jobs」 | 標題改 `Crawl Cycle Failure`；移除「hung jobs」字眼，改述 in-process 爬取失敗的處置（重啟 bot；DB lock / PVC full / sitemap / embedding API 內容保留） |
| §6 Re-index Procedure | L186–213 `kubectl exec rm -rf chroma` + `--from=cronjob` 重抓 job | **改寫為重啟重建**：`kubectl scale deployment/estimator-king-bot --replicas=0` → 以 debug pod（掛 PVC）`rm -rf /data/chroma /data/estimator_king.db` → `--replicas=1`；bot 啟動的 on-start 爬取全量重建。註明會重置 SQLite 爬取狀態 |
| §7 Smoke Tests | L240–246「Summary Report Verification」抓 crawler pod 的 `Crawler completed` | 改抓 bot log 的 `Crawl cycle complete`（[scheduler.py:36](../../../estimator_king/bot/scheduler.py) 的 log 文字） |
| §8 Summary Report JSON | L253「the crawler outputs a JSON object to stdout」 | 釐清：CLI `crawl` 把 JSON 印到 stdout；`run` 內的排程器以 `logger.info` 記錄 counters（不印 stdout） |
| §9 Observability | L278–279「(Crawler only)」、L289–291「crawler CronJob fails」告警 | 欄位註記同 §4 改 `(crawl phase only)`；CronJob 告警改為「bot 每日爬取 `errors > 0` 連續 2 天」 |

> `deploy/crawler-pvc.yaml`、image tag、k8s label 中的 `crawler` 既有命名不在本任務
> 改名範圍（與入口點統一無關）。

### 8.2 標記但不在本任務處理（out of scope）

- `docs/superpowers/` 下的歷史 spec / plan：是當時時間點的記錄，維持原樣不追改。
- Dockerfile 的 `python-dotenv` 安裝：原始碼未 import，疑似可移除，但屬獨立清理，
  本任務保留不動。
- [README.md:94-95](../../../README.md) 的 `docker run ... estimator-king`
  （無子命令）片段：因新 image 以 `CMD ["run"]` 為預設，bare `docker run` 仍會啟動
  `run`，語意維持正確，**無需編輯**（列此僅為完成入口點引用稽核）。

## 9. 測試

- [tests/test_cli.py](../../../tests/test_cli.py)：
  - `parse_args` 測試改帶子命令，例如
    `parse_args(["crawl", "--config", "c.yaml", "--force-refetch"])`、
    `parse_args(["run", "--guild-id", "123"])`。
  - 移除 / 改寫假設 bare flag 的測試（`parse_args(["--force-refetch"])` 等）。
  - 新增 `parse_args([])` 應 `SystemExit`（子命令必填）。
  - `--help` 斷言改為頂層列出子命令 `{run,crawl}`；`crawl --help` 才含
    `--force-refetch`。
  - `run_crawl` 成功 / 缺 embedding key（exit 2）測試走 crawl 分派，patch
    `estimator_king.__main__.{AppConfig.from_yaml, EmbeddingProvider, VectorStore,
    run_crawl_cycle, asyncio.run}`。
- [tests/test_main_async.py](../../../tests/test_main_async.py)：`_make_args`
  補 `command="crawl"` 並改呼叫 `run_crawl`，patch 對象隨 §5.1 重構調整。
- 新增 `run` 分派路由測試：patch `estimator_king.__main__.bot_runner.run_bot`
  （或等效匯入點）與 `asyncio.run`，驗證 `parse_args(["run", ...])` 經 dispatcher
  正確導向 bot runner、且 `--token` 覆寫與 discord token 驗證行為符合預期。
- [tests/test_bot_commands.py](../../../tests/test_bot_commands.py) 等其餘測試
  匯入自 `estimator_king.bot.commands` / `.estimator` / `.scheduler`，不受入口點
  重構影響，不需改動。
- [tests/test_smoke.py](../../../tests/test_smoke.py)：`import estimator_king.bot`
  匯入的是 package（`bot/__init__.py`），刪除 `bot/__main__.py` 後 package 仍存在；
  其對 `estimator_king.crawler` 的匯入同理（匯入的是 package，本就無 `__main__`），
  皆不受影響，不需改動。

## 10. 驗收條件

1. `python -m estimator_king run` 啟動 bot + 排程器，行為與舊
   `python -m estimator_king.bot` 等價（含 `--token`、`--guild-id`、graceful
   shutdown、command sync）。
2. `python -m estimator_king crawl [--force-refetch] [--db ...]` 行為與舊 bare
   `python -m estimator_king` 等價（stdout 純 JSON、exit code 0/1/2 一致）。
3. `python -m estimator_king`（無子命令）印 usage 並以非零碼結束。
4. `estimator_king/bot/__main__.py` 已移除；`python -m estimator_king.bot` 不再可用。
5. Dockerfile 僅剩 base + app 兩階段，ENTRYPOINT=`["python","-m","estimator_king"]`、
   CMD=`["run"]`；無 `estimator_king.crawler` / `estimator_king.bot` 殘留。
6. deployment 以 `args: [run, ...]` 啟動，rollout 後 bot 正常登入。
7. README、local-runbook 的入口點呼叫全部更新為 `run` / `crawl` 子命令。
8. ops-runbook 已移除所有獨立 crawler / CronJob 內容（無 `cronjob/estimator-king-crawler`、
   `--from=cronjob`、`estimator-king-crawler` pod label、`crawler-cronjob.yaml` 殘留）；
   re-index 程序為重啟重建（scale 0 → 清 `chroma/`+`estimator_king.db` → scale 1）；
   爬取定位為 `run` 程序內階段；`crawl` 僅於 local-runbook 作本地工具出現。
9. `pyright`/型別檢查、`ruff` lint、相關單元測試（test_cli、test_main_async、
   test_scheduler、test_bot_commands）全數通過。

## 11. 不在範圍

- 不改 `run_crawl_cycle` 與爬蟲核心邏輯。
- 不改 `CrawlScheduler`、`Estimator`、Discord command 實作。
- 不移除 `python-dotenv`（§8.2）。
- 不改 image tag / k8s label / `deploy/crawler-pvc.yaml` 等既有 `crawler` 命名。
