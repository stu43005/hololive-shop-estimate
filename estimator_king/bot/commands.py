"""Discord bot slash command implementation for product price estimation."""

import discord
from discord import app_commands
from discord.ui import Modal, TextInput
import requests

from estimator_king.config_schema import AppConfig
from estimator_king.bot.workflow_client import WorkflowClient, WorkflowResult

# Constants for input validation
MAX_PRODUCTS = 10
MAX_INPUT_LENGTH = 2000


def parse_product_lines(text: str) -> list[str]:
    """Parse multi-line product input into list of product names.

    Splits input by newlines, strips whitespace from each line, and filters
    out empty lines.

    Args:
        text: Raw multi-line input from Discord modal

    Returns:
        List of non-empty product names (whitespace stripped)
    """
    lines = text.split("\n")
    return [line.strip() for line in lines if line.strip()]


def format_workflow_result(
    result: WorkflowResult, max_length: int = 2000
) -> list[discord.Embed]:
    """Format WorkflowResult into Discord embeds with length limits.

    Converts a WorkflowResult from the API into Discord embed objects,
    handling length limits by splitting into multiple embeds if needed.

    Args:
        result: WorkflowResult from WorkflowClient
        max_length: Maximum characters per embed description (default: 2000)

    Returns:
        List of discord.Embed objects (may be multiple if content exceeds limit)
    """
    if not result.estimates:
        embed = discord.Embed(
            title="Price Estimates (0 products)",
            description="No estimates returned from the workflow.",
            color=discord.Color.blue(),
        )
        if result.workflow_run_id and result.elapsed_time is not None:
            embed.set_footer(
                text=f"Run ID: {result.workflow_run_id} | {result.elapsed_time:.2f}s"
            )
        return [embed]

    formatted_products = []
    for estimate in result.estimates:
        rationale = estimate.rationale
        if len(rationale) > 300:
            rationale = rationale[:297] + "..."

        refs_text = ""
        if estimate.reference_products:
            refs_list = [
                f"{ref.name} (¥{ref.price_jpy:,})"
                for ref in estimate.reference_products
            ]
            refs_text = ", ".join(refs_list)
            refs_text = f"🔗 References: {refs_text}"

        product_block = (
            f"**{estimate.product_name}**\n"
            f"💰 Suggested Price: ¥{estimate.suggested_price_jpy:,}\n"
            f"📊 Range: ¥{estimate.price_range_jpy.min:,} - ¥{estimate.price_range_jpy.max:,} "
            f"(confidence: {estimate.confidence})\n"
            f"📝 Rationale: {rationale}\n"
        )
        if refs_text:
            product_block += f"{refs_text}\n"
        product_block += "\n---\n\n"

        formatted_products.append(product_block)

    full_content = "".join(formatted_products)

    embeds = []
    total_products = len(result.estimates)
    current_content = ""
    page_num = 1

    for product_block in formatted_products:
        test_content = current_content + product_block
        if len(test_content) > max_length and current_content:
            embed = discord.Embed(
                title=f"Price Estimates (page {page_num}/{1 if len(full_content) <= max_length else 2})",
                description=current_content.rstrip("\n---\n\n"),
                color=discord.Color.blue(),
            )
            if (
                page_num == 1
                and result.workflow_run_id
                and result.elapsed_time is not None
            ):
                embed.set_footer(
                    text=f"Run ID: {result.workflow_run_id} | {result.elapsed_time:.2f}s"
                )
            embeds.append(embed)
            current_content = product_block
            page_num += 1
        else:
            current_content = test_content

    if current_content:
        embed = discord.Embed(
            title=f"Price Estimates (page {page_num}/{page_num})",
            description=current_content.rstrip("\n---\n\n"),
            color=discord.Color.blue(),
        )
        if page_num == 1 and result.workflow_run_id and result.elapsed_time is not None:
            embed.set_footer(
                text=f"Run ID: {result.workflow_run_id} | {result.elapsed_time:.2f}s"
            )
        embeds.append(embed)

    return embeds


class ProductInputModal(Modal, title="Enter Product Names"):
    """Modal dialog for collecting product names from user.

    Provides a paragraph-style text input for users to enter multiple product
    names (one per line) for price estimation.
    """

    products = TextInput(
        label="Products (one per line)",
        style=discord.TextStyle.paragraph,
        max_length=MAX_INPUT_LENGTH,
        required=True,
        placeholder="Example:\nHololive T-Shirt\nFigure Set\nLimited Edition Merch",
    )

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self._config = config

    async def on_submit(self, interaction: discord.Interaction) -> None:
        """Handle modal submission with validation and processing.

        Parses the input, validates product count, defers the response for
        processing, and sends a placeholder message indicating that workflow
        integration is coming.

        Args:
            interaction: Discord interaction object from modal submission
        """
        # Parse product lines from user input
        product_list = parse_product_lines(self.products.value)

        # Validation: minimum 1 product
        if len(product_list) < 1:
            await interaction.response.send_message(
                "❌ Please enter at least 1 product name", ephemeral=True
            )
            return

        # Validation: maximum 10 products
        if len(product_list) > MAX_PRODUCTS:
            await interaction.response.send_message(
                f"❌ Maximum {MAX_PRODUCTS} products allowed", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)

        api_key = self._config.dify_workflow_api_key
        if not api_key:
            await interaction.followup.send(
                "❌ Bot configuration error: Missing DIFY_WORKFLOW_API_KEY"
            )
            return

        try:
            base_url = self._config.dify_workflow_base_url
            if base_url:
                client = WorkflowClient(api_key=api_key, base_url=base_url)
            else:
                client = WorkflowClient(api_key=api_key)
            user_id = f"discord-{interaction.user.id}"

            result = client.estimate_products(product_list, user_id)

            embeds = format_workflow_result(result)
            for embed in embeds:
                await interaction.followup.send(embed=embed)

        except requests.Timeout:
            await interaction.followup.send(
                "❌ Request timed out. Please try again with fewer products."
            )
        except requests.HTTPError as e:
            await interaction.followup.send(
                f"❌ API error: {e}. Please try again later."
            )
        except ValueError as e:
            await interaction.followup.send(f"❌ Invalid response from API: {e}")
        except Exception as e:
            await interaction.followup.send(f"❌ Unexpected error: {e}")


def setup_commands(bot: discord.Client, config: AppConfig) -> app_commands.CommandTree:
    """Register slash commands with the bot.

    Creates and registers the /estimate command for collecting product names
    and initiating price estimation.

    Args:
        bot: Discord bot client instance
        config: Application configuration (provides workflow API credentials)

    Returns:
        CommandTree with registered commands
    """
    tree = app_commands.CommandTree(bot)

    @tree.command(
        name="estimate", description="Estimate product prices from Shopify stores"
    )
    async def estimate(interaction: discord.Interaction) -> None:
        """Slash command handler for /estimate.

        Displays the product input modal when user invokes /estimate command.

        Args:
            interaction: Discord interaction object from slash command invocation
        """
        await interaction.response.send_modal(ProductInputModal(config))

    return tree
