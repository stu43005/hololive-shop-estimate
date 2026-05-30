# 解耦 bot 與 crawl 排程器 + 共用 provider 建立 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `run` 指令對 bot 與 crawl 排程器的耦合解開（兩者成為獨立元件，由組合根 `runtime.serve` 並行組合），並把 provider 建立抽成單一 `runtime.build_providers` 供 run/crawl 共用。

**Architecture:** 新增中性模組 `estimator_king/runtime.py`（`build_providers` + 生命週期 `_Shutdowner` + 組合根 `serve`）；`CrawlScheduler` 從 `bot/` 搬到 `crawler/`；`bot/runner.py` 瘦身為只負責 bot（`create_bot` + `build_bot`）；`__main__.py` 的 `run` 走 `serve`、`crawl` 走 `build_providers`，`--db` 移到共用 parent。外部行為完全不變。

**Tech Stack:** Python 3.14、argparse、discord.py、pytest、basedpyright。

**驗證工具（每個含程式碼的 Task 後執行）：**
- 型別：`.venv/bin/basedpyright estimator_king/`（production 須 0 errors；測試檔的 MagicMock 型別 warning/error 屬專案既有 baseline，不 gate）
- 測試：`.venv/bin/python -m pytest <path> -q`

**關鍵不變式：** 同程序只建一個 `VectorStore`（ChromaDB 單寫入者），由 `serve` 建立後注入 bot 與 scheduler 共用。

---

## Task 1: 把 CrawlScheduler 搬到 crawler/scheduler.py

`CrawlScheduler` 只依賴 `crawler.cycle.run_crawl_cycle`，搬出 bot package。

**Files:**
- Move: `estimator_king/bot/scheduler.py` → `estimator_king/crawler/scheduler.py`
- Modify: `estimator_king/bot/runner.py`（lazy import 路徑）
- Modify: `tests/test_scheduler.py`（import + 4 處 monkeypatch 目標）

- [ ] **Step 1: git mv 搬移檔案**

Run:
```bash
git mv estimator_king/bot/scheduler.py estimator_king/crawler/scheduler.py
```
（檔案內容不變——它本來就只 import `estimator_king.crawler.cycle.run_crawl_cycle`。）

- [ ] **Step 2: 更新 bot/runner.py 內的 lazy import**

先 Read `estimator_king/bot/runner.py`，把 `run_bot` 函式體內的：
```python
    from estimator_king.bot.scheduler import CrawlScheduler
```
改為：
```python
    from estimator_king.crawler.scheduler import CrawlScheduler
```

- [ ] **Step 3: 更新 tests/test_scheduler.py**

先 Read `tests/test_scheduler.py`。把第 5 行：
```python
from estimator_king.bot.scheduler import CrawlScheduler
```
改為：
```python
from estimator_king.crawler.scheduler import CrawlScheduler
```
並把 **4 處** monkeypatch 目標（原 `"estimator_king.bot.scheduler.run_crawl_cycle"`）改為 `"estimator_king.crawler.scheduler.run_crawl_cycle"`（出現在 `test_run_once_calls_cycle`、`test_run_once_is_reentrancy_guarded`、`test_run_once_swallows_cycle_errors`、`test_run_forever_propagates_cancellation`）。四處字串完全相同，以 `replace_all` 一次替換最乾淨。

- [ ] **Step 4: 驗證**

Run:
```bash
.venv/bin/python -c "import estimator_king.crawler.scheduler; import estimator_king.bot.runner"
.venv/bin/python -m pytest tests/test_scheduler.py tests/test_smoke.py -q
```
Expected: import 成功；測試 PASS。

- [ ] **Step 5: Commit**

```bash
git add estimator_king/crawler/scheduler.py estimator_king/bot/runner.py tests/test_scheduler.py
git commit -m "refactor(crawler): move CrawlScheduler from bot/ to crawler/ package"
```
結尾加：
```
Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
```

---

## Task 2: 新增 runtime.py 的 build_providers + test_runtime

只建 provider 建立部分（不含 serve / _Shutdowner，那在 Task 4）。

**Files:**
- Create: `estimator_king/runtime.py`
- Create: `tests/test_runtime.py`

- [ ] **Step 1: 寫失敗測試 tests/test_runtime.py（build_providers 部分）**

建立 `tests/test_runtime.py`：
```python
"""Tests for runtime.build_providers (shared provider construction)."""

from unittest.mock import MagicMock, patch

import pytest

from estimator_king.runtime import build_providers, MissingEmbeddingKey, Providers


def _make_cfg(*, embedding_api_key="sk-test"):
    mock_cfg = MagicMock()
    mock_cfg.chroma_path = "./chroma"
    provider_cfg = MagicMock()
    provider_cfg.embedding_api_key = embedding_api_key
    mock_cfg.build_provider_config.return_value = provider_cfg
    return mock_cfg


def test_build_providers_without_chat_skips_chat_provider():
    """Default (with_chat=False): embedder + vector_store built, chat stays None."""
    mock_cfg = _make_cfg()
    with patch("estimator_king.runtime.EmbeddingProvider") as mock_ep, \
         patch("estimator_king.runtime.VectorStore") as mock_vs, \
         patch("estimator_king.runtime.ChatProvider") as mock_chat:
        providers = build_providers(mock_cfg)

    assert isinstance(providers, Providers)
    mock_ep.assert_called_once_with(mock_cfg.build_provider_config.return_value)
    mock_vs.assert_called_once_with(mock_cfg.chroma_path)
    mock_chat.assert_not_called()
    assert providers.chat is None
    assert providers.embedder is mock_ep.return_value
    assert providers.vector_store is mock_vs.return_value


def test_build_providers_with_chat_builds_chat_provider():
    """with_chat=True: ChatProvider constructed once, providers.chat non-None."""
    mock_cfg = _make_cfg()
    with patch("estimator_king.runtime.EmbeddingProvider"), \
         patch("estimator_king.runtime.VectorStore"), \
         patch("estimator_king.runtime.ChatProvider") as mock_chat:
        providers = build_providers(mock_cfg, with_chat=True)

    mock_chat.assert_called_once_with(mock_cfg.build_provider_config.return_value)
    assert providers.chat is mock_chat.return_value


def test_build_providers_raises_when_embedding_key_missing():
    """Empty embedding key raises MissingEmbeddingKey (no sys.exit)."""
    for empty_key in (None, ""):
        mock_cfg = _make_cfg(embedding_api_key=empty_key)
        with pytest.raises(MissingEmbeddingKey):
            build_providers(mock_cfg)
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `.venv/bin/python -m pytest tests/test_runtime.py -q`
Expected: FAIL（`estimator_king.runtime` 不存在）。

- [ ] **Step 3: 建立 estimator_king/runtime.py（build_providers 部分）**

建立 `estimator_king/runtime.py`：
```python
"""Composition root: shared provider construction and the long-lived service.

``build_providers`` is the single place that constructs the embedding / chat /
vector-store providers, shared by both the ``run`` and ``crawl`` commands. The
``serve`` composition root (added later) wires the bot and crawl scheduler as
two independent components over one shared VectorStore.
"""

from dataclasses import dataclass
from typing import Optional

from estimator_king.config_schema import AppConfig
from estimator_king.llm.embeddings import EmbeddingProvider
from estimator_king.llm.chat import ChatProvider
from estimator_king.vectorstore.store import VectorStore


class MissingEmbeddingKey(Exception):
    """Raised by build_providers when no embedding API key is configured.

    The caller maps this to its own exit code (crawl -> 2, run -> 1) so the
    validation lives in one place while CLI exit semantics stay per-command.
    """


@dataclass
class Providers:
    embedder: EmbeddingProvider
    vector_store: VectorStore
    chat: Optional[ChatProvider] = None


def build_providers(config: AppConfig, *, with_chat: bool = False) -> Providers:
    """Construct the shared providers; raise MissingEmbeddingKey if no key.

    chat is only built when with_chat=True (the bot needs it; crawl does not).
    Building ChatProvider with an empty chat_api_key raises OpenAIError under
    openai>=2, so crawl must never request it.
    """
    provider_config = config.build_provider_config()
    if not provider_config.embedding_api_key:
        raise MissingEmbeddingKey()
    embedder = EmbeddingProvider(provider_config)
    vector_store = VectorStore(config.chroma_path)
    chat = ChatProvider(provider_config) if with_chat else None
    return Providers(embedder=embedder, vector_store=vector_store, chat=chat)
```

- [ ] **Step 4: 執行測試確認通過 + 型別**

Run:
```bash
.venv/bin/python -m pytest tests/test_runtime.py -q
.venv/bin/basedpyright estimator_king/runtime.py
```
Expected: 3 passed；basedpyright 0 errors。

- [ ] **Step 5: Commit**

```bash
git add estimator_king/runtime.py tests/test_runtime.py
git commit -m "feat(runtime): add build_providers as the single shared provider factory"
```
結尾加 Co-Authored-By（同上）。

---

## Task 3: `crawl` 指令改用 build_providers

`__main__.run_crawl` 改用 `build_providers`，移除 `__main__` 內 `EmbeddingProvider`/`VectorStore` 的直接 import。`run`（`run_bot`）此 Task 不動。

**Files:**
- Modify: `estimator_king/__main__.py`（import + run_crawl）
- Modify: `tests/test_main_async.py`（整檔取代）
- Modify: `tests/test_cli.py`（crawl exit-code 測試 patch 點）

- [ ] **Step 1: 整檔取代 tests/test_main_async.py（先寫測試表達新介面）**

先 Read `tests/test_main_async.py`，以下列內容整檔取代：
```python
"""Tests for run_crawl() wiring: build_providers -> run_crawl_cycle."""

import json
from unittest.mock import MagicMock, patch

import pytest

import estimator_king.__main__ as m
from estimator_king.__main__ import run_crawl
from estimator_king.runtime import Providers, MissingEmbeddingKey


def _make_args(**kwargs):
    defaults = dict(config="stores.yaml", db=None, force_refetch=False)
    defaults.update(kwargs)
    return MagicMock(**defaults)


def _make_cfg(*, db="./estimator_king.db"):
    mock_cfg = MagicMock()
    mock_cfg.database_path = db
    return mock_cfg


def _make_providers():
    return Providers(embedder=MagicMock(), vector_store=MagicMock(), chat=None)


def test_run_crawl_passes_providers_to_cycle():
    """run_crawl passes providers.embedder / providers.vector_store to the cycle."""
    mock_cfg = _make_cfg()
    providers = _make_providers()
    counters = {"errors": 0}
    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.build_providers", return_value=providers), \
         patch("estimator_king.__main__.run_crawl_cycle", new_callable=MagicMock) as mock_cycle, \
         patch("estimator_king.__main__.asyncio.run", return_value=counters):
        with pytest.raises(SystemExit) as exc:
            run_crawl(_make_args())

    assert exc.value.code == 0
    call_args = mock_cycle.call_args
    assert call_args.args[0] is mock_cfg
    assert call_args.args[2] is providers.embedder
    assert call_args.args[3] is providers.vector_store


def test_run_crawl_passes_force_refetch_to_cycle():
    mock_cfg = _make_cfg()
    providers = _make_providers()
    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.build_providers", return_value=providers), \
         patch("estimator_king.__main__.run_crawl_cycle", new_callable=MagicMock) as mock_cycle, \
         patch("estimator_king.__main__.asyncio.run", return_value={"errors": 0}):
        with pytest.raises(SystemExit):
            run_crawl(_make_args(force_refetch=True))

    assert mock_cycle.call_args.kwargs.get("force_refetch") is True


def test_run_crawl_uses_db_path_from_config():
    mock_cfg = _make_cfg(db="/configured/path.db")
    providers = _make_providers()
    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.build_providers", return_value=providers), \
         patch("estimator_king.__main__.run_crawl_cycle", new_callable=MagicMock) as mock_cycle, \
         patch("estimator_king.__main__.asyncio.run", return_value={"errors": 0}):
        with pytest.raises(SystemExit):
            run_crawl(_make_args())

    assert mock_cycle.call_args.args[1] == "/configured/path.db"


def test_run_crawl_applies_db_override_before_cycle():
    mock_cfg = _make_cfg(db="./original.db")
    providers = _make_providers()
    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.build_providers", return_value=providers), \
         patch("estimator_king.__main__.run_crawl_cycle", new_callable=MagicMock) as mock_cycle, \
         patch("estimator_king.__main__.asyncio.run", return_value={"errors": 0}):
        with pytest.raises(SystemExit):
            run_crawl(_make_args(db="/override.db"))

    assert mock_cycle.call_args.args[1] == "/override.db"


def test_run_crawl_prints_json_counters(capsys):
    mock_cfg = _make_cfg()
    providers = _make_providers()
    counters = {"discovered": 10, "fetched_ok": 9, "created": 3,
                "updated": 2, "skipped": 4, "inactive": 1, "errors": 1}
    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.build_providers", return_value=providers), \
         patch("estimator_king.__main__.run_crawl_cycle", new_callable=MagicMock), \
         patch("estimator_king.__main__.asyncio.run", return_value=counters):
        with pytest.raises(SystemExit) as exc:
            run_crawl(_make_args())

    assert exc.value.code == 0
    assert json.loads(capsys.readouterr().out) == counters


def test_run_crawl_exits_2_when_embedding_key_missing():
    """build_providers raising MissingEmbeddingKey maps to exit 2."""
    mock_cfg = _make_cfg()
    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.build_providers",
               side_effect=MissingEmbeddingKey()):
        with pytest.raises(SystemExit) as exc:
            run_crawl(_make_args())
    assert exc.value.code == 2


def test_run_crawl_exits_1_on_cycle_exception():
    mock_cfg = _make_cfg()
    providers = _make_providers()
    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.build_providers", return_value=providers), \
         patch("estimator_king.__main__.run_crawl_cycle", new_callable=MagicMock), \
         patch("estimator_king.__main__.asyncio.run",
               side_effect=RuntimeError("network error")):
        with pytest.raises(SystemExit) as exc:
            run_crawl(_make_args())
    assert exc.value.code == 1


def test_run_crawl_no_dify_client_constructed():
    """The refactored __main__ must NOT carry a DifyKBClient symbol."""
    assert not hasattr(m, "DifyKBClient")
```

- [ ] **Step 2: 改 tests/test_cli.py 的 crawl exit-code 測試 patch 點**

先 Read `tests/test_cli.py`。把 `test_run_crawl_success_prints_json_and_exits_0` 內的：
```python
    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.EmbeddingProvider"), \
         patch("estimator_king.__main__.VectorStore"), \
         patch("estimator_king.__main__.run_crawl_cycle", new_callable=MagicMock), \
         patch("estimator_king.__main__.asyncio.run", return_value=counters):
```
改為（用 build_providers 取代 EmbeddingProvider/VectorStore）：
```python
    from estimator_king.runtime import Providers
    providers = Providers(embedder=MagicMock(), vector_store=MagicMock(), chat=None)
    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.build_providers", return_value=providers), \
         patch("estimator_king.__main__.run_crawl_cycle", new_callable=MagicMock), \
         patch("estimator_king.__main__.asyncio.run", return_value=counters):
```
把 `test_run_crawl_missing_embedding_key_exits_2` 改為：
```python
def test_run_crawl_missing_embedding_key_exits_2():
    from estimator_king.runtime import MissingEmbeddingKey
    mock_cfg = _make_cfg()
    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.build_providers", side_effect=MissingEmbeddingKey()):
        with pytest.raises(SystemExit) as exc:
            run_crawl(_make_crawl_args())
    assert exc.value.code == 2
```
（`test_run_crawl_config_load_failure_exits_1` 不變——它在 build_providers 之前就 fail。其餘 parse_args / run-routing 測試此 Task 不動。）

- [ ] **Step 3: 執行測試確認對舊 __main__ 失敗**

Run: `.venv/bin/python -m pytest tests/test_main_async.py -q`
Expected: FAIL（舊 `__main__` 無 `build_providers` 屬性，patch 失敗）。

- [ ] **Step 4: 改 estimator_king/__main__.py 的 import 與 run_crawl**

先 Read `estimator_king/__main__.py`。

4a. 頂層 import：把
```python
from estimator_king.llm.embeddings import EmbeddingProvider
from estimator_king.vectorstore.store import VectorStore
from estimator_king.bot import runner as bot_runner
```
改為
```python
from estimator_king.runtime import build_providers, MissingEmbeddingKey
from estimator_king.bot import runner as bot_runner
```
（移除 EmbeddingProvider / VectorStore，新增 build_providers / MissingEmbeddingKey；`bot_runner` 暫時保留給尚未改名的 `run_bot`，Task 4 移除。）

4b. 把 `run_crawl` 函式中「`provider_config = config.build_provider_config()` 起到 `vector_store = VectorStore(config.chroma_path)` 為止」的區塊：
```python
    if args.db is not None:
        config.database_path = args.db
    provider_config = config.build_provider_config()
    if not provider_config.embedding_api_key:
        logger.error("OPENAI_API_KEY (or EMBEDDING_API_KEY) is required")
        sys.exit(2)

    embedder = EmbeddingProvider(provider_config)
    vector_store = VectorStore(config.chroma_path)
    try:
        counters = asyncio.run(
            run_crawl_cycle(config, config.database_path, embedder, vector_store,
                            force_refetch=args.force_refetch))
```
改為：
```python
    if args.db is not None:
        config.database_path = args.db
    try:
        providers = build_providers(config)
    except MissingEmbeddingKey:
        logger.error("OPENAI_API_KEY (or EMBEDDING_API_KEY) is required")
        sys.exit(2)
    try:
        counters = asyncio.run(
            run_crawl_cycle(config, config.database_path,
                            providers.embedder, providers.vector_store,
                            force_refetch=args.force_refetch))
```

- [ ] **Step 5: 執行測試確認通過 + 型別**

Run:
```bash
.venv/bin/python -m pytest tests/test_main_async.py tests/test_cli.py -q
.venv/bin/basedpyright estimator_king/__main__.py
```
Expected: PASS；basedpyright 0 errors。

- [ ] **Step 6: Commit**

```bash
git add estimator_king/__main__.py tests/test_main_async.py tests/test_cli.py
git commit -m "refactor(cli): crawl command builds providers via runtime.build_providers"
```
結尾加 Co-Authored-By。

---

## Task 4: 組合根切換（serve）+ bot/runner 瘦身

一次原子變更：`bot/runner.py` 只剩 `create_bot` + `build_bot`；`_Shutdowner` / `_background_tasks` / `_force_exit` + `serve` 移入 `runtime.py`；`__main__` 的 `run_bot` 改名 `run_service` 走 `serve`。

**Files:**
- Modify: `estimator_king/bot/runner.py`（整檔取代）
- Modify: `estimator_king/runtime.py`（追加 serve + 生命週期）
- Modify: `estimator_king/__main__.py`（run_bot→run_service、import）
- Modify: `tests/test_runner_shutdown.py`（import 改 runtime）
- Modify: `tests/test_cli.py`（run-routing 測試）
- Modify: `tests/test_runtime.py`（追加 serve 測試）
- Modify: `tests/test_main_async.py`（補 `not hasattr(m, "run_bot")` 守護）

- [ ] **Step 1: 整檔取代 estimator_king/bot/runner.py**

先 Read，再以下列內容整檔取代：
```python
"""Bot construction: assemble a fully-configured (unstarted) Discord client.

The crawl scheduler and process lifecycle live in ``estimator_king.runtime``;
this module only knows how to build the bot (Estimator + commands + on_ready).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

import discord

from estimator_king.config_schema import AppConfig
from estimator_king.bot.commands import setup_commands

if TYPE_CHECKING:
    from estimator_king.llm.embeddings import EmbeddingProvider
    from estimator_king.llm.chat import ChatProvider
    from estimator_king.vectorstore.store import VectorStore

logger = logging.getLogger(__name__)


def create_bot() -> discord.Client:
    """Create and configure the Discord client with the required intents."""
    intents = discord.Intents.default()
    intents.guilds = True
    return discord.Client(intents=intents)


def build_bot(
    config: AppConfig,
    *,
    embedder: "EmbeddingProvider",
    chat: "ChatProvider",
    vector_store: "VectorStore",
    guild_id: Optional[int],
) -> discord.Client:
    """Construct a fully-configured (but not yet started) Discord client: build
    the Estimator from injected providers, register commands, and wire the
    on_ready command-sync handler. The caller starts it via bot.start()."""
    from estimator_king.bot.estimator import Estimator

    estimator = Estimator(embedder, chat, vector_store)
    bot = create_bot()
    tree = setup_commands(bot, config, estimator)

    @bot.event
    async def on_ready() -> None:
        assert bot.user is not None
        logger.info(f"Logged in as {bot.user}")
        if guild_id:
            guild = discord.Object(id=guild_id)
            tree.copy_global_to(guild=guild)
            await tree.sync(guild=guild)
            logger.info(f"Synced commands to guild {guild_id}")
        else:
            await tree.sync()
            logger.info("Synced commands globally")
        logger.info("Bot ready and commands synchronized")

    return bot
```

- [ ] **Step 2: 追加生命週期 + serve 到 estimator_king/runtime.py**

先 Read `estimator_king/runtime.py`。

2a. 把 Task 2 建立的頂層 import 區塊（確切 old_string）：
```python
from dataclasses import dataclass
from typing import Optional

from estimator_king.config_schema import AppConfig
from estimator_king.llm.embeddings import EmbeddingProvider
from estimator_king.llm.chat import ChatProvider
from estimator_king.vectorstore.store import VectorStore
```
替換為含生命週期所需的完整 import：
```python
import asyncio
import logging
import os
import signal
from dataclasses import dataclass
from typing import Callable, Optional

import discord

from estimator_king.config_schema import AppConfig
from estimator_king.llm.embeddings import EmbeddingProvider
from estimator_king.llm.chat import ChatProvider
from estimator_king.vectorstore.store import VectorStore
from estimator_king.crawler.scheduler import CrawlScheduler
from estimator_king.bot.runner import build_bot

logger = logging.getLogger(__name__)
```

2b. 在 `build_providers` 函式之後、檔尾，追加生命週期類別與 `serve`：
```python


# Strong references to background tasks: asyncio only keeps a weak reference, so
# an unreferenced create_task() result can be garbage-collected mid-run.
_background_tasks: set["asyncio.Task[None]"] = set()


def _force_exit(code: int) -> None:  # pragma: no cover - replaced via injection in tests
    os._exit(code)


_default_force_exit: Callable[[int], None] = _force_exit


class _Shutdowner:
    """Two-stage shutdown: first signal cancels the scheduler and closes the
    bot gracefully; a second signal forces an immediate exit (escape hatch for
    in-flight blocking work that cannot be cancelled cooperatively)."""

    _scheduler_task: "asyncio.Task[None]"
    _bot: discord.Client
    _force_exit: Callable[[int], None]
    _requested: bool

    def __init__(
        self,
        scheduler_task: "asyncio.Task[None]",
        bot: discord.Client,
        *,
        force_exit: Callable[[int], None] = _default_force_exit,
    ) -> None:
        self._scheduler_task = scheduler_task
        self._bot = bot
        self._force_exit = force_exit
        self._requested = False

    def handle_signal(self) -> None:
        if self._requested:
            logger.warning("Forced shutdown (second interrupt)")
            self._force_exit(130)
            return
        self._requested = True
        logger.info("Shutdown requested; press Ctrl+C again to force quit")
        task = asyncio.create_task(self.shutdown())
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    async def shutdown(self) -> None:
        logger.info("Shutting down bot...")
        self._scheduler_task.cancel()
        try:
            await self._scheduler_task
        except asyncio.CancelledError:
            pass
        await self._bot.close()


async def serve(config: AppConfig, *, guild_id: Optional[int]) -> None:
    """Composition root for ``run``: build shared providers once, then run the
    Discord bot and the crawl scheduler as two independent components over one
    shared VectorStore, with coordinated two-stage graceful shutdown."""
    providers = build_providers(config, with_chat=True)
    assert providers.chat is not None

    scheduler = CrawlScheduler(
        config, config.database_path, providers.embedder, providers.vector_store)
    scheduler_task = asyncio.create_task(scheduler.run_forever())
    _background_tasks.add(scheduler_task)
    scheduler_task.add_done_callback(_background_tasks.discard)

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

- [ ] **Step 3: 改 estimator_king/__main__.py：run_bot → run_service**

先 Read。

3a. 頂層 import：把
```python
from estimator_king.runtime import build_providers, MissingEmbeddingKey
from estimator_king.bot import runner as bot_runner
```
改為
```python
from estimator_king.runtime import serve, build_providers, MissingEmbeddingKey
```
（新增 `serve`，移除 `bot_runner` import。）

3b. 把整個 `run_bot` 函式：
```python
def run_bot(args) -> None:
    ...
    try:
        asyncio.run(bot_runner.run_bot(config, guild_id=args.guild_id))
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
```
改名並改走 serve（同時保留 config 載入 + token 驗證；embedding key 缺失改由 serve 內 build_providers raise，於此 catch → exit 1）：
```python
def run_service(args) -> None:
    """Run the long-lived service (bot + in-process crawl scheduler).

    Exit codes: config load failure -> 1; missing discord token -> 1;
    missing embedding key (from serve/build_providers) -> 1.
    KeyboardInterrupt exits quietly.
    """
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

3c. `_main()` 的分派：把
```python
    elif args.command == "run":
        run_bot(args)
```
改為
```python
    elif args.command == "run":
        run_service(args)
```

- [ ] **Step 4: 改 tests/test_runner_shutdown.py 的 import**

先 Read。把第 5 行 `from estimator_king.bot import runner` 改為 `from estimator_king import runtime as runner`。

（用 `as runner` 別名，使檔內既有的 `runner._Shutdowner` / `runner._background_tasks` 引用無需逐處改動，行為斷言不變。）

- [ ] **Step 5: 改 tests/test_cli.py 的 run-routing 測試與 import**

先 Read。

5a. 頂層 import 第 10 行：`from estimator_king.__main__ import parse_args, run_bot, run_crawl` → `from estimator_king.__main__ import parse_args, run_service, run_crawl`。

5b. `test_run_bot_routes_to_bot_runner_with_token_override` 整支改為：
```python
def test_run_service_routes_to_serve_with_token_override():
    """run_service applies --token override and dispatches to runtime.serve."""
    mock_cfg = MagicMock()
    mock_cfg.discord_token = "cfg-token"
    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.serve", new_callable=MagicMock) as mock_serve, \
         patch("estimator_king.__main__.asyncio.run") as mock_asyncio_run:
        run_service(MagicMock(config="stores.yaml", db=None, token="cli-token", guild_id=123))

    assert mock_cfg.discord_token == "cli-token"
    mock_serve.assert_called_once_with(mock_cfg, guild_id=123)
    mock_asyncio_run.assert_called_once_with(mock_serve.return_value)
```

5c. `test_run_bot_exits_1_when_token_missing` 整支改為：
```python
def test_run_service_exits_1_when_token_missing():
    """run_service exits 1 when no token is provided and config has none."""
    mock_cfg = MagicMock()
    mock_cfg.discord_token = None
    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg):
        with pytest.raises(SystemExit) as exc:
            run_service(MagicMock(config="stores.yaml", db=None, token=None, guild_id=None))
    assert exc.value.code == 1
```

- [ ] **Step 6: 追加 serve 注入測試到 tests/test_runtime.py**

先 Read，在檔尾追加：
```python
def test_serve_shares_one_vector_store_between_scheduler_and_bot():
    """serve injects the SAME vector_store into CrawlScheduler and build_bot."""
    from unittest.mock import AsyncMock
    import asyncio as _asyncio
    from estimator_king import runtime

    providers = Providers(embedder=MagicMock(), vector_store=MagicMock(), chat=MagicMock())
    fake_bot = MagicMock()
    fake_bot.start = AsyncMock()
    cfg = MagicMock()
    cfg.discord_token = "tok"

    with patch("estimator_king.runtime.build_providers", return_value=providers), \
         patch("estimator_king.runtime._background_tasks", set()), \
         patch("estimator_king.runtime.CrawlScheduler") as MockSched, \
         patch("estimator_king.runtime.build_bot", return_value=fake_bot) as mock_build_bot, \
         patch("estimator_king.runtime.asyncio.create_task"), \
         patch("estimator_king.runtime.asyncio.get_running_loop"):
        _asyncio.run(runtime.serve(cfg, guild_id=None))

    sched_vs = MockSched.call_args.args[3]          # CrawlScheduler(config, db, embedder, vector_store)
    bot_vs = mock_build_bot.call_args.kwargs["vector_store"]
    assert sched_vs is bot_vs is providers.vector_store
    fake_bot.start.assert_awaited_once()
```

> `patch("estimator_king.runtime._background_tasks", set())` 是必要的：`serve` 內
> `_background_tasks.add(scheduler_task)` 會把被 patch 成 MagicMock 的 task 塞進模組全域
> set，且 `add_done_callback` 不會真的 fire `discard`，殘留物會在後續 `test_runner_shutdown`
> 的 drain 迴圈（`asyncio.gather(<MagicMock>)`）引發 TypeError。patch 成拋棄式 set 可隔離。

- [ ] **Step 6.5: 補 `not hasattr(m, "run_bot")` 守護到 tests/test_main_async.py**

`run_bot` 在本 Task 已改名為 `run_service`，補上持久化回歸守護（spec §9.7）。先 Read `tests/test_main_async.py`，把 `test_run_crawl_no_dify_client_constructed`：
```python
def test_run_crawl_no_dify_client_constructed():
    """The refactored __main__ must NOT carry a DifyKBClient symbol."""
    assert not hasattr(m, "DifyKBClient")
```
改為：
```python
def test_run_crawl_no_dify_client_constructed():
    """The refactored __main__ must NOT carry DifyKBClient nor the renamed run_bot."""
    assert not hasattr(m, "DifyKBClient")
    assert not hasattr(m, "run_bot")  # renamed to run_service in this task
```

- [ ] **Step 7: 執行測試確認通過 + 型別**

Run:
```bash
.venv/bin/python -m pytest tests/test_runtime.py tests/test_runner_shutdown.py tests/test_cli.py tests/test_main_async.py tests/test_scheduler.py -q
.venv/bin/basedpyright estimator_king/
```
Expected: PASS；basedpyright `estimator_king/` 0 errors。

額外確認 `__main__` 無 `run_bot` 符號、無 unawaited coroutine 警告：
```bash
.venv/bin/python -c "import estimator_king.__main__ as m; assert not hasattr(m, 'run_bot'); assert not hasattr(m, 'bot_runner'); print('ok')"
.venv/bin/python -m pytest tests/test_cli.py tests/test_runtime.py -W error::RuntimeWarning -q
```
Expected: 印 `ok`；測試全 PASS。

- [ ] **Step 8: Commit**

```bash
git add estimator_king/bot/runner.py estimator_king/runtime.py estimator_king/__main__.py tests/test_runner_shutdown.py tests/test_cli.py tests/test_runtime.py tests/test_main_async.py
git commit -m "refactor(runtime): add serve composition root, slim bot/runner to build_bot"
```
結尾加 Co-Authored-By。

---

## Task 5: `--db` 移到共用 parent（run 也可用）+ run_service 覆寫

**Files:**
- Modify: `estimator_king/__main__.py`（parse_args + run_service）
- Modify: `tests/test_cli.py`（新增 run --db 測試）

- [ ] **Step 1: 寫失敗測試 tests/test_cli.py**

先 Read。在 parse_args run 區段附近新增：
```python
def test_parse_args_run_db_flag():
    """run accepts the shared --db (moved to the common parent)."""
    args = parse_args(["run", "--db", "/x"])
    assert args.command == "run"
    assert args.db == "/x"
```
並在 run-routing 測試區段新增（驗證 run_service 套用 --db 覆寫）：
```python
def test_run_service_applies_db_override():
    """run_service overrides config.database_path when --db is given."""
    mock_cfg = MagicMock()
    mock_cfg.discord_token = "tok"
    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.serve", new_callable=MagicMock), \
         patch("estimator_king.__main__.asyncio.run"):
        run_service(MagicMock(config="stores.yaml", db="/x", token=None, guild_id=None))
    assert mock_cfg.database_path == "/x"
```

- [ ] **Step 2: 執行測試確認失敗**

Run: `.venv/bin/python -m pytest tests/test_cli.py::test_parse_args_run_db_flag tests/test_cli.py::test_run_service_applies_db_override -q`
Expected: FAIL（run 尚無 --db；run_service 尚未套用 db 覆寫）。

- [ ] **Step 3: parse_args 把 --db 移到 common parent**

先 Read `estimator_king/__main__.py`。在 `common` parent parser 中（`--config` 與 `--log-level` 之間或之後）新增：
```python
    common.add_argument("--db", default=None,
                        help="Override database path from config")
```
並從 `p_crawl` 移除其原本的：
```python
    p_crawl.add_argument("--db", default=None,
                         help="Override database path from config")
```
（`p_crawl` 仍透過 `parents=[common]` 取得 `--db`；不可同時定義於 common 與 p_crawl，否則 argparse 衝突。）

- [ ] **Step 4: run_service 套用 --db 覆寫**

在 `run_service` 中，把（Task 4 結束後的這段，token 覆寫緊接 config 載入）：
```python
    if args.token is not None:
        config.discord_token = args.token
```
改為（在前面插入 db 覆寫，得到 spec §7.2 的「db 先於 token」順序）：
```python
    if args.db is not None:
        config.database_path = args.db
    if args.token is not None:
        config.discord_token = args.token
```
（以 `if args.token is not None:` 整段為錨點，避免用不唯一的 `sys.exit(1)` 作錨。此變更後 `run_service` 會讀 `args.db`，故所有 `run_service` 測試的 args mock 都須帶 `db=...`；Task 4 的路由測試已帶 `db=None`。）

- [ ] **Step 5: 執行測試確認通過 + 型別**

Run:
```bash
.venv/bin/python -m pytest tests/test_cli.py -q
.venv/bin/basedpyright estimator_king/__main__.py
```
Expected: PASS（含新增的兩個 db 測試與既有 crawl --db 測試）；0 errors。

- [ ] **Step 6: Commit**

```bash
git add estimator_king/__main__.py tests/test_cli.py
git commit -m "feat(cli): accept --db on the run command (shared parent); apply to scheduler db path"
```
結尾加 Co-Authored-By。

---

## Task 6: 最終整體驗證

**Files:** 無（僅驗證）

- [ ] **Step 1: 型別檢查（全 production）**

Run: `.venv/bin/basedpyright estimator_king/`
Expected: `0 errors`。

- [ ] **Step 2: 全測試套件**

Run: `.venv/bin/python -m pytest -q`
Expected: 全 PASS（含 test_runtime、test_runner_shutdown、test_cli、test_main_async、test_scheduler、test_smoke、test_bot_commands、test_estimator）。

- [ ] **Step 3: 手動驗證 CLI（含 run 的 --db）**

Run:
```bash
.venv/bin/python -m estimator_king >/dev/null 2>&1; echo "no-subcmd exit=$?"
.venv/bin/python -m estimator_king run --help 2>&1 | grep -c -- "--db"; echo "run-help shows --db (expect >=1)"
.venv/bin/python -m estimator_king crawl --help 2>&1 | grep -c -- "--db"; echo "crawl-help shows --db (expect >=1)"
```
Expected：`no-subcmd exit=2`；run-help 與 crawl-help 各印出 `--db` 計數 >= 1。

- [ ] **Step 4: 確認模組解耦（bot 不再依賴 scheduler/providers/signal；runtime 為組合根）**

Run（注意 grep 的 `&&`/`||`：有匹配 → 報 FAIL，無匹配 → 報 CLEAN）：
```bash
grep -rn "CrawlScheduler\|_Shutdowner\|build_providers\|add_signal_handler\|signal\." estimator_king/bot/runner.py \
  && echo "runner STILL references coupled symbols (FAIL)" \
  || echo "runner CLEAN of scheduler/shutdowner/providers/signal"
.venv/bin/python -c "import estimator_king.bot.runner as r; assert not hasattr(r, 'run_bot'); assert not hasattr(r, '_Shutdowner'); print('runner slim ok')"
.venv/bin/python -c "import estimator_king.runtime as rt; assert hasattr(rt, 'serve') and hasattr(rt, 'build_providers') and hasattr(rt, '_Shutdowner'); print('runtime ok')"
.venv/bin/python -c "import estimator_king.__main__ as m; assert not hasattr(m, 'run_bot'); assert hasattr(m, 'run_service') and hasattr(m, 'run_crawl'); print('__main__ dispatch ok')"
ls estimator_king/bot/scheduler.py 2>&1 | head -1
ls estimator_king/crawler/scheduler.py 2>&1 | head -1
```
Expected：runner 印 `runner CLEAN of scheduler/shutdowner/providers/signal` 與 `runner slim ok`；runtime 印 `runtime ok`；`__main__` 印 `__main__ dispatch ok`；`bot/scheduler.py` 不存在（No such file）、`crawler/scheduler.py` 存在。

- [ ] **Step 5: 確認 build_providers 是唯一 provider 來源（§9.3）**

Run（檢查 runtime 之外無殘留的 provider 建構）：
```bash
grep -rn "EmbeddingProvider(\|ChatProvider(\|VectorStore(" estimator_king/ --include="*.py" | grep -v "estimator_king/runtime.py" \
  && echo "provider construction LEAKED outside runtime (FAIL)" \
  || echo "build_providers is the sole provider source OK"
```
Expected：印 `build_providers is the sole provider source OK`（grep 無匹配）。

> §9.4（serve 注入同一 VectorStore）與 §9.8（build_providers 預設不建 chat）為行為不變式，由 Step 2 的 `tests/test_runtime.py`（`test_serve_shares_one_vector_store_between_scheduler_and_bot`、`test_build_providers_without_chat_skips_chat_provider`）覆蓋。

- [ ] **Step 6: 確認 working tree 乾淨**

Run: `git status --porcelain`
Expected: 空輸出（各 Task 已各自 commit）。
