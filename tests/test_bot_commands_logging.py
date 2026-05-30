import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

from estimator_king.bot.commands import ProductInputModal
from estimator_king.llm.chat import EstimationError


def _interaction(user_id=123):
    interaction = MagicMock()
    interaction.user.id = user_id
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


class OkEstimator:
    def estimate_products(self, names, user_id):
        from estimator_king.llm.chat import EstimateBatch
        return EstimateBatch(estimates=[])


class BoomEstimator:
    def estimate_products(self, names, user_id):
        raise EstimationError("model refused")


class BoomRuntimeEstimator:
    def estimate_products(self, names, user_id):
        raise RuntimeError("boom")


def test_too_many_products_logs_warning(monkeypatch, caplog):
    monkeypatch.setattr(
        "estimator_king.bot.commands.parse_product_lines",
        lambda text: ["p"] * 11,
    )

    # discord.py 在 Modal 建構時呼叫 asyncio.get_running_loop()，故 modal 必須
    # 在 running loop 內建構，否則建構就拋 RuntimeError: no running event loop。
    async def _run():
        modal = ProductInputModal(OkEstimator())
        interaction = _interaction()
        with caplog.at_level(logging.DEBUG, logger="estimator_king.bot.commands"):
            await modal.on_submit(interaction)
        return interaction

    interaction = asyncio.run(_run())

    recs = [r for r in caplog.records if r.name == "estimator_king.bot.commands"]
    assert any(
        r.levelno == logging.WARNING and "exceeds max" in r.getMessage() for r in recs
    )
    interaction.response.send_message.assert_awaited()  # 既有行為保留


def test_estimation_error_logs_error(monkeypatch, caplog):
    monkeypatch.setattr(
        "estimator_king.bot.commands.parse_product_lines",
        lambda text: ["p"],
    )

    async def _run():
        modal = ProductInputModal(BoomEstimator())
        interaction = _interaction()
        with caplog.at_level(logging.DEBUG, logger="estimator_king.bot.commands"):
            await modal.on_submit(interaction)
        return interaction

    interaction = asyncio.run(_run())

    recs = [r for r in caplog.records if r.name == "estimator_king.bot.commands"]
    assert any(
        r.levelno == logging.ERROR and "estimation failed" in r.getMessage() for r in recs
    )
    interaction.followup.send.assert_awaited()  # 既有行為保留


def test_empty_input_logs_warning(monkeypatch, caplog):
    monkeypatch.setattr(
        "estimator_king.bot.commands.parse_product_lines",
        lambda text: [],
    )

    async def _run():
        modal = ProductInputModal(OkEstimator())
        interaction = _interaction()
        with caplog.at_level(logging.DEBUG, logger="estimator_king.bot.commands"):
            await modal.on_submit(interaction)
        return interaction

    interaction = asyncio.run(_run())

    recs = [r for r in caplog.records if r.name == "estimator_king.bot.commands"]
    assert any(
        r.levelno == logging.WARNING and "empty input" in r.getMessage() for r in recs
    )
    interaction.response.send_message.assert_awaited()


def test_unexpected_error_logs_exception(monkeypatch, caplog):
    monkeypatch.setattr(
        "estimator_king.bot.commands.parse_product_lines",
        lambda text: ["p"],
    )

    async def _run():
        modal = ProductInputModal(BoomRuntimeEstimator())
        interaction = _interaction()
        with caplog.at_level(logging.DEBUG, logger="estimator_king.bot.commands"):
            await modal.on_submit(interaction)
        return interaction

    interaction = asyncio.run(_run())

    recs = [r for r in caplog.records if r.name == "estimator_king.bot.commands"]
    assert any(
        r.levelno == logging.ERROR and "unexpected error handling request" in r.getMessage()
        for r in recs
    )
    interaction.followup.send.assert_awaited()
