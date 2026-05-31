import asyncio

import pytest

from estimator_king import runtime as runner


class _FakeBot:
    def __init__(self):
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_shutdown_cancels_scheduler_then_closes_bot():
    async def long_running():
        await asyncio.sleep(3600)

    scheduler_task = asyncio.create_task(long_running())
    bot = _FakeBot()
    shutdowner = runner._Shutdowner(scheduler_task, bot)

    await shutdowner.shutdown()

    assert scheduler_task.cancelled()
    assert bot.closed is True


@pytest.mark.asyncio
async def test_first_signal_requests_shutdown_second_forces_exit():
    async def long_running():
        await asyncio.sleep(3600)

    scheduler_task = asyncio.create_task(long_running())
    bot = _FakeBot()
    exits: list[int] = []
    shutdowner = runner._Shutdowner(scheduler_task, bot, force_exit=exits.append)

    # First signal: schedules graceful shutdown, no force exit.
    shutdowner.handle_signal()
    assert shutdowner._requested is True
    assert exits == []

    # Second signal: forces exit with code 130.
    shutdowner.handle_signal()
    assert exits == [130]

    # 讓第一次 signal 建立的 graceful-shutdown task 收尾，
    # 並 drain 背景 task 集合，避免洩漏到全域 set。
    await asyncio.sleep(0)
    for t in list(runner._background_tasks):
        await asyncio.gather(t, return_exceptions=True)
