"""Unit tests for Discord bot command parsing and formatting."""

import discord

from estimator_king.bot.commands import format_estimates, parse_product_lines
from estimator_king.llm.chat import EstimateBatch, PriceRange, ProductEstimate, ReferenceProduct


def _batch(n=1):
    return EstimateBatch(estimates=[
        ProductEstimate(
            product_name=f"p{i}", suggested_price_jpy=2000,
            price_range_jpy=PriceRange(min=1800, max=2200), confidence="high",
            rationale="r", reference_products=[ReferenceProduct(name="ref", price_jpy=2000, store="hololive")],
        ) for i in range(n)
    ])


def test_parse_product_lines_strips_and_filters():
    assert parse_product_lines(" a \n\n b \n") == ["a", "b"]


def test_format_estimates_returns_embeds_with_prices():
    embeds = format_estimates(_batch(1))
    assert embeds and isinstance(embeds[0], discord.Embed)
    assert "¥2,000" in embeds[0].description


def test_format_estimates_empty_batch():
    embeds = format_estimates(EstimateBatch(estimates=[]))
    assert len(embeds) == 1
    assert "0 products" in embeds[0].title


def test_modal_uses_injected_estimator():
    import asyncio
    from estimator_king.bot.commands import ProductInputModal

    class FakeEstimator:
        def estimate_products(self, names, user_id):
            from estimator_king.llm.chat import EstimateBatch
            return EstimateBatch(estimates=[])

    async def _make_modal():
        return ProductInputModal(FakeEstimator())

    modal = asyncio.run(_make_modal())
    assert modal._estimator is not None


def _est(name, rationale="r"):
    return ProductEstimate(
        product_name=name, suggested_price_jpy=100,
        price_range_jpy=PriceRange(min=100, max=100), confidence="high",
        rationale=rationale, reference_products=[])


def test_page_denominator_consistent_across_pages():
    batch = EstimateBatch(estimates=[_est(f"item {i}", rationale="x" * 250) for i in range(12)])
    embeds = format_estimates(batch, max_length=400)
    total = len(embeds)
    assert total >= 3
    for i, embed in enumerate(embeds, start=1):
        assert f"page {i}/{total}" in embed.title


def test_trailing_dash_in_rationale_not_stripped():
    batch = EstimateBatch(estimates=[_est("solo", rationale="ends with dash -")])
    embeds = format_estimates(batch, max_length=2000)
    assert "ends with dash -" in embeds[0].description
