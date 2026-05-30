import asyncio

import pytest

from estimator_king.bot.scheduler import CrawlScheduler
from estimator_king.config_schema import AppConfig, CrawlerPolicy, Store


@pytest.mark.asyncio
async def test_run_once_calls_cycle(monkeypatch):
    calls = []

    async def fake_cycle(config, db_path, embedder, vector_store, *, force_refetch=False):
        calls.append(db_path)
        return {"errors": 0}

    monkeypatch.setattr("estimator_king.bot.scheduler.run_crawl_cycle", fake_cycle)
    sched = CrawlScheduler(config=object(), db_path="db", embedder=object(), vector_store=object())

    await sched.run_once()

    assert calls == ["db"]


@pytest.mark.asyncio
async def test_run_once_is_reentrancy_guarded(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    count = 0

    async def fake_cycle(*a, **k):
        nonlocal count
        count += 1
        started.set()
        await release.wait()
        return {"errors": 0}

    monkeypatch.setattr("estimator_king.bot.scheduler.run_crawl_cycle", fake_cycle)
    sched = CrawlScheduler(config=object(), db_path="db", embedder=object(), vector_store=object())

    first = asyncio.create_task(sched.run_once())
    await started.wait()
    await sched.run_once()  # should be skipped (already running)
    release.set()
    await first

    assert count == 1


@pytest.mark.asyncio
async def test_run_once_swallows_cycle_errors(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("cycle failed")

    monkeypatch.setattr("estimator_king.bot.scheduler.run_crawl_cycle", boom)
    sched = CrawlScheduler(config=object(), db_path="db", embedder=object(), vector_store=object())

    await sched.run_once()  # must not raise


def _schedulable_config():
    return AppConfig(
        stores=[Store(id="hololive", base_url="https://x", sitemap_url="https://x/sm.xml")],
        crawler=CrawlerPolicy(),
    )


@pytest.mark.asyncio
async def test_run_forever_propagates_cancellation(monkeypatch):
    entered = asyncio.Event()

    async def fake_cycle(*a, **k):
        entered.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr("estimator_king.bot.scheduler.run_crawl_cycle", fake_cycle)
    sched = CrawlScheduler(
        config=_schedulable_config(), db_path="db", embedder=object(), vector_store=object()
    )

    task = asyncio.create_task(sched.run_forever())
    await entered.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert sched._running is False
