# 統一 CLI 入口點（子命令制）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把兩個獨立的 `__main__` 入口點合併為單一 `python -m estimator_king`，以子命令 `run`（bot + 程序內排程器）與 `crawl`（單次爬蟲）區分，並同步更新 Docker / 部署 / 文件 / 測試。

**Architecture:** `estimator_king/__main__.py` 變成 argparse subparser dispatcher；bot bootstrap 邏輯抽到新的 `estimator_king/bot/runner.py`；刪除 `estimator_king/bot/__main__.py`。Docker 合併為單一 image（`ENTRYPOINT ["python","-m","estimator_king"]` + `CMD ["run"]`）；k8s 部署改用 `args:`。生產只跑 `run`，ChromaDB 單寫入者，故 ops-runbook 移除獨立 crawler/CronJob 模型、re-index 改為重啟重建。

**Tech Stack:** Python 3.11、argparse、discord.py、pytest、basedpyright（型別檢查；專案未安裝 ruff）。

**驗證工具（每個含程式碼的 Task 完成後執行）：**
- 型別檢查：`.venv/bin/basedpyright`
- 測試：`.venv/bin/python -m pytest <path> -q`
- 專案未安裝 ruff/mypy，故 lint = basedpyright。

---

## Task 1: 抽出 bot runner

把 `estimator_king/bot/__main__.py` 的 bot bootstrap 邏輯搬到新模組 `runner.py`，提供 `create_bot()` 與 `async run_bot(config, *, guild_id)`。本 Task 不動 `__main__.py`、不刪舊檔——只新增，保持測試全綠。

**Files:**
- Create: `estimator_king/bot/runner.py`

- [ ] **Step 1: 建立 runner.py**

建立 `estimator_king/bot/runner.py`，內容如下：

```python
"""Bot runtime: build providers, register commands, start the scheduler and bot.

Extracted from the former ``estimator_king.bot.__main__`` so the unified
``python -m estimator_king`` dispatcher can start the bot via ``run_bot()``.
"""

import asyncio
import logging
import signal
import sys
from typing import Optional

import discord

from estimator_king.config_schema import AppConfig
from estimator_king.bot.commands import setup_commands

# Strong references to background tasks: asyncio only keeps a weak reference, so
# an unreferenced create_task() result can be garbage-collected mid-run.
_background_tasks: set["asyncio.Task[None]"] = set()


def create_bot() -> discord.Client:
    """Create and configure the Discord client with the required intents."""
    intents = discord.Intents.default()
    intents.message_content = True
    intents.guilds = True
    return discord.Client(intents=intents)


async def run_bot(config: AppConfig, *, guild_id: Optional[int]) -> None:
    """Build providers, register commands, start the crawl scheduler and the bot.

    The caller is responsible for loading ``config`` and applying any token
    override before calling this; here we only receive a ready ``config`` and
    the optional ``guild_id`` for command sync.
    """
    from estimator_king.llm.embeddings import EmbeddingProvider
    from estimator_king.llm.chat import ChatProvider
    from estimator_king.vectorstore.store import VectorStore
    from estimator_king.bot.estimator import Estimator
    from estimator_king.bot.scheduler import CrawlScheduler

    provider_config = config.build_provider_config()
    if not provider_config.embedding_api_key:
        sys.stderr.write("Error: OPENAI_API_KEY (or EMBEDDING_API_KEY) is required\n")
        sys.exit(1)

    embedder = EmbeddingProvider(provider_config)
    chat = ChatProvider(provider_config)
    vector_store = VectorStore(config.chroma_path)
    estimator = Estimator(embedder, chat, vector_store)

    bot = create_bot()
    tree = setup_commands(bot, config, estimator)

    scheduler = CrawlScheduler(config, config.database_path, embedder, vector_store)
    scheduler_task = asyncio.create_task(scheduler.run_forever())
    _background_tasks.add(scheduler_task)
    scheduler_task.add_done_callback(_background_tasks.discard)

    @bot.event
    async def on_ready() -> None:
        assert bot.user is not None
        logging.info(f"Logged in as {bot.user}")
        if guild_id:
            guild = discord.Object(id=guild_id)
            tree.copy_global_to(guild=guild)
            await tree.sync(guild=guild)
            logging.info(f"Synced commands to guild {guild_id}")
        else:
            await tree.sync()
            logging.info("Synced commands globally")
        logging.info("Bot ready and commands synchronized")

    async def shutdown() -> None:
        logging.info("Shutting down bot...")
        await bot.close()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(shutdown()))

    await bot.start(config.discord_token)
```

- [ ] **Step 2: 驗證模組可匯入且型別正確**

Run:

```bash
.venv/bin/python -c "import estimator_king.bot.runner as r; print(r.create_bot, r.run_bot)"
.venv/bin/basedpyright estimator_king/bot/runner.py
```

Expected: 印出 `create_bot` 與 `run_bot` 的 function 物件；basedpyright `0 errors`。

- [ ] **Step 3: 確認既有測試仍全綠**

Run: `.venv/bin/python -m pytest tests/test_smoke.py tests/test_scheduler.py -q`
Expected: PASS（新增 runner.py 不影響既有行為）。

- [ ] **Step 4: Commit**

```bash
git add estimator_king/bot/runner.py
git commit -m "refactor(bot): extract bot runtime into bot/runner.py"
```

---

## Task 2: 統一入口 dispatcher（含測試改寫）

把 `__main__.py` 改成 subparser dispatcher（`run_crawl` / `run_bot` / `_main`），並同步改寫對應測試。`__main__.py` 與其測試是同一原子單元，一起變更、一起 commit。

**Files:**
- Modify: `estimator_king/__main__.py`（整檔取代）
- Modify: `tests/test_cli.py`（整檔取代）
- Modify: `tests/test_main_async.py`（整檔取代）

- [ ] **Step 1: 改寫測試（先讓測試表達新介面）— tests/test_main_async.py**

以下列內容整檔取代 `tests/test_main_async.py`：

```python
"""Tests for run_crawl() wiring: EmbeddingProvider + VectorStore + run_crawl_cycle."""

import json
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_args(**kwargs):
    defaults = dict(
        config="stores.yaml",
        db=None,
        force_refetch=False,
    )
    defaults.update(kwargs)
    return MagicMock(**defaults)


def _make_cfg(*, embedding_api_key: str = "sk-test", db: str = "./estimator_king.db"):
    mock_cfg = MagicMock()
    mock_cfg.database_path = db
    mock_cfg.chroma_path = "./chroma"
    provider_cfg = MagicMock()
    provider_cfg.embedding_api_key = embedding_api_key
    mock_cfg.build_provider_config.return_value = provider_cfg
    return mock_cfg


# ---------------------------------------------------------------------------
# run_crawl() builds EmbeddingProvider and VectorStore, then calls run_crawl_cycle
# ---------------------------------------------------------------------------

def test_run_crawl_builds_embedding_provider_and_vector_store():
    """run_crawl() constructs EmbeddingProvider(provider_config) and VectorStore(chroma_path)."""
    from estimator_king.__main__ import run_crawl

    mock_cfg = _make_cfg()
    counters = {"discovered": 0, "fetched_ok": 0, "created": 0,
                "updated": 0, "skipped": 0, "inactive": 0, "errors": 0}

    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.EmbeddingProvider") as mock_ep, \
         patch("estimator_king.__main__.VectorStore") as mock_vs, \
         patch("estimator_king.__main__.asyncio.run", return_value=counters):
        with pytest.raises(SystemExit) as exc:
            run_crawl(_make_args())

    assert exc.value.code == 0
    mock_ep.assert_called_once_with(mock_cfg.build_provider_config.return_value)
    mock_vs.assert_called_once_with(mock_cfg.chroma_path)


def test_run_crawl_passes_embedder_and_vector_store_to_cycle():
    """run_crawl() passes the embedder and vector store to run_crawl_cycle(...)."""
    from estimator_king.__main__ import run_crawl

    mock_cfg = _make_cfg()
    counters = {"discovered": 1, "fetched_ok": 1, "created": 0,
                "updated": 0, "skipped": 1, "inactive": 0, "errors": 0}

    captured_coro = []

    def fake_asyncio_run(coro):
        captured_coro.append(coro)
        return counters

    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.EmbeddingProvider") as mock_ep, \
         patch("estimator_king.__main__.VectorStore") as mock_vs, \
         patch("estimator_king.__main__.run_crawl_cycle") as mock_cycle, \
         patch("estimator_king.__main__.asyncio.run", side_effect=fake_asyncio_run):
        with pytest.raises(SystemExit):
            run_crawl(_make_args())

    assert mock_cycle.called
    call_args = mock_cycle.call_args
    assert call_args.args[0] is mock_cfg  # config
    assert call_args.args[2] is mock_ep.return_value  # embedder
    assert call_args.args[3] is mock_vs.return_value  # vector_store


def test_run_crawl_passes_force_refetch_to_cycle():
    """run_crawl() passes force_refetch=True to run_crawl_cycle when --force-refetch given."""
    from estimator_king.__main__ import run_crawl

    mock_cfg = _make_cfg()
    counters = {"discovered": 0, "fetched_ok": 0, "created": 0,
                "updated": 0, "skipped": 0, "inactive": 0, "errors": 0}

    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.EmbeddingProvider"), \
         patch("estimator_king.__main__.VectorStore"), \
         patch("estimator_king.__main__.run_crawl_cycle") as mock_cycle, \
         patch("estimator_king.__main__.asyncio.run", return_value=counters):
        with pytest.raises(SystemExit):
            run_crawl(_make_args(force_refetch=True))

    assert mock_cycle.call_args.kwargs.get("force_refetch") is True


def test_run_crawl_uses_db_path_from_config():
    """run_crawl() passes config.database_path to run_crawl_cycle."""
    from estimator_king.__main__ import run_crawl

    mock_cfg = _make_cfg(db="/configured/path.db")
    counters = {"discovered": 0, "fetched_ok": 0, "created": 0,
                "updated": 0, "skipped": 0, "inactive": 0, "errors": 0}

    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.EmbeddingProvider"), \
         patch("estimator_king.__main__.VectorStore"), \
         patch("estimator_king.__main__.run_crawl_cycle") as mock_cycle, \
         patch("estimator_king.__main__.asyncio.run", return_value=counters):
        with pytest.raises(SystemExit):
            run_crawl(_make_args())

    assert mock_cycle.call_args.args[1] == "/configured/path.db"


def test_run_crawl_applies_db_override_before_cycle():
    """run_crawl() overrides config.database_path with --db value before calling cycle."""
    from estimator_king.__main__ import run_crawl

    mock_cfg = _make_cfg(db="./original.db")
    counters = {"discovered": 0, "fetched_ok": 0, "created": 0,
                "updated": 0, "skipped": 0, "inactive": 0, "errors": 0}

    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.EmbeddingProvider"), \
         patch("estimator_king.__main__.VectorStore"), \
         patch("estimator_king.__main__.run_crawl_cycle") as mock_cycle, \
         patch("estimator_king.__main__.asyncio.run", return_value=counters):
        with pytest.raises(SystemExit):
            run_crawl(_make_args(db="/override.db"))

    assert mock_cycle.call_args.args[1] == "/override.db"


def test_run_crawl_prints_json_counters(capsys):
    """run_crawl() prints JSON counters from run_crawl_cycle to stdout."""
    from estimator_king.__main__ import run_crawl

    mock_cfg = _make_cfg()
    counters = {"discovered": 10, "fetched_ok": 9, "created": 3,
                "updated": 2, "skipped": 4, "inactive": 1, "errors": 1}

    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.EmbeddingProvider"), \
         patch("estimator_king.__main__.VectorStore"), \
         patch("estimator_king.__main__.asyncio.run", return_value=counters):
        with pytest.raises(SystemExit) as exc:
            run_crawl(_make_args())

    assert exc.value.code == 0
    output = json.loads(capsys.readouterr().out)
    assert output == counters


def test_run_crawl_exits_2_when_embedding_key_missing():
    """run_crawl() exits 2 when embedding_api_key is falsy (None or empty string)."""
    from estimator_king.__main__ import run_crawl

    for empty_key in (None, ""):
        mock_cfg = _make_cfg(embedding_api_key=empty_key)
        with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg):
            with pytest.raises(SystemExit) as exc:
                run_crawl(_make_args())
        assert exc.value.code == 2, f"Expected exit 2 for embedding_api_key={empty_key!r}"


def test_run_crawl_exits_1_on_cycle_exception():
    """run_crawl() exits 1 when asyncio.run(run_crawl_cycle(...)) raises."""
    from estimator_king.__main__ import run_crawl

    mock_cfg = _make_cfg()
    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.EmbeddingProvider"), \
         patch("estimator_king.__main__.VectorStore"), \
         patch("estimator_king.__main__.asyncio.run",
               side_effect=RuntimeError("network error")):
        with pytest.raises(SystemExit) as exc:
            run_crawl(_make_args())

    assert exc.value.code == 1


def test_run_crawl_no_dify_client_constructed():
    """The refactored __main__ must NOT carry a DifyKBClient symbol."""
    import estimator_king.__main__ as m
    assert not hasattr(m, "DifyKBClient"), (
        "DifyKBClient should not be present in the refactored __main__"
    )
```

- [ ] **Step 2: 改寫測試 — tests/test_cli.py**

以下列內容整檔取代 `tests/test_cli.py`：

```python
"""Unit tests for CLI argument parser and orchestration (run/crawl subcommands)."""

import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from estimator_king.__main__ import parse_args


# ---------------------------------------------------------------------------
# parse_args — crawl subcommand
# ---------------------------------------------------------------------------

def test_parse_args_crawl_flags():
    """crawl subcommand exposes --config / --force-refetch; no --dify-* flag."""
    args = parse_args(["crawl", "--config", "c.yaml", "--force-refetch"])
    assert args.command == "crawl"
    assert args.config == "c.yaml"
    assert args.force_refetch is True
    assert not hasattr(args, "dify_api_key")


def test_parse_args_crawl_defaults():
    """crawl with no extra args uses expected defaults."""
    args = parse_args(["crawl"])
    assert args.command == "crawl"
    assert args.config == "stores_config.yaml"
    assert args.db is None
    assert args.log_level == "INFO"
    assert args.force_refetch is False


def test_parse_args_crawl_db_flag():
    """crawl accepts --db."""
    args = parse_args(["crawl", "--db", "/tmp/test.db"])
    assert args.db == "/tmp/test.db"


def test_parse_args_crawl_log_level():
    """crawl accepts the shared --log-level."""
    args = parse_args(["crawl", "--log-level", "DEBUG"])
    assert args.log_level == "DEBUG"


# ---------------------------------------------------------------------------
# parse_args — run subcommand
# ---------------------------------------------------------------------------

def test_parse_args_run_defaults():
    """run with no extra args uses expected defaults."""
    args = parse_args(["run"])
    assert args.command == "run"
    assert args.config == "stores_config.yaml"
    assert args.token is None
    assert args.guild_id is None
    assert args.log_level == "INFO"


def test_parse_args_run_token_and_guild():
    """run accepts --token and --guild-id."""
    args = parse_args(["run", "--token", "abc", "--guild-id", "123"])
    assert args.command == "run"
    assert args.token == "abc"
    assert args.guild_id == 123


# ---------------------------------------------------------------------------
# parse_args — subcommand required
# ---------------------------------------------------------------------------

def test_parse_args_requires_subcommand():
    """No subcommand is a SystemExit (hard cutover: subcommand required)."""
    with pytest.raises(SystemExit):
        parse_args([])


# ---------------------------------------------------------------------------
# --help (subprocess)
# ---------------------------------------------------------------------------

def test_top_level_help_lists_subcommands():
    """Top-level --help lists the run and crawl subcommands, no --dify-* flag."""
    result = subprocess.run(
        [sys.executable, "-m", "estimator_king", "--help"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "Estimator King" in result.stdout
    assert "run" in result.stdout
    assert "crawl" in result.stdout
    assert "--dify" not in result.stdout


def test_crawl_help_lists_crawl_flags():
    """`crawl --help` lists --config / --db / --force-refetch; no --dify-* flag."""
    result = subprocess.run(
        [sys.executable, "-m", "estimator_king", "crawl", "--help"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "--config" in result.stdout
    assert "--db" in result.stdout
    assert "--force-refetch" in result.stdout
    assert "--dify" not in result.stdout


# ---------------------------------------------------------------------------
# run_crawl() — exit codes (mocked)
# ---------------------------------------------------------------------------

def _make_crawl_args(**kwargs):
    defaults = dict(config="stores.yaml", db=None, force_refetch=False)
    defaults.update(kwargs)
    return MagicMock(**defaults)


def _make_cfg(*, embedding_api_key="sk-test"):
    mock_cfg = MagicMock()
    mock_cfg.database_path = "./estimator_king.db"
    mock_cfg.chroma_path = "./chroma"
    provider_cfg = MagicMock()
    provider_cfg.embedding_api_key = embedding_api_key
    mock_cfg.build_provider_config.return_value = provider_cfg
    return mock_cfg


def test_run_crawl_success_prints_json_and_exits_0(capsys):
    import json
    from estimator_king.__main__ import run_crawl

    mock_cfg = _make_cfg()
    counters = {"discovered": 5, "fetched_ok": 5, "created": 2,
                "updated": 1, "skipped": 2, "inactive": 0, "errors": 0}
    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.EmbeddingProvider"), \
         patch("estimator_king.__main__.VectorStore"), \
         patch("estimator_king.__main__.asyncio.run", return_value=counters):
        with pytest.raises(SystemExit) as exc:
            run_crawl(_make_crawl_args())

    assert exc.value.code == 0
    assert json.loads(capsys.readouterr().out)["discovered"] == 5


def test_run_crawl_missing_embedding_key_exits_2():
    from estimator_king.__main__ import run_crawl

    mock_cfg = _make_cfg(embedding_api_key="")
    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg):
        with pytest.raises(SystemExit) as exc:
            run_crawl(_make_crawl_args())
    assert exc.value.code == 2


def test_run_crawl_config_load_failure_exits_1():
    from estimator_king.__main__ import run_crawl

    with patch("estimator_king.__main__.AppConfig.from_yaml",
               side_effect=FileNotFoundError("Config not found")):
        with pytest.raises(SystemExit) as exc:
            run_crawl(_make_crawl_args(config="missing.yaml"))
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# run_bot() — routing + token handling (mocked)
# ---------------------------------------------------------------------------

def test_run_bot_routes_to_bot_runner_with_token_override():
    """run_bot() applies --token override and dispatches to bot_runner.run_bot."""
    from estimator_king.__main__ import run_bot

    mock_cfg = MagicMock()
    mock_cfg.discord_token = "cfg-token"
    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg), \
         patch("estimator_king.__main__.bot_runner.run_bot") as mock_run_bot, \
         patch("estimator_king.__main__.asyncio.run") as mock_asyncio_run:
        run_bot(MagicMock(config="stores.yaml", token="cli-token", guild_id=123))

    assert mock_cfg.discord_token == "cli-token"
    mock_run_bot.assert_called_once_with(mock_cfg, guild_id=123)
    mock_asyncio_run.assert_called_once_with(mock_run_bot.return_value)


def test_run_bot_exits_1_when_token_missing():
    """run_bot() exits 1 when no token is provided and config has none."""
    from estimator_king.__main__ import run_bot

    mock_cfg = MagicMock()
    mock_cfg.discord_token = None
    with patch("estimator_king.__main__.AppConfig.from_yaml", return_value=mock_cfg):
        with pytest.raises(SystemExit) as exc:
            run_bot(MagicMock(config="stores.yaml", token=None, guild_id=None))
    assert exc.value.code == 1


# ---------------------------------------------------------------------------
# CLI integration (subprocess)
# ---------------------------------------------------------------------------

def test_cli_no_subcommand_exits_nonzero():
    """`python -m estimator_king` with no subcommand prints usage and exits nonzero."""
    result = subprocess.run(
        [sys.executable, "-m", "estimator_king"],
        capture_output=True, text=True,
    )
    assert result.returncode != 0
    assert "usage" in result.stderr.lower()


def test_cli_crawl_missing_config_file_exits_1():
    """`crawl --config <missing>` exits 1 with a config-load error."""
    result = subprocess.run(
        [sys.executable, "-m", "estimator_king", "crawl",
         "--config", "/nonexistent/path/missing.yaml"],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "Failed to load config" in result.stderr or "No such file" in result.stderr
```

- [ ] **Step 3: 執行新測試，確認對舊 `__main__.py` 失敗**

Run: `.venv/bin/python -m pytest tests/test_cli.py tests/test_main_async.py -q`
Expected: FAIL（舊 `__main__.py` 只有 `main`/舊 `parse_args`，無 `run_crawl` / `run_bot` / `bot_runner`，且 `parse_args` 不吃子命令）。

- [ ] **Step 4: 改寫 `estimator_king/__main__.py`**

以下列內容整檔取代 `estimator_king/__main__.py`：

```python
"""Unified entry point: ``python -m estimator_king {run,crawl}``.

- ``run``   starts the Discord bot with the in-process crawl scheduler.
- ``crawl`` runs one crawl cycle (sitemap -> fetch -> embed -> upsert) and exits.
"""

import argparse
import asyncio
import json
import logging
import sys
from typing import Optional, Sequence

from estimator_king.config_schema import AppConfig
from estimator_king.crawler.cycle import run_crawl_cycle
from estimator_king.llm.embeddings import EmbeddingProvider
from estimator_king.vectorstore.store import VectorStore
from estimator_king.bot import runner as bot_runner


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


def run_crawl(args: argparse.Namespace) -> None:
    """Run one crawl cycle; print JSON counters to stdout and exit.

    Exit codes: config load failure -> 1; missing embedding key -> 2;
    cycle exception -> 1; success -> 0.
    """
    try:
        config = AppConfig.from_yaml(args.config)
    except Exception as e:
        logging.error("Failed to load config from %s: %s", args.config, e)
        sys.exit(1)

    if args.db is not None:
        config.database_path = args.db
    provider_config = config.build_provider_config()
    if not provider_config.embedding_api_key:
        logging.error("OPENAI_API_KEY (or EMBEDDING_API_KEY) is required")
        sys.exit(2)

    embedder = EmbeddingProvider(provider_config)
    vector_store = VectorStore(config.chroma_path)
    try:
        counters = asyncio.run(
            run_crawl_cycle(config, config.database_path, embedder, vector_store,
                            force_refetch=args.force_refetch))
    except Exception as e:
        logging.error("Crawler failed: %s", e)
        sys.exit(1)

    print(json.dumps(counters, indent=2))
    sys.exit(0)


def run_bot(args: argparse.Namespace) -> None:
    """Load config, apply token override, then start the bot via bot_runner.run_bot.

    Exit codes: config load failure -> 1; missing discord token -> 1.
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
        asyncio.run(bot_runner.run_bot(config, guild_id=args.guild_id))
    except KeyboardInterrupt:
        logging.info("Bot stopped by user")


def _main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
        stream=sys.stderr,
    )
    if args.command == "crawl":
        run_crawl(args)
    elif args.command == "run":
        run_bot(args)


if __name__ == "__main__":
    _main()
```

- [ ] **Step 5: 執行測試，確認全綠**

Run: `.venv/bin/python -m pytest tests/test_cli.py tests/test_main_async.py -q`
Expected: PASS（所有 parse_args、run_crawl、run_bot、subprocess 測試通過）。

- [ ] **Step 6: 型別檢查**

Run: `.venv/bin/basedpyright estimator_king/__main__.py`
Expected: `0 errors`。

- [ ] **Step 7: Commit**

```bash
git add estimator_king/__main__.py tests/test_cli.py tests/test_main_async.py
git commit -m "feat(cli): unify entry point with run/crawl subcommands"
```

---

## Task 3: 移除舊 bot 入口點

刪除 `estimator_king/bot/__main__.py`（bot 邏輯已搬到 `runner.py`，dispatcher 已接手）。

**Files:**
- Delete: `estimator_king/bot/__main__.py`

- [ ] **Step 1: 確認無任何程式碼 import 舊入口**

Run: `grep -rn "bot.__main__\|bot import __main__\|from estimator_king.bot.__main__" estimator_king tests`
Expected: 無任何匹配（runner.py 已取代；dispatcher import 的是 `bot.runner`）。

- [ ] **Step 2: 刪除檔案**

Run: `git rm estimator_king/bot/__main__.py`
Expected: 檔案被移除。

- [ ] **Step 3: 確認 package 仍可匯入、smoke 測試綠**

Run:

```bash
.venv/bin/python -c "import estimator_king.bot; print(estimator_king.bot)"
.venv/bin/python -m pytest tests/test_smoke.py -q
```

Expected: `estimator_king.bot` 匯入成功（package `__init__.py` 仍在）；smoke PASS。

- [ ] **Step 4: Commit**

```bash
git commit -m "refactor(bot): remove the standalone bot __main__ entry point"
```

---

## Task 4: Dockerfile 合併為單一 image

移除壞掉的 crawler 階段（含 `estimator_king.crawler` ENTRYPOINT 與 gunicorn），改為單一 `app` 階段，ENTRYPOINT 固定為 module、`CMD ["run"]`。

**Files:**
- Modify: `Dockerfile`（整檔取代）

- [ ] **Step 1: 改寫 Dockerfile**

以下列內容整檔取代 `Dockerfile`：

```dockerfile
# Multi-stage Dockerfile for Estimator King

# Stage 1: Base
FROM python:3.11-alpine AS base

WORKDIR /app

# Copy requirements first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy package code
COPY estimator_king/ estimator_king/

# Stage 2: App (unified entry point for run + crawl)
FROM base AS app

RUN pip install --no-cache-dir python-dotenv

ENTRYPOINT ["python", "-m", "estimator_king"]
CMD ["run"]
```

- [ ] **Step 2: 驗證沒有殘留的舊入口與壞階段**

Run:

```bash
grep -n "estimator_king.crawler\|estimator_king.bot\|AS crawler\|gunicorn" Dockerfile || echo "CLEAN"
grep -n "ENTRYPOINT\|CMD" Dockerfile
```

Expected: 第一個指令印 `CLEAN`（無殘留）；第二個印出 `ENTRYPOINT ["python", "-m", "estimator_king"]` 與 `CMD ["run"]`。

- [ ] **Step 3: Commit**

```bash
git add Dockerfile
git commit -m "build(docker): single image with estimator_king entrypoint, default CMD run"
```

---

## Task 5: 部署改用 args

`deploy/bot-deployment.yaml` 由 `command:`（全覆蓋 ENTRYPOINT+CMD）改為 `args:`（沿用 image ENTRYPOINT，覆寫預設 CMD），子命令改為 `run`。

**Files:**
- Modify: `deploy/bot-deployment.yaml:21-28`

- [ ] **Step 1: 改寫 command 區塊為 args**

把：

```yaml
          command:
            - python
            - -m
            - estimator_king.bot
            - --token
            - $(DISCORD_TOKEN)
            - --config
            - /config/stores_config.yaml
```

取代為：

```yaml
          args:
            - run
            - --token
            - $(DISCORD_TOKEN)
            - --config
            - /config/stores_config.yaml
```

- [ ] **Step 2: 驗證 YAML 合法且無舊入口殘留**

Run:

```bash
.venv/bin/python -c "import yaml; list(yaml.safe_load_all(open('deploy/bot-deployment.yaml')))" && echo "YAML OK"
grep -n "estimator_king.bot\|command:" deploy/bot-deployment.yaml || echo "NO OLD ENTRY"
grep -n "args:\|- run" deploy/bot-deployment.yaml
```

Expected: 印 `YAML OK`；第二個指令印 `NO OLD ENTRY`（無 `command:`、無 `estimator_king.bot`）；第三個印出 `args:` 與 `- run`。

- [ ] **Step 3: Commit**

```bash
git add deploy/bot-deployment.yaml
git commit -m "deploy(bot): start with args [run, ...] using the image entrypoint"
```

---

## Task 6: README + local-runbook 入口點更新

把 README 與 local-runbook 內所有舊入口點呼叫改為 `run` / `crawl` 子命令；並把 local-runbook 的日誌格式描述對齊統一後的 `[LEVEL]` 格式。

**Files:**
- Modify: `README.md:80`
- Modify: `docs/local-runbook.md`（多處）

- [ ] **Step 1: README.md — re-index 指令**

把 `README.md:80`：

```bash
> python -m estimator_king --force-refetch
```

改為：

```bash
> python -m estimator_king crawl --force-refetch
```

- [ ] **Step 2: local-runbook.md — crawl 呼叫（§4）**

逐處取代（保留各行其餘內容不變）：

- L122：`.venv/bin/python -m estimator_king --config stores_config.yaml` → `.venv/bin/python -m estimator_king crawl --config stores_config.yaml`
- L128：`python -m estimator_king [OPTIONS]` → `python -m estimator_king crawl [OPTIONS]`
- L145：`.venv/bin/python -m estimator_king --force-refetch` → `.venv/bin/python -m estimator_king crawl --force-refetch`
- L150：`structured format: `timestamp - LEVEL - message`` → `structured format: `timestamp [LEVEL] message``
- L170：`.venv/bin/python -m estimator_king --config stores_config.yaml > result.json` → `.venv/bin/python -m estimator_king crawl --config stores_config.yaml > result.json`

- [ ] **Step 3: local-runbook.md — bot 呼叫（§5）**

- L198：`.venv/bin/python -m estimator_king.bot` → `.venv/bin/python -m estimator_king run`
- L204：`python -m estimator_king.bot [OPTIONS]` → `python -m estimator_king run [OPTIONS]`
- L215：`python -m estimator_king.bot --guild-id 123456789` → `python -m estimator_king run --guild-id 123456789`
- L216：`python -m estimator_king.bot` → `python -m estimator_king run`

- [ ] **Step 4: local-runbook.md — 包裝腳本與 smoke / re-index（§6/§7/§8）**

- L242（`run-crawler.sh`）：`.venv/bin/python -m estimator_king --config stores_config.yaml "$@"` → `.venv/bin/python -m estimator_king crawl --config stores_config.yaml "$@"`
- L252（`run-bot.sh`）：`.venv/bin/python -m estimator_king.bot "$@"` → `.venv/bin/python -m estimator_king run "$@"`
- L284（§7.2）：`.venv/bin/python -m estimator_king --config stores_config.yaml 2>crawler.log | python -m json.tool` → `.venv/bin/python -m estimator_king crawl --config stores_config.yaml 2>crawler.log | python -m json.tool`
- L320（§8 re-index）：`.venv/bin/python -m estimator_king --force-refetch` → `.venv/bin/python -m estimator_king crawl --force-refetch`

- [ ] **Step 5: 驗證無殘留舊入口**

Run:

```bash
grep -nE "python -m estimator_king\.bot|python -m estimator_king --|python -m estimator_king \[OPTIONS\]" README.md docs/local-runbook.md || echo "CLEAN"
```

Expected: 印 `CLEAN`（README/local-runbook 中無 `estimator_king.bot`、無 bare `estimator_king --`、無 bare `[OPTIONS]`）。

- [ ] **Step 6: Commit**

```bash
git add README.md docs/local-runbook.md
git commit -m "docs: update README and local-runbook to run/crawl subcommands"
```

---

## Task 7: ops-runbook 移除獨立 crawler / CronJob 模型

整份移除「crawler 是獨立 CronJob 進程」內容、把爬取重新定位為 `run` 程序內的階段，re-index 改為重啟重建，日誌格式對齊 `[LEVEL]`。

**Files:**
- Modify: `docs/ops-runbook.md`（多處）

- [ ] **Step 1: 開頭簡介（L3）**

把：

```
This runbook provides procedures for deploying, managing, and troubleshooting the Estimator King crawler and Discord bot on Kubernetes.
```

改為：

```
This runbook provides procedures for deploying, managing, and troubleshooting the Estimator King Discord bot — which runs the crawl scheduler in-process — on Kubernetes.
```

- [ ] **Step 2: §1 部署步驟 5（L57-62）**

把：

````
5. **Deploy Crawler (CronJob) and Bot (Deployment)**:

   ```bash
   kubectl apply -f deploy/crawler-cronjob.yaml
   kubectl apply -f deploy/bot-deployment.yaml
   ```
````

改為：

````
5. **Deploy the Bot (Deployment)**:

   The bot process runs the crawl scheduler in-process; there is no separate crawler workload.

   ```bash
   kubectl apply -f deploy/bot-deployment.yaml
   ```
````

- [ ] **Step 3: §2 移除「Verify Crawler」步驟（L86-88）**

刪除整個步驟 3 區塊：

```
3. **Verify Crawler**:

   The crawler will pick up new secrets on its next scheduled run. No manual restart is needed unless a crawl is currently running.
```

（保留步驟 1、2；步驟 2 的 rollout restart 已涵蓋程序內排程器。）

- [ ] **Step 4: §3 改寫為「Crawling」說明（取代 L92-121 整節內容）**

把 `## 3. Ad-hoc Crawl Commands` 整節（從該標題到下一個 `---` 之前，含 Trigger a Manual Crawl / Force a Full Re-fetch / Debug a Specific Store 三個子節）取代為：

```
## 3. Crawling

Crawling runs **in-process** inside the bot: the `CrawlScheduler` triggers a cycle on startup (`run_on_start`) and then every `crawl_schedule_hours`. There is no separate crawler workload and no ad-hoc crawl job in production — the ChromaDB vector store is single-writer, so a second crawl process must never run against the live PVC.

- To force a fresh full rebuild (for example after an embedding-model change), see [§6 Re-index Procedure](#6-re-index-procedure).
- For a local one-off crawl during development, see [local-runbook.md](local-runbook.md) (`python -m estimator_king crawl`).
```

- [ ] **Step 5: §4 移除 Crawler Logs 區塊 + 格式/欄位更新**

5a. 刪除「Crawler Logs (Latest Job)」整個子項（L135-141）：

````
- **Crawler Logs (Latest Job)**:

  ```bash
  kubectl logs -n estimator-king \
    $(kubectl get pods -n estimator-king -l app.kubernetes.io/name=estimator-king-crawler \
      --sort-by=.metadata.creationTimestamp | tail -n 1 | awk '{print $1}')
  ```
````

並在保留的「Bot Logs」子項後補一句說明：

```
The in-process crawl cycle logs to the same bot logs (there is no separate crawler pod).
```

5b. 日誌格式（L145）：`Logging follows a structured format: `%(asctime)s - %(levelname)s - %(message)s`` → `Logging follows a structured format: `%(asctime)s [%(levelname)s] %(message)s``

5c. 欄位定義表（L152-154）：三處 `(Crawler only)` → `(crawl phase only)`。

5d. 「Common message patterns」清單中的最後一項（L161）——`crawler` 字眼且與重構後事實不符——取代。把：

```
- `Crawler completed: <JSON_SUMMARY>`: Final report for the entire run.
```

改為：

```
- `Crawl cycle complete: <JSON_SUMMARY>`: Final report for the entire crawl cycle (logged by the in-process scheduler under `run`).
```

（與 [scheduler.py:36](../../../estimator_king/bot/scheduler.py) 的 `Crawl cycle complete` log 字串一致，並消除 Step 11 grep 會卡住的 `Crawler completed` 殘留。）

- [ ] **Step 6: §5 Recovery（L175-183）**

- 標題 `### Crawler Failure` → `### Crawl Cycle Failure`
- L179 `1. **Database Lock**: If SQLite is locked, check for hung jobs and delete them.` → `1. **Database Lock**: If SQLite is locked, restart the bot Deployment to release the in-process lock.`
- 第 2–4 項（PVC Full / Sitemap Changes / Embedding API Error）內容保留不變。

- [ ] **Step 7: §6 Re-index Procedure 改寫為重啟重建（取代 L186-213 整節）**

把 `## 6. Re-index Procedure` 整節（從標題到下一個 `---` 之前）取代為：

````
## 6. Re-index Procedure

Vectors from different embedding models or dimension settings are incompatible. If you change `EMBEDDING_MODEL` or `EMBEDDING_DIMENSIONS`, you must clear the vector store and let the bot rebuild it from scratch. Because ChromaDB is single-writer, the bot must be stopped before clearing the data.

1. **Scale the bot down** (releases the PVC / ChromaDB):

   ```bash
   kubectl scale deployment/estimator-king-bot --replicas=0 -n estimator-king
   ```

2. **Clear the vector store and crawl state** with a short-lived pod that mounts the PVC:

   ```bash
   kubectl run reindex-clean -it --rm -n estimator-king \
     --image=busybox --restart=Never \
     --overrides='{"spec":{"containers":[{"name":"reindex-clean","image":"busybox","command":["sh","-c","rm -rf /data/chroma /data/estimator_king.db"],"volumeMounts":[{"name":"data","mountPath":"/data"}]}],"volumes":[{"name":"data","persistentVolumeClaim":{"claimName":"estimator-king-state-pvc"}}]}}'
   ```

3. **Scale the bot back up**:

   ```bash
   kubectl scale deployment/estimator-king-bot --replicas=1 -n estimator-king
   ```

On startup the bot's scheduler runs a crawl immediately (`run_on_start`). Because the SQLite crawl state was deleted, every product is rediscovered and re-embedded in a single cycle, rebuilding the vector index from scratch.

> Deleting `estimator_king.db` resets crawl state (content hashes, active/inactive tracking). This is intended for a full re-index — the next crawl rebuilds it.
````

- [ ] **Step 8: §7 Summary Report Verification + Bot Smoke 預期格式（L234-247）**

8a. Bot Smoke Test 預期輸出（L238）：`**Expected**: `... - INFO - Logged in as EstimatorKing#1234`` → `**Expected**: `... [INFO] Logged in as EstimatorKing#1234``

8b. Summary Report Verification（L240-247）整塊取代為：

````
### Summary Report Verification

```bash
kubectl logs -l app.kubernetes.io/name=estimator-king-bot -n estimator-king | grep "Crawl cycle complete"
```
````

- [ ] **Step 9: §8 Summary Report JSON 說明（L253）**

把：

```
At the end of every crawl run, the crawler outputs a JSON object to stdout and logs it.
```

改為：

```
The `crawl` CLI prints this JSON object to stdout on completion. Under `run`, the in-process scheduler logs the same counters via `logger.info` (`Crawl cycle complete: ...`) rather than printing to stdout.
```

- [ ] **Step 10: §9 Observability（L278-291）**

- L278-279：兩處 `(Crawler only)` → `(crawl phase only)`
- L289：`- **Crawler Failures**: Alert if `errors > 0` in the summary report.` → `- **Crawl Failures**: Alert if `errors > 0` in the crawl summary counters.`
- L291：`- **Persistent Failures**: Alert if crawler CronJob fails for 2 consecutive days.` → `- **Persistent Failures**: Alert if the bot's daily crawl logs `errors > 0` for 2 consecutive days.`

- [ ] **Step 11: 驗證 ops-runbook 無 crawler/CronJob 殘留**

Run:

```bash
grep -nE "cronjob/estimator-king-crawler|--from=cronjob|crawler-cronjob\.yaml|estimator-king-crawler|Crawler completed|Ad-hoc Crawl Commands" docs/ops-runbook.md || echo "CLEAN"
```

Expected: 印 `CLEAN`（無上述任何殘留）。

- [ ] **Step 12: Commit**

```bash
git add docs/ops-runbook.md
git commit -m "docs(ops): remove crawler CronJob model, restart-rebuild re-index"
```

---

## Task 8: 最終整體驗證

跨全套件確認重構無回歸，並手動驗證新 CLI 行為。

**Files:** 無（僅驗證）

- [ ] **Step 1: 型別檢查（全專案）**

Run: `.venv/bin/basedpyright`
Expected: `0 errors`（若有既存 warning 與本次無關，記錄但不視為失敗）。

- [ ] **Step 2: 全測試套件**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS（特別含 test_cli、test_main_async、test_scheduler、test_bot_commands、test_smoke）。

- [ ] **Step 3: 手動驗證 CLI 介面**

Run:

```bash
.venv/bin/python -m estimator_king; echo "no-subcmd exit=$?"
.venv/bin/python -m estimator_king --help >/dev/null; echo "help exit=$?"
.venv/bin/python -m estimator_king crawl --help >/dev/null; echo "crawl-help exit=$?"
.venv/bin/python -m estimator_king run --help >/dev/null; echo "run-help exit=$?"
```

Expected：`no-subcmd exit=2`（argparse 缺必填子命令）；其餘三個 `exit=0`。

- [ ] **Step 4: 確認 `python -m estimator_king.bot` 已不可用**

避免 pipeline 吃掉退出碼（本環境 Bash tool 實際為 zsh，`PIPESTATUS` 不適用）——直接以 `$?` 取結束碼：

```bash
.venv/bin/python -m estimator_king.bot >/tmp/bot_run.out 2>&1; echo "exit=$?"; head -3 /tmp/bot_run.out
```

Expected：`exit=1`，且輸出含 `No module named estimator_king.bot.__main__`（Task 3 刪除 `bot/__main__.py` 後即如此）。

- [ ] **Step 5: 最終 commit（如有未提交的驗證副產物則無，純驗證可略過）**

本 Task 不改檔，若 working tree 乾淨則無需 commit。Run: `git status --porcelain`
Expected: 空輸出（前述各 Task 已各自 commit）。

---

## 附註：本計畫相對 spec 的延伸

spec §3 要求統一日誌格式為 `[%(levelname)s]`。此格式變更的直接後果，是 local-runbook §4.4 與 ops-runbook §4/§7 中對舊 dash 格式（`- LEVEL -`）的描述需同步更新——這些更新已分別納入 Task 6 Step 2（L150）、Task 7 Step 5b（L145）與 Task 7 Step 8a（L238）。其餘皆嚴格對應 spec §4–§11。
