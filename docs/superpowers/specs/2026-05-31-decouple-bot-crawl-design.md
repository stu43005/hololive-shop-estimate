# 解耦 bot 與 crawl 排程器 + 共用 provider 建立 — 設計規格

日期：2026-05-31

## 1. 目標

把 `run` 指令對 bot 與 crawl 排程器的耦合解開，讓兩者成為**彼此不依賴的獨立元件**，由 `run` 指令並行組合；同時把 provider 的建立（embedder / chat / vector_store + embedding key 驗證）抽成**單一共用函式**，供 `run` 與 `crawl` 兩個指令共用（消除重複程式碼）。

外部行為**完全不變**：`run` 仍 = Discord bot + 程序內 crawl 排程；`crawl` 仍是一次性爬取後結束。

## 2. 現況與問題

- `run` 指令路徑：[__main__.run_bot](../../../estimator_king/__main__.py)（CLI 層）→ [bot/runner.py::run_bot](../../../estimator_king/bot/runner.py)（async 層）。
- **耦合問題**：`bot/runner.py::run_bot` 同時擁有 Discord bot 與 `CrawlScheduler` 的生命週期——它建立 providers、建 Estimator、**建立並啟動 `CrawlScheduler`**、註冊 signal/shutdown、啟動 bot。crawl 排程在結構上隸屬於 bot。
- `CrawlScheduler` 住在 [bot/scheduler.py](../../../estimator_king/bot/scheduler.py)，但它只依賴 `run_crawl_cycle`，無任何 bot 邏輯——放在 bot package 不合理。
- **重複程式碼**：provider 建立（`build_provider_config` + embedding key 檢查 + `EmbeddingProvider` + `VectorStore`）在 [__main__.run_crawl](../../../estimator_king/__main__.py) 與 [bot/runner.py::run_bot](../../../estimator_king/bot/runner.py) 各寫一次。

## 3. 目標架構

```
run 指令 → __main__.run_service(args)        ← CLI 層：載 config、套 --token、驗證 token
             └─ asyncio.run(runtime.serve(config, guild_id=...))
                   ├─ build_providers(config)           ← 共用，唯一一份
                   │    → embedder / chat / vector_store （單一 ChromaDB 實例）
                   ├─ bot 元件   = bot/runner.build_bot(...)   ← 只負責 Discord bot
                   ├─ crawl 元件 = crawler/scheduler.CrawlScheduler(...)  ← 只負責 crawl 排程
                   └─ 並行執行兩者 + 統一 graceful shutdown

crawl 指令 → __main__.run_crawl(args)
              └─ build_providers(config) → embedder / vector_store → run_crawl_cycle 一次
```

關鍵不變式：**同一程序只建立一個 `VectorStore`（ChromaDB 單寫入者）**，由 `runtime.serve` 建立後同時注入 bot 與 scheduler 共用。

## 4. 新增模組：`estimator_king/runtime.py`

中性組合模組（不屬 bot 也不屬 crawl），供 `run` 與 `crawl` 共用。內容：

### 4.1 `MissingEmbeddingKey` 例外

```python
class MissingEmbeddingKey(Exception):
    """Raised by build_providers when no embedding API key is configured.

    The caller maps this to its own exit code (crawl -> 2, run -> 1) so the
    validation lives in one place while CLI exit semantics stay per-command.
    """
```

### 4.2 `Providers` 容器 + `build_providers`

```python
from dataclasses import dataclass
from typing import Optional

@dataclass
class Providers:
    embedder: EmbeddingProvider
    vector_store: VectorStore
    chat: Optional[ChatProvider] = None   # 只有 with_chat=True 時建立


def build_providers(config: AppConfig, *, with_chat: bool = False) -> Providers:
    provider_config = config.build_provider_config()
    if not provider_config.embedding_api_key:
        raise MissingEmbeddingKey()
    embedder = EmbeddingProvider(provider_config)
    vector_store = VectorStore(config.chroma_path)
    chat = ChatProvider(provider_config) if with_chat else None
    return Providers(embedder=embedder, vector_store=vector_store, chat=chat)
```

- **chat 仍由 `build_providers` 統一負責（唯一一處建 provider，DRY）**，但採 `with_chat` 旗標**條件式建立**：`crawl` 不需要 chat → `with_chat=False`（預設）→ 不建；`run` → `with_chat=True` → 建。
- **為何條件式而非無條件建**（關鍵正確性，已實測 `openai==2.38.0`）：`ChatProvider(provider_config)` 建構會執行 `OpenAI(api_key=config.chat_api_key, ...)`，而 openai 2.38.0 在 `api_key` 為空字串時 **raise `OpenAIError("Missing credentials...")`**。`build_provider_config` 的 `chat_api_key = chat_api_key or openai_api_key or ""`：當操作者只設 `EMBEDDING_API_KEY`（未設 `OPENAI_API_KEY` / `CHAT_API_KEY`）時，embedding key 非空（通過驗證）但 `chat_api_key=""`。若無條件建 chat，`crawl` 會被此 `OpenAIError` 打斷而 exit 1（回歸——現況 crawl 根本不建 chat）。故 crawl 一律 `with_chat=False`，從根本避免建構 chat。
- embedding key 驗證只在此一處；**不在此 `sys.exit`**，改 raise `MissingEmbeddingKey`，由呼叫端決定退出碼。

> `runtime.py` 頂層 import 清單（消除 implementer 推測空間）：`import asyncio`、`import os`、`import signal`、`from dataclasses import dataclass`、`from typing import Callable, Optional`、`import discord`、`from estimator_king.config_schema import AppConfig`、`from estimator_king.llm.embeddings import EmbeddingProvider`、`from estimator_king.llm.chat import ChatProvider`、`from estimator_king.vectorstore.store import VectorStore`、`from estimator_king.crawler.scheduler import CrawlScheduler`、`from estimator_king.bot.runner import build_bot`。（`logging` 視需要。）

### 4.3 元件生命週期協調（從 bot/runner 上移）

把目前 [bot/runner.py](../../../estimator_king/bot/runner.py) 的 `_background_tasks`、`_force_exit` / `_default_force_exit`、`_Shutdowner` 類別**整批搬到 `runtime.py`**——因為「協調 bot + scheduler 的關閉」是組合根的職責，不是 bot 的職責。`_Shutdowner` 維持現有兩段式語意（第一次訊號取消 scheduler task + 關 bot；第二次強制 `os._exit(130)`），其 `force_exit` 注入點保留供測試替換。

### 4.4 組合根 `serve`

```python
async def serve(config: AppConfig, *, guild_id: Optional[int]) -> None:
    providers = build_providers(config, with_chat=True)   # 可能 raise MissingEmbeddingKey
    assert providers.chat is not None   # with_chat=True 保證已建立（型別收窄）

    # crawl 元件（注入共用 vector_store）
    scheduler = CrawlScheduler(
        config, config.database_path, providers.embedder, providers.vector_store)
    scheduler_task = asyncio.create_task(scheduler.run_forever())
    _background_tasks.add(scheduler_task)
    scheduler_task.add_done_callback(_background_tasks.discard)

    # bot 元件（注入共用 providers）
    bot = build_bot(
        config,
        embedder=providers.embedder,
        chat=providers.chat,
        vector_store=providers.vector_store,
        guild_id=guild_id,
    )

    shutdowner = _Shutdowner(scheduler_task, bot)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdowner.handle_signal)

    assert config.discord_token is not None
    await bot.start(config.discord_token)
```

- 維持現有「scheduler 以背景 task 跑 `run_forever()`、bot 以 `await bot.start()` 跑」的並行模型；兩者透過共用的 `providers.vector_store` 操作同一 ChromaDB，但各自獨立（scheduler 不知道 bot，bot 不知道 scheduler）。

## 5. `bot/runner.py` 瘦身為「只負責 bot」

移除：providers 建立、embedding key 驗證、`CrawlScheduler` 建立/啟動、`_background_tasks`、`_force_exit`、`_Shutdowner`、signal 註冊（全部移到 `runtime.py`）。

保留 / 改寫為：

```python
def create_bot() -> discord.Client:
    # 不變（intents 設定）
    ...

def build_bot(
    config: AppConfig,
    *,
    embedder: EmbeddingProvider,
    chat: ChatProvider,
    vector_store: VectorStore,
    guild_id: Optional[int],
) -> discord.Client:
    """Construct a fully-configured (but not yet started) Discord client:
    build the Estimator from injected providers, register commands, and wire
    the on_ready command-sync handler. The caller starts it via bot.start()."""
    estimator = Estimator(embedder, chat, vector_store)
    bot = create_bot()
    tree = setup_commands(bot, config, estimator)

    @bot.event
    async def on_ready() -> None:
        # 沿用現有 guild vs global sync 邏輯（讀 guild_id 參數），不變
        ...

    return bot
```

- `build_bot` 接收注入的 `embedder/chat/vector_store`（不自己建），只組裝 bot 專屬的 Estimator + 指令 + on_ready，回傳**未啟動**的 client。
- bot 不再 import `CrawlScheduler`，達成 bot 與 crawl 的模組解耦。

## 6. 模組搬移：`bot/scheduler.py` → `crawler/scheduler.py`

`CrawlScheduler` 整個搬到 `estimator_king/crawler/scheduler.py`（內容不變——它本來就只依賴 `crawler.cycle.run_crawl_cycle`）。bot package 不再包含爬蟲排程。

更新所有 import：
- `runtime.py`：`from estimator_king.crawler.scheduler import CrawlScheduler`。
- 測試（見 §8）。

## 7. `estimator_king/__main__.py` 調整

### 7.1 `crawl` 指令改用共用 `build_providers`

`run_crawl(args)` 改為：

```python
def run_crawl(args) -> None:
    try:
        config = AppConfig.from_yaml(args.config)
    except Exception as e:
        logger.error("Failed to load config from %s: %s", args.config, e)
        sys.exit(1)
    if args.db is not None:
        config.database_path = args.db
    try:
        providers = build_providers(config)   # with_chat 預設 False → 不建 chat
    except MissingEmbeddingKey:
        logger.error("OPENAI_API_KEY (or EMBEDDING_API_KEY) is required")
        sys.exit(2)
    try:
        counters = asyncio.run(run_crawl_cycle(
            config, config.database_path, providers.embedder, providers.vector_store,
            force_refetch=args.force_refetch))
    except Exception as e:
        logger.error("Crawler failed: %s", e)
        sys.exit(1)
    print(json.dumps(counters, indent=2))
    sys.exit(0)
```

- 只用 `providers.embedder` 與 `providers.vector_store`；`providers.chat` 為 `None`（crawl 不需 chat、也避免缺 chat key 時建構失敗）。
- exit code 維持現況：config 載入失敗 → 1；缺 embedding key → 2；cycle 例外 → 1；成功 → 0。

### 7.2 `run` 指令處理器改名並改走組合根

把現有 `run_bot(args)` 改名為 `run_service(args)`，內容：

```python
def run_service(args) -> None:
    try:
        config = AppConfig.from_yaml(args.config)
    except Exception as e:
        sys.stderr.write(f"Error: Failed to load config: {e}\n")
        sys.exit(1)
    if args.token is not None:
        config.discord_token = args.token
    if not config.discord_token:
        sys.stderr.write("Error: --token required or set DISCORD_BOT_TOKEN / DISCORD_TOKEN\n")
        sys.exit(1)
    try:
        asyncio.run(serve(config, guild_id=args.guild_id))
    except MissingEmbeddingKey:
        sys.stderr.write("Error: OPENAI_API_KEY (or EMBEDDING_API_KEY) is required\n")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
```

- embedding key 缺失：`build_providers` 在 `serve` 內 raise，傳播出 `asyncio.run` → 此處 catch → exit 1（維持 run 現況）。
- `_main()` 的分派改為 `args.command == "run"` → `run_service(args)`；`__main__` 不再有 `run_bot` 符號。

### 7.3 import 調整

`__main__.py` 頂層**採具名 import**（唯一形式，使 mock patch 目標確定）：

```python
from estimator_king.runtime import serve, build_providers, MissingEmbeddingKey
```

因此測試一律 patch `estimator_king.__main__.serve` / `estimator_king.__main__.build_providers`（名稱被查找的位置在 `__main__`，與既有 `test_main_async.py` patch `estimator_king.__main__.EmbeddingProvider` 的風格一致）。`__main__.py` **不再** import `EmbeddingProvider` / `VectorStore`（移入 runtime），也不再 import `bot.runner`；`run_crawl_cycle`、`AppConfig`、`asyncio`、`json`、`logging` 維持頂層 import。

## 8. 測試

- **新增 `tests/test_runtime.py`**（patch 目標皆為 `estimator_king.runtime.X`）：
  - `build_providers(config)`（預設 `with_chat=False`）：回傳 `Providers`，`embedder` / `vector_store` 各建構一次，**`chat` 為 `None` 且 `ChatProvider` 完全不被建構**（patch `estimator_king.runtime.ChatProvider`，斷言 `assert_not_called()`）。
  - `build_providers(config, with_chat=True)`：`ChatProvider` 建構一次、`Providers.chat` 非 None。
  - `build_providers` 在 `embedding_api_key` 為空時 raise `MissingEmbeddingKey`（不 sys.exit；以 `pytest.raises` 斷言）。
  - `serve` 把**同一個** `vector_store` 同時注入 `CrawlScheduler` 與 `build_bot`：patch `estimator_king.runtime.build_providers` 回傳含 sentinel `vector_store` 的 `Providers`（chat 為 sentinel）、patch `estimator_king.runtime.CrawlScheduler` 與 `estimator_king.runtime.build_bot`（後者回傳 `fake_bot`，其 `start` 為 `AsyncMock` 以便 `await bot.start(...)` 即時返回）、patch `estimator_king.runtime.asyncio.create_task` 與 `estimator_king.runtime.asyncio.get_running_loop`（避免真正建立背景 task / 註冊 signal）；以 `asyncio.run(serve(config_mock, guild_id=None))` 執行，斷言 `CrawlScheduler` 收到的位置參數 `vector_store`（第 4 個）與 `build_bot` 收到的 `vector_store=` 是**同一物件**（即 `providers.vector_store`）。`config_mock.discord_token` 設為非 None 以通過 `assert`。
- **`tests/test_scheduler.py`**：import 由 `estimator_king.bot.scheduler` 改為 `estimator_king.crawler.scheduler`；4 處 patch `estimator_king.bot.scheduler.run_crawl_cycle` → `estimator_king.crawler.scheduler.run_crawl_cycle`（行為測試不變）。
- **`tests/test_runner_shutdown.py`**：`_Shutdowner` / `_background_tasks` / `_force_exit` 已從 `bot/runner.py` 移到 `runtime.py`，故 import 由 `from estimator_king.bot import runner` 改為 `from estimator_king import runtime`，`runner._Shutdowner` / `runner._background_tasks` → `runtime._Shutdowner` / `runtime._background_tasks`（含 `force_exit=` 注入測試，行為斷言不變）。
- **`tests/test_cli.py`**：
  - 頂層 import 由 `from estimator_king.__main__ import parse_args, run_bot, run_crawl` 改為 `... parse_args, run_service, run_crawl`。
  - `run` 路由測試：函式 `test_run_bot_*` 改測 `run_service`；patch 點由 `estimator_king.__main__.bot_runner.run_bot` 改為 `estimator_king.__main__.serve`（`new_callable=MagicMock`，避免 async coroutine 警告）；驗證 `--token` 覆寫、缺 token → exit 1、`mock_serve.assert_called_once_with(mock_cfg, guild_id=123)` 且 `mock_asyncio_run.assert_called_once_with(mock_serve.return_value)`。
  - crawl 測試：patch 點由 `estimator_king.__main__.{EmbeddingProvider,VectorStore}` 改為 patch `estimator_king.__main__.build_providers`，回傳 `Providers(embedder=mock, vector_store=mock, chat=None)`；缺 key 改為 `build_providers` `side_effect=MissingEmbeddingKey()` → 斷言 exit 2。
- **`tests/test_main_async.py`**：`run_crawl` 測試由 patch `estimator_king.__main__.{EmbeddingProvider,VectorStore}` 改為 patch `estimator_king.__main__.build_providers`（回傳 `Providers(embedder=mock, vector_store=mock, chat=None)`）；維持 `run_crawl_cycle` 呼叫參數（`args[0]=config, args[1]=db_path, args[2]=embedder, args[3]=vector_store`）與 exit code 斷言；保留以 `new_callable=MagicMock` patch `run_crawl_cycle`。`test_run_crawl_no_dify_client_constructed` 並斷言 `not hasattr(m, "run_bot")`（守護改名）。
- 既有 bot 指令/estimator 測試（test_bot_commands、test_estimator）不受影響（Estimator 仍由 `build_bot` 建）。

## 9. 驗收條件

1. `bot/runner.py` 不再 import 或建立 `CrawlScheduler`；不再包含 `_Shutdowner` / signal 註冊 / providers 建立。
2. `CrawlScheduler` 位於 `estimator_king/crawler/scheduler.py`；`estimator_king/bot/scheduler.py` 不再存在。
3. `runtime.build_providers` 是 embedder/chat/vector_store + embedding key 驗證的**唯一**來源；`run_crawl` 與 `serve` 都呼叫它，無重複的 provider 建立程式碼。
4. `runtime.serve` 把同一個 `VectorStore` 實例同時供給 bot（Estimator）與 scheduler（驗證單一 ChromaDB）。
5. 外部行為不變：`run` 啟動 bot + 程序內排程（含 on_ready sync、兩段式 graceful shutdown）；`crawl` 一次性爬取，stdout 純 JSON，exit code 0/1/2 一致；`run` 缺 token/缺 key 各 exit 1。
6. `python -m estimator_king`（無子命令）、`run`/`crawl` 子命令、`estimator_king.bot` 不可用等既有行為不變。
7. `_main()` dispatch：`run` → `run_service`、`crawl` → `run_crawl`；`estimator_king.__main__` 不再有 `run_bot` 符號（由 `test_run_crawl_no_dify_client_constructed` 的 `not hasattr(m, "run_bot")` 守護）。
8. `build_providers(config)`（預設）不建構 `ChatProvider`；`providers.chat` 為 `None`；只有 `with_chat=True`（`serve`）才建 chat。
9. `basedpyright estimator_king/` 0 errors；`pytest -q` 全綠（含新增 test_runtime、改寫的 test_cli/test_main_async/test_scheduler/test_runner_shutdown）。

## 10. 不在範圍

- 不改 `run_crawl_cycle`、`CrawlScheduler` 的爬取邏輯（只搬位置）。
- 不改 `Estimator`、Discord command、`EmbeddingProvider` / `ChatProvider` / `VectorStore` 的實作。
- 不改 CLI 介面（子命令、旗標、exit code 全保留）。
- 不改 Dockerfile / 部署 / 文件的入口點呼叫（`run` / `crawl` 子命令不變）。
- 不改既有日誌格式與 httpx 抑制邏輯。
