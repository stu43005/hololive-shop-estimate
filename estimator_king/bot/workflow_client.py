"""Dify Workflow API client for Discord bot price estimation.

Provides dataclasses for workflow responses and a client class for calling
the Dify Workflow API with chunking support.
"""

from dataclasses import dataclass
from typing import Optional
import requests


@dataclass
class PriceRange:
    """Price range indicating estimation uncertainty.

    Attributes:
        min: Minimum price in JPY
        max: Maximum price in JPY
    """

    min: int
    max: int


@dataclass
class ReferenceProduct:
    """Reference product retrieved from Knowledge Base.

    Attributes:
        name: Product name
        price_jpy: Actual price in JPY
        store: Store identifier (hololive/vspo)
    """

    name: str
    price_jpy: int
    store: str


@dataclass
class ProductEstimate:
    """Price estimate for a single product.

    Attributes:
        product_name: Name of the product being estimated
        suggested_price_jpy: Primary estimate in JPY
        price_range_jpy: Range indicating uncertainty
        confidence: Estimate quality (high/medium/low)
        rationale: Explanation for the estimate (2-3 sentences)
        reference_products: Evidence items from Knowledge Base
    """

    product_name: str
    suggested_price_jpy: int
    price_range_jpy: PriceRange
    confidence: str
    rationale: str
    reference_products: list[ReferenceProduct]


@dataclass
class WorkflowResult:
    """Aggregated result from workflow API call(s).

    Attributes:
        estimates: List of product estimates (may be from multiple chunks)
        workflow_run_id: Last workflow run ID (optional)
        elapsed_time: Total elapsed time for all chunks in seconds (optional)
    """

    estimates: list[ProductEstimate]
    workflow_run_id: Optional[str] = None
    elapsed_time: Optional[float] = None


# Module-level constants
DEFAULT_BASE_URL = "https://dify.long-cod.ts.net/v1"
DEFAULT_TIMEOUT = 30  # seconds
WORKFLOW_ENDPOINT = "/workflows/run"
CHUNK_SIZE = 10  # Maximum products per workflow request (Cloudflare timeout mitigation)


class WorkflowClient:
    """Client for Dify Workflow API price estimation.

    Manages requests to the Dify Workflow API for product price estimates.
    Handles authentication, request building, and response parsing.

    Attributes:
        api_key: Bearer token for authentication (app-{uuid} format)
        base_url: Base URL of Dify Workflow API
        timeout: Request timeout in seconds
        session: requests.Session with configured headers
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        """Initialize Workflow API client.

        Args:
            api_key: Dify Workflow API key
            base_url: Base URL for Dify API (default: https://dify.long-cod.ts.net/v1)
            timeout: Request timeout in seconds (default: 30)

        Raises:
            ValueError: If api_key is empty
        """
        if not api_key:
            raise ValueError("api_key cannot be empty")

        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

        # Create session with headers
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        )

    def _call_workflow(self, product_names: list[str], user_id: str) -> WorkflowResult:
        """Call workflow API with list of products (single request).

        Args:
            product_names: List of product names (1-10 items)
            user_id: Discord user ID for rate limiting (format: "discord-{snowflake}")

        Returns:
            WorkflowResult with estimates from API response

        Raises:
            requests.Timeout: If request exceeds timeout
            requests.HTTPError: If HTTP status >= 400
            ValueError: If response JSON is malformed
        """
        url = f"{self.base_url}{WORKFLOW_ENDPOINT}"

        payload = {
            "inputs": {
                "query": "\n".join(product_names),
            },
            "response_mode": "blocking",
            "user": user_id,
        }

        try:
            response = self.session.post(url, json=payload, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            return self._parse_response(data)
        except requests.Timeout as e:
            raise requests.Timeout(f"Workflow API timeout after {self.timeout}s") from e
        except requests.HTTPError as e:
            raise requests.HTTPError(
                f"Workflow API error: {e.response.status_code}"
            ) from e
        except (KeyError, TypeError) as e:
            raise ValueError(f"Malformed workflow response: {e}") from e

    def _parse_response(self, data: dict) -> WorkflowResult:
        """Parse workflow API response to WorkflowResult.

        Args:
            data: Raw JSON response from API

        Returns:
            WorkflowResult with parsed estimates

        Raises:
            ValueError: If required fields missing or malformed
        """
        try:
            # Extract estimates array
            estimates_data = data["data"]["outputs"]["estimates"]

            # Parse each estimate into ProductEstimate dataclass
            estimates = []
            for estimate in estimates_data:
                # Parse price range
                price_range = PriceRange(
                    min=estimate["price_range_jpy"]["min"],
                    max=estimate["price_range_jpy"]["max"],
                )

                # Parse reference products
                reference_products = [
                    ReferenceProduct(
                        name=ref["name"],
                        price_jpy=ref["price_jpy"],
                        store=ref["store"],
                    )
                    for ref in estimate.get("reference_products", [])
                ]

                # Create ProductEstimate
                product_estimate = ProductEstimate(
                    product_name=estimate["product_name"],
                    suggested_price_jpy=estimate["suggested_price_jpy"],
                    price_range_jpy=price_range,
                    confidence=estimate["confidence"],
                    rationale=estimate["rationale"],
                    reference_products=reference_products,
                )
                estimates.append(product_estimate)

            # Extract optional fields
            workflow_run_id = data.get("workflow_run_id")
            elapsed_time = data.get("elapsed_time")

            return WorkflowResult(
                estimates=estimates,
                workflow_run_id=workflow_run_id,
                elapsed_time=elapsed_time,
            )
        except (KeyError, TypeError) as e:
            raise ValueError(f"Malformed workflow response: {e}") from e

    def estimate_products(
        self, product_names: list[str], user_id: str
    ) -> WorkflowResult:
        """Estimate prices for multiple products with automatic chunking.

        Splits large product lists into chunks of CHUNK_SIZE to avoid API timeouts.
        Calls workflow API once per chunk and aggregates results.

        Args:
            product_names: List of product names (unlimited, will be chunked)
            user_id: Discord user ID for rate limiting (format: "discord-{snowflake}")

        Returns:
            WorkflowResult with all estimates aggregated across chunks

        Raises:
            requests.Timeout: If any chunk request exceeds timeout
            requests.HTTPError: If any chunk returns HTTP error
            ValueError: If any chunk response is malformed
        """
        if not product_names:
            return WorkflowResult(estimates=[], workflow_run_id=None, elapsed_time=None)

        # Split into chunks
        chunks = [
            product_names[i : i + CHUNK_SIZE]
            for i in range(0, len(product_names), CHUNK_SIZE)
        ]

        all_estimates = []
        total_elapsed = 0.0
        last_run_id = None

        # Process each chunk
        for chunk in chunks:
            result = self._call_workflow(chunk, user_id)
            all_estimates.extend(result.estimates)
            if result.elapsed_time:
                total_elapsed += result.elapsed_time
            if result.workflow_run_id:
                last_run_id = result.workflow_run_id

        return WorkflowResult(
            estimates=all_estimates,
            workflow_run_id=last_run_id,
            elapsed_time=total_elapsed if total_elapsed > 0 else None,
        )
