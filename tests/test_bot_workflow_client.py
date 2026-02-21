"""Tests for WorkflowClient class with API mocking and chunking validation."""

import json
import pytest
import responses
import requests

from estimator_king.bot.workflow_client import (
    WorkflowClient,
    WorkflowResult,
    ProductEstimate,
    PriceRange,
    ReferenceProduct,
)


def create_mock_response(
    product_names, workflow_run_id="test-run-123", elapsed_time=2.5
):
    """Create a mock Dify workflow API response."""
    estimates = []
    for i, product_name in enumerate(product_names):
        estimates.append(
            {
                "product_name": product_name,
                "suggested_price_jpy": 5000 + (i * 100),
                "price_range_jpy": {
                    "min": 4500 + (i * 100),
                    "max": 5500 + (i * 100),
                },
                "confidence": "high",
                "rationale": f"Based on similar products like {product_name}",
                "reference_products": [
                    {
                        "name": f"Reference {product_name}",
                        "price_jpy": 4800 + (i * 100),
                        "store": "hololive",
                    }
                ],
            }
        )

    return {
        "data": {
            "outputs": {
                "estimates": estimates,
            }
        },
        "workflow_run_id": workflow_run_id,
        "elapsed_time": elapsed_time,
    }


class TestWorkflowClientInit:
    """Tests for WorkflowClient initialization."""

    def test_valid_api_key(self):
        """Initialize with valid API key sets attributes correctly."""
        client = WorkflowClient(api_key="app-test-key-123")

        assert client.api_key == "app-test-key-123"
        assert client.base_url == "https://dify.long-cod.ts.net/v1"
        assert client.timeout == 30

    def test_custom_base_url_and_timeout(self):
        """Initialize with custom base_url and timeout."""
        client = WorkflowClient(
            api_key="app-key",
            base_url="https://custom.dify.io/v1",
            timeout=60,
        )

        assert client.base_url == "https://custom.dify.io/v1"
        assert client.timeout == 60

    def test_base_url_trailing_slash_removed(self):
        """Trailing slash is stripped from base_url."""
        client = WorkflowClient(
            api_key="app-key",
            base_url="https://custom.dify.io/v1/",
        )

        assert client.base_url == "https://custom.dify.io/v1"

    def test_empty_api_key_raises_value_error(self):
        """Empty string api_key raises ValueError."""
        with pytest.raises(ValueError, match="api_key cannot be empty"):
            WorkflowClient(api_key="")

    def test_session_headers_configured(self):
        """Session is configured with correct authorization headers."""
        api_key = "app-test-key-456"
        client = WorkflowClient(api_key=api_key)

        assert client.session.headers["Authorization"] == f"Bearer {api_key}"
        assert client.session.headers["Content-Type"] == "application/json"


class TestCallWorkflow:
    """Tests for _call_workflow method."""

    @responses.activate
    def test_single_product_success(self):
        """Single product request returns WorkflowResult with one estimate."""
        mock_response = create_mock_response(["Product A"])
        responses.add(
            responses.POST,
            "https://dify.long-cod.ts.net/v1/workflows/run",
            json=mock_response,
            status=200,
        )

        client = WorkflowClient(api_key="app-test-key")
        result = client._call_workflow(["Product A"], "discord-123456")

        assert len(result.estimates) == 1
        assert result.estimates[0].product_name == "Product A"
        assert result.estimates[0].suggested_price_jpy == 5000
        assert result.workflow_run_id == "test-run-123"
        assert result.elapsed_time == 2.5

    @responses.activate
    def test_multiple_products_success(self):
        """Multiple products in single request returns multiple estimates."""
        mock_response = create_mock_response(["Product A", "Product B", "Product C"])
        responses.add(
            responses.POST,
            "https://dify.long-cod.ts.net/v1/workflows/run",
            json=mock_response,
            status=200,
        )

        client = WorkflowClient(api_key="app-test-key")
        result = client._call_workflow(
            ["Product A", "Product B", "Product C"], "discord-123456"
        )

        assert len(result.estimates) == 3
        assert result.estimates[0].product_name == "Product A"
        assert result.estimates[1].product_name == "Product B"
        assert result.estimates[2].product_name == "Product C"

    @responses.activate
    def test_payload_format_correct(self):
        """Request payload has correct structure."""
        mock_response = create_mock_response(["Product A"])
        responses.add(
            responses.POST,
            "https://dify.long-cod.ts.net/v1/workflows/run",
            json=mock_response,
            status=200,
        )

        client = WorkflowClient(api_key="app-test-key")
        client._call_workflow(["Product A", "Product B"], "discord-123456")

        request = responses.calls[0].request
        payload = json.loads(request.body)
        assert payload["inputs"]["query"] == "Product A\nProduct B"
        assert payload["response_mode"] == "blocking"
        assert payload["user"] == "discord-123456"

    @responses.activate
    def test_authorization_header_sent(self):
        """Authorization header is sent with Bearer token."""
        api_key = "app-specific-key-789"
        mock_response = create_mock_response(["Product A"])
        responses.add(
            responses.POST,
            "https://dify.long-cod.ts.net/v1/workflows/run",
            json=mock_response,
            status=200,
        )

        client = WorkflowClient(api_key=api_key)
        client._call_workflow(["Product A"], "discord-123456")

        request = responses.calls[0].request
        assert request.headers["Authorization"] == f"Bearer {api_key}"

    @responses.activate
    def test_timeout_raises_requests_timeout(self):
        """Request timeout raises requests.Timeout."""
        responses.add(
            responses.POST,
            "https://dify.long-cod.ts.net/v1/workflows/run",
            body=requests.Timeout("Connection timeout"),
        )

        client = WorkflowClient(api_key="app-test-key", timeout=5)

        with pytest.raises(requests.Timeout):
            client._call_workflow(["Product A"], "discord-123456")

    @responses.activate
    def test_http_error_raises_http_error(self):
        """HTTP error (400+) raises requests.HTTPError."""
        responses.add(
            responses.POST,
            "https://dify.long-cod.ts.net/v1/workflows/run",
            json={"error": "Unauthorized"},
            status=401,
        )

        client = WorkflowClient(api_key="app-invalid-key")

        with pytest.raises(requests.HTTPError):
            client._call_workflow(["Product A"], "discord-123456")

    @responses.activate
    def test_malformed_json_raises_value_error(self):
        """Missing required fields in response raises ValueError."""
        responses.add(
            responses.POST,
            "https://dify.long-cod.ts.net/v1/workflows/run",
            json={"wrong_field": "data"},
            status=200,
        )

        client = WorkflowClient(api_key="app-test-key")

        with pytest.raises(ValueError, match="Malformed workflow response"):
            client._call_workflow(["Product A"], "discord-123456")


class TestParseResponse:
    """Tests for _parse_response method."""

    def test_valid_json_parsing(self):
        """Valid response JSON is parsed into WorkflowResult."""
        mock_data = create_mock_response(["Product A", "Product B"])

        client = WorkflowClient(api_key="app-test-key")
        result = client._parse_response(mock_data)

        assert isinstance(result, WorkflowResult)
        assert len(result.estimates) == 2
        assert result.workflow_run_id == "test-run-123"
        assert result.elapsed_time == 2.5

    def test_parsed_estimates_are_dataclass_objects(self):
        """Parsed estimates are ProductEstimate dataclass instances."""
        mock_data = create_mock_response(["Product A"])

        client = WorkflowClient(api_key="app-test-key")
        result = client._parse_response(mock_data)

        assert isinstance(result.estimates[0], ProductEstimate)
        assert result.estimates[0].product_name == "Product A"
        assert result.estimates[0].suggested_price_jpy == 5000

    def test_price_range_is_dataclass(self):
        """Price range is parsed into PriceRange dataclass."""
        mock_data = create_mock_response(["Product A"])

        client = WorkflowClient(api_key="app-test-key")
        result = client._parse_response(mock_data)

        price_range = result.estimates[0].price_range_jpy
        assert isinstance(price_range, PriceRange)
        assert price_range.min == 4500
        assert price_range.max == 5500

    def test_reference_products_parsed(self):
        """Reference products are parsed into ReferenceProduct objects."""
        mock_data = create_mock_response(["Product A"])

        client = WorkflowClient(api_key="app-test-key")
        result = client._parse_response(mock_data)

        ref_products = result.estimates[0].reference_products
        assert len(ref_products) == 1
        assert isinstance(ref_products[0], ReferenceProduct)
        assert ref_products[0].name == "Reference Product A"
        assert ref_products[0].price_jpy == 4800
        assert ref_products[0].store == "hololive"

    def test_missing_data_key_raises_value_error(self):
        """Missing 'data' key raises ValueError."""
        invalid_response = {"workflow_run_id": "123"}

        client = WorkflowClient(api_key="app-test-key")

        with pytest.raises(ValueError, match="Malformed workflow response"):
            client._parse_response(invalid_response)

    def test_missing_outputs_key_raises_value_error(self):
        """Missing 'outputs' key raises ValueError."""
        invalid_response = {"data": {"wrong_key": {}}}

        client = WorkflowClient(api_key="app-test-key")

        with pytest.raises(ValueError, match="Malformed workflow response"):
            client._parse_response(invalid_response)

    def test_missing_estimates_list_raises_value_error(self):
        """Missing 'estimates' array raises ValueError."""
        invalid_response = {"data": {"outputs": {"other_key": []}}}

        client = WorkflowClient(api_key="app-test-key")

        with pytest.raises(ValueError, match="Malformed workflow response"):
            client._parse_response(invalid_response)

    def test_empty_reference_products_allowed(self):
        """Estimate with empty reference_products list is valid."""
        mock_data = create_mock_response(["Product A"])
        mock_data["data"]["outputs"]["estimates"][0]["reference_products"] = []

        client = WorkflowClient(api_key="app-test-key")
        result = client._parse_response(mock_data)

        assert result.estimates[0].reference_products == []

    def test_optional_workflow_run_id_none(self):
        """Missing workflow_run_id is set to None."""
        mock_data = create_mock_response(["Product A"])
        del mock_data["workflow_run_id"]

        client = WorkflowClient(api_key="app-test-key")
        result = client._parse_response(mock_data)

        assert result.workflow_run_id is None

    def test_optional_elapsed_time_none(self):
        """Missing elapsed_time is set to None."""
        mock_data = create_mock_response(["Product A"])
        del mock_data["elapsed_time"]

        client = WorkflowClient(api_key="app-test-key")
        result = client._parse_response(mock_data)

        assert result.elapsed_time is None


class TestEstimateProducts:
    """Tests for estimate_products method with chunking."""

    def test_empty_product_list_returns_empty_result(self):
        """Empty product list returns WorkflowResult with no estimates."""
        client = WorkflowClient(api_key="app-test-key")
        result = client.estimate_products([], "discord-123456")

        assert result.estimates == []
        assert result.workflow_run_id is None
        assert result.elapsed_time is None

    @responses.activate
    def test_single_product_one_api_call(self):
        """Single product makes exactly one API call."""
        mock_response = create_mock_response(["Product A"], elapsed_time=1.5)
        responses.add(
            responses.POST,
            "https://dify.long-cod.ts.net/v1/workflows/run",
            json=mock_response,
            status=200,
        )

        client = WorkflowClient(api_key="app-test-key")
        result = client.estimate_products(["Product A"], "discord-123456")

        assert len(responses.calls) == 1
        assert len(result.estimates) == 1
        assert result.estimates[0].product_name == "Product A"

    @responses.activate
    def test_ten_products_one_api_call(self):
        """Exactly 10 products makes one API call (CHUNK_SIZE boundary)."""
        products = [f"Product {i}" for i in range(1, 11)]
        mock_response = create_mock_response(products)
        responses.add(
            responses.POST,
            "https://dify.long-cod.ts.net/v1/workflows/run",
            json=mock_response,
            status=200,
        )

        client = WorkflowClient(api_key="app-test-key")
        result = client.estimate_products(products, "discord-123456")

        assert len(responses.calls) == 1
        assert len(result.estimates) == 10

    @responses.activate
    def test_eleven_products_two_api_calls(self):
        """11 products causes chunking into 2 API calls."""
        products = [f"Product {i}" for i in range(1, 12)]

        chunk1_response = create_mock_response(
            products[:10], workflow_run_id="run-chunk-1", elapsed_time=2.0
        )
        chunk2_response = create_mock_response(
            products[10:], workflow_run_id="run-chunk-2", elapsed_time=1.5
        )

        responses.add(
            responses.POST,
            "https://dify.long-cod.ts.net/v1/workflows/run",
            json=chunk1_response,
            status=200,
        )
        responses.add(
            responses.POST,
            "https://dify.long-cod.ts.net/v1/workflows/run",
            json=chunk2_response,
            status=200,
        )

        client = WorkflowClient(api_key="app-test-key")
        result = client.estimate_products(products, "discord-123456")

        assert len(responses.calls) == 2
        assert len(result.estimates) == 11

    @responses.activate
    def test_aggregates_all_estimates(self):
        """Multiple chunks aggregate all estimates into single result."""
        call_count = [0]

        def request_callback(request):
            call_count[0] += 1
            if call_count[0] == 1:
                products = [f"Product {i}" for i in range(10)]
                return (
                    200,
                    {},
                    json.dumps(create_mock_response(products, elapsed_time=1.0)),
                )
            else:
                return (
                    200,
                    {},
                    json.dumps(create_mock_response(["Product 10"], elapsed_time=0.5)),
                )

        responses.add_callback(
            responses.POST,
            "https://dify.long-cod.ts.net/v1/workflows/run",
            callback=request_callback,
            content_type="application/json",
        )

        client = WorkflowClient(api_key="app-test-key")
        products = [f"Product {i}" for i in range(11)]
        result = client.estimate_products(products, "discord-123456")

        assert len(result.estimates) == 11
        assert result.estimates[0].product_name == "Product 0"
        assert result.estimates[10].product_name == "Product 10"

    @responses.activate
    def test_aggregates_elapsed_time(self):
        """Multiple chunks sum elapsed_time correctly."""
        call_count = [0]

        def request_callback(request):
            call_count[0] += 1
            if call_count[0] == 1:
                products = [f"Product {i}" for i in range(10)]
                return (
                    200,
                    {},
                    json.dumps(create_mock_response(products, elapsed_time=2.5)),
                )
            else:
                return (
                    200,
                    {},
                    json.dumps(create_mock_response(["Product 10"], elapsed_time=1.3)),
                )

        responses.add_callback(
            responses.POST,
            "https://dify.long-cod.ts.net/v1/workflows/run",
            callback=request_callback,
            content_type="application/json",
        )

        client = WorkflowClient(api_key="app-test-key")
        products = [f"Product {i}" for i in range(11)]
        result = client.estimate_products(products, "discord-123456")

        assert result.elapsed_time is not None
        assert abs(result.elapsed_time - 3.8) < 0.01

    @responses.activate
    def test_uses_last_workflow_run_id(self):
        """Uses workflow_run_id from last chunk, not first."""
        call_count = [0]

        def request_callback(request):
            call_count[0] += 1
            if call_count[0] == 1:
                products = [f"Product {i}" for i in range(10)]
                return (
                    200,
                    {},
                    json.dumps(
                        create_mock_response(products, workflow_run_id="first-run-id")
                    ),
                )
            else:
                return (
                    200,
                    {},
                    json.dumps(
                        create_mock_response(
                            ["Product 10"], workflow_run_id="last-run-id"
                        )
                    ),
                )

        responses.add_callback(
            responses.POST,
            "https://dify.long-cod.ts.net/v1/workflows/run",
            callback=request_callback,
            content_type="application/json",
        )

        client = WorkflowClient(api_key="app-test-key")
        products = [f"Product {i}" for i in range(11)]
        result = client.estimate_products(products, "discord-123456")

        assert result.workflow_run_id == "last-run-id"

    @responses.activate
    def test_twenty_five_products_three_api_calls(self):
        """25 products causes chunking into 3 API calls (10, 10, 5)."""
        products = [f"Product {i}" for i in range(1, 26)]

        responses.add(
            responses.POST,
            "https://dify.long-cod.ts.net/v1/workflows/run",
            json=create_mock_response(products[0:10]),
            status=200,
        )
        responses.add(
            responses.POST,
            "https://dify.long-cod.ts.net/v1/workflows/run",
            json=create_mock_response(products[10:20]),
            status=200,
        )
        responses.add(
            responses.POST,
            "https://dify.long-cod.ts.net/v1/workflows/run",
            json=create_mock_response(products[20:25]),
            status=200,
        )

        client = WorkflowClient(api_key="app-test-key")
        result = client.estimate_products(products, "discord-123456")

        assert len(responses.calls) == 3
        assert len(result.estimates) == 25

    @responses.activate
    def test_propagates_timeout_error(self):
        """Timeout error in any chunk propagates to caller."""
        products = [f"Product {i}" for i in range(1, 12)]

        responses.add(
            responses.POST,
            "https://dify.long-cod.ts.net/v1/workflows/run",
            json=create_mock_response(products[0:10]),
            status=200,
        )
        responses.add(
            responses.POST,
            "https://dify.long-cod.ts.net/v1/workflows/run",
            body=requests.Timeout("Timeout"),
        )

        client = WorkflowClient(api_key="app-test-key")

        with pytest.raises(requests.Timeout):
            client.estimate_products(products, "discord-123456")

    @responses.activate
    def test_propagates_http_error(self):
        """HTTP error in any chunk propagates to caller."""
        products = [f"Product {i}" for i in range(1, 12)]

        responses.add(
            responses.POST,
            "https://dify.long-cod.ts.net/v1/workflows/run",
            json=create_mock_response(products[0:10]),
            status=200,
        )
        responses.add(
            responses.POST,
            "https://dify.long-cod.ts.net/v1/workflows/run",
            json={"error": "Server error"},
            status=500,
        )

        client = WorkflowClient(api_key="app-test-key")

        with pytest.raises(requests.HTTPError):
            client.estimate_products(products, "discord-123456")


class TestChunkingEdgeCases:
    """Edge case tests for chunking behavior."""

    @responses.activate
    def test_no_elapsed_time_returns_none(self):
        """When no chunks have elapsed_time, result.elapsed_time is None."""
        products = ["Product A", "Product B"]

        chunk_response = create_mock_response(products)
        del chunk_response["elapsed_time"]

        responses.add(
            responses.POST,
            "https://dify.long-cod.ts.net/v1/workflows/run",
            json=chunk_response,
            status=200,
        )

        client = WorkflowClient(api_key="app-test-key")
        result = client.estimate_products(products, "discord-123456")

        assert result.elapsed_time is None

    @responses.activate
    def test_partial_elapsed_time_aggregation(self):
        """Aggregates elapsed_time even if some chunks lack it."""
        call_count = [0]

        def request_callback(request):
            call_count[0] += 1
            if call_count[0] == 1:
                products = [f"Product {i}" for i in range(10)]
                return (
                    200,
                    {},
                    json.dumps(create_mock_response(products, elapsed_time=2.0)),
                )
            else:
                response = create_mock_response(["Product 10"])
                del response["elapsed_time"]
                return (200, {}, json.dumps(response))

        responses.add_callback(
            responses.POST,
            "https://dify.long-cod.ts.net/v1/workflows/run",
            callback=request_callback,
            content_type="application/json",
        )

        client = WorkflowClient(api_key="app-test-key")
        products = [f"Product {i}" for i in range(11)]
        result = client.estimate_products(products, "discord-123456")

        assert result.elapsed_time == 2.0

    @responses.activate
    def test_no_workflow_run_id_in_chunks(self):
        """When no chunks have workflow_run_id, result.workflow_run_id is None."""
        products = ["Product A", "Product B"]

        chunk_response = create_mock_response(products)
        del chunk_response["workflow_run_id"]

        responses.add(
            responses.POST,
            "https://dify.long-cod.ts.net/v1/workflows/run",
            json=chunk_response,
            status=200,
        )

        client = WorkflowClient(api_key="app-test-key")
        result = client.estimate_products(products, "discord-123456")

        assert result.workflow_run_id is None
