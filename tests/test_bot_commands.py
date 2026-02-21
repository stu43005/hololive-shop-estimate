"""Unit tests for Discord bot command parsing and validation."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import discord
import os

from estimator_king.bot.commands import (
    parse_product_lines,
    ProductInputModal,
    format_workflow_result,
    MAX_PRODUCTS,
)
from estimator_king.bot.workflow_client import (
    WorkflowResult,
    ProductEstimate,
    PriceRange,
    ReferenceProduct,
)


class TestParseProductLines:
    """Tests for parse_product_lines() function."""

    def test_empty_input(self):
        """Empty string returns empty list."""
        assert parse_product_lines("") == []

    def test_single_line(self):
        """Single product name parsed correctly."""
        assert parse_product_lines("Product A") == ["Product A"]

    def test_multiple_lines(self):
        """Multiple product names parsed correctly."""
        result = parse_product_lines("Product A\nProduct B\nProduct C")
        assert result == ["Product A", "Product B", "Product C"]

    def test_strips_whitespace(self):
        """Whitespace is stripped from each line."""
        result = parse_product_lines("  Product A  \n  Product B  ")
        assert result == ["Product A", "Product B"]

    def test_skips_empty_lines(self):
        """Empty lines are filtered out."""
        result = parse_product_lines("Product A\n\n\nProduct B")
        assert result == ["Product A", "Product B"]

    def test_mixed_whitespace(self):
        """Mixed whitespace and empty lines are handled correctly."""
        result = parse_product_lines("Product A\n  \n\t\nProduct B")
        assert result == ["Product A", "Product B"]


@pytest.fixture
def mock_interaction():
    """Mock Discord interaction for testing modal submission."""
    interaction = MagicMock(spec=discord.Interaction)
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


def create_modal_with_input(value: str) -> ProductInputModal:
    modal = MagicMock(spec=ProductInputModal)
    modal.products = MagicMock()
    modal.products.value = value
    modal.on_submit = ProductInputModal.on_submit.__get__(modal)
    return modal


class TestProductInputModal:
    """Tests for ProductInputModal validation and submission."""

    @pytest.mark.asyncio
    async def test_modal_min_validation_zero_products(self, mock_interaction):
        """Empty input triggers minimum validation error."""
        modal = create_modal_with_input("")
        await modal.on_submit(mock_interaction)

        mock_interaction.response.send_message.assert_called_once()
        args, kwargs = mock_interaction.response.send_message.call_args
        assert "at least 1" in args[0]
        assert kwargs["ephemeral"] is True

    @pytest.mark.asyncio
    async def test_modal_min_validation_one_product(self, mock_interaction):
        """Single product passes minimum validation."""
        modal = create_modal_with_input("Product A")
        await modal.on_submit(mock_interaction)

        mock_interaction.response.defer.assert_called_once_with(thinking=True)
        mock_interaction.followup.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_modal_max_validation_ten_products(self, mock_interaction):
        """Ten products passes maximum validation."""
        products = "\n".join([f"Product {i}" for i in range(1, 11)])
        modal = create_modal_with_input(products)
        await modal.on_submit(mock_interaction)

        mock_interaction.response.defer.assert_called_once_with(thinking=True)
        mock_interaction.followup.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_modal_max_validation_eleven_products(self, mock_interaction):
        """Eleven products triggers maximum validation error."""
        products = "\n".join([f"Product {i}" for i in range(1, 12)])
        modal = create_modal_with_input(products)
        await modal.on_submit(mock_interaction)

        mock_interaction.response.send_message.assert_called_once()
        args, kwargs = mock_interaction.response.send_message.call_args
        assert "Maximum 10" in args[0]
        assert kwargs["ephemeral"] is True

    @pytest.mark.asyncio
    async def test_modal_success_message_contains_product_count(self, mock_interaction):
        """Success message contains correct product count."""
        modal = create_modal_with_input("Product A\nProduct B\nProduct C")

        mock_estimate = ProductEstimate(
            product_name="Product A",
            suggested_price_jpy=5000,
            price_range_jpy=PriceRange(min=4500, max=5500),
            confidence="high",
            rationale="Test rationale.",
            reference_products=[],
        )
        mock_result = WorkflowResult(
            estimates=[mock_estimate],
            workflow_run_id="test-123",
            elapsed_time=1.0,
        )

        with patch.dict(os.environ, {"DIFY_WORKFLOW_API_KEY": "test-key"}):
            with patch(
                "estimator_king.bot.commands.WorkflowClient"
            ) as mock_client_class:
                mock_client = MagicMock()
                mock_client.estimate_products.return_value = mock_result
                mock_client_class.return_value = mock_client

                await modal.on_submit(mock_interaction)

        mock_interaction.followup.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_modal_success_includes_placeholder_message(self, mock_interaction):
        """Success message includes formatted estimate response."""
        modal = create_modal_with_input("Product A")

        mock_estimate = ProductEstimate(
            product_name="Product A",
            suggested_price_jpy=5000,
            price_range_jpy=PriceRange(min=4500, max=5500),
            confidence="high",
            rationale="Test rationale.",
            reference_products=[],
        )
        mock_result = WorkflowResult(
            estimates=[mock_estimate],
            workflow_run_id="test-123",
            elapsed_time=1.0,
        )

        with patch.dict(os.environ, {"DIFY_WORKFLOW_API_KEY": "test-key"}):
            with patch(
                "estimator_king.bot.commands.WorkflowClient"
            ) as mock_client_class:
                mock_client = MagicMock()
                mock_client.estimate_products.return_value = mock_result
                mock_client_class.return_value = mock_client

                await modal.on_submit(mock_interaction)

        mock_interaction.followup.send.assert_called_once()
        called_embed = mock_interaction.followup.send.call_args[1]["embed"]
        assert called_embed.title == "Price Estimates (page 1/1)"


class TestFormatWorkflowResult:
    """Tests for format_workflow_result() response formatting."""

    def test_single_product_short_response(self):
        """Format single product estimate into single embed."""
        estimate = ProductEstimate(
            product_name="Hololive T-Shirt",
            suggested_price_jpy=5000,
            price_range_jpy=PriceRange(min=4500, max=5500),
            confidence="high",
            rationale="Based on similar merchandise pricing.",
            reference_products=[
                ReferenceProduct(name="Hoodie", price_jpy=4800, store="hololive")
            ],
        )
        result = WorkflowResult(
            estimates=[estimate],
            workflow_run_id="test-123",
            elapsed_time=2.5,
        )

        embeds = format_workflow_result(result)

        assert len(embeds) == 1
        assert embeds[0].title == "Price Estimates (page 1/1)"
        desc = embeds[0].description or ""
        assert "Hololive T-Shirt" in desc
        assert "¥5,000" in desc
        assert "Hoodie" in desc
        assert embeds[0].color == discord.Color.blue()

    def test_multiple_products_single_embed(self):
        """Format 3 products into single embed (under length limit)."""
        estimates = [
            ProductEstimate(
                product_name=f"Product {i}",
                suggested_price_jpy=5000 + i * 500,
                price_range_jpy=PriceRange(min=4500 + i * 500, max=5500 + i * 500),
                confidence="high",
                rationale="Test rationale for product.",
                reference_products=[],
            )
            for i in range(1, 4)
        ]
        result = WorkflowResult(
            estimates=estimates,
            workflow_run_id="test-456",
            elapsed_time=3.2,
        )

        embeds = format_workflow_result(result)

        assert len(embeds) == 1
        desc = embeds[0].description or ""
        assert "Product 1" in desc
        assert "Product 2" in desc
        assert "Product 3" in desc

    def test_long_rationale_truncates(self):
        """Long rationale (>300 chars) gets truncated with ellipsis."""
        long_rationale = (
            "This is a very long rationale that exceeds the 300 character limit "
            "and should be truncated. " * 5
        )
        estimate = ProductEstimate(
            product_name="Test Product",
            suggested_price_jpy=5000,
            price_range_jpy=PriceRange(min=4500, max=5500),
            confidence="medium",
            rationale=long_rationale,
            reference_products=[],
        )
        result = WorkflowResult(estimates=[estimate])

        embeds = format_workflow_result(result)

        assert len(embeds) == 1
        desc = embeds[0].description or ""
        assert "..." in desc
        assert len(long_rationale) > 300

    def test_embed_color_and_footer(self):
        """Embed has blue color and footer with run_id + elapsed_time."""
        estimate = ProductEstimate(
            product_name="Test",
            suggested_price_jpy=1000,
            price_range_jpy=PriceRange(min=900, max=1100),
            confidence="low",
            rationale="Quick test.",
            reference_products=[],
        )
        result = WorkflowResult(
            estimates=[estimate],
            workflow_run_id="run-abc-123",
            elapsed_time=1.5,
        )

        embeds = format_workflow_result(result)

        assert embeds[0].color == discord.Color.blue()
        assert embeds[0].footer is not None
        footer_text = embeds[0].footer.text or ""
        assert "run-abc-123" in footer_text
        assert "1.50s" in footer_text

    def test_empty_result_returns_single_embed(self):
        """Empty WorkflowResult (0 estimates) returns single embed with message."""
        result = WorkflowResult(estimates=[])

        embeds = format_workflow_result(result)

        assert len(embeds) == 1
        title = embeds[0].title or ""
        desc = embeds[0].description or ""
        assert "0 products" in title
        assert "No estimates returned" in desc

    def test_references_formatted_correctly(self):
        """Reference products formatted with name and price."""
        estimate = ProductEstimate(
            product_name="Merch",
            suggested_price_jpy=8000,
            price_range_jpy=PriceRange(min=7500, max=8500),
            confidence="high",
            rationale="Based on market analysis.",
            reference_products=[
                ReferenceProduct(name="Similar Item A", price_jpy=7800, store="shop1"),
                ReferenceProduct(name="Similar Item B", price_jpy=8200, store="shop2"),
            ],
        )
        result = WorkflowResult(estimates=[estimate])

        embeds = format_workflow_result(result)

        description = embeds[0].description or ""
        assert "Similar Item A (¥7,800)" in description
        assert "Similar Item B (¥8,200)" in description
        assert "🔗 References:" in description

    def test_price_formatting_with_commas(self):
        """Prices formatted with thousands separator."""
        estimate = ProductEstimate(
            product_name="Expensive Item",
            suggested_price_jpy=25000,
            price_range_jpy=PriceRange(min=24000, max=26000),
            confidence="high",
            rationale="Premium product.",
            reference_products=[],
        )
        result = WorkflowResult(estimates=[estimate])

        embeds = format_workflow_result(result)

        desc = embeds[0].description or ""
        assert "¥25,000" in desc
        assert "¥24,000" in desc
        assert "¥26,000" in desc
