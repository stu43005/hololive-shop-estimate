"""Tests for _poll_indexing_status() with correct Dify API response shape.

This test file validates fixes for two bugs:
1. Response parsing: data is a LIST not a dict
   - Old (buggy): response.get("data", {}).get("indexing_status")
   - New (fixed): response.get("data", [{}])[0].get("indexing_status")
2. Terminal failure status: Dify uses "error" not "failed"
   - Old (buggy): status == "failed"
   - New (fixed): status == "error"
"""

# pyright: reportMissingImports=false

import pytest
from unittest.mock import Mock

from estimator_king.sync.dify_client import (
    DifyKBClient,
    DifyRateLimitError,
    DifyAPIError,
)
from estimator_king.sync.engine import _poll_indexing_status


@pytest.fixture
def dify_client():
    """Mock DifyKBClient for testing."""
    return Mock(spec=DifyKBClient)


class TestPollIndexingStatusListResponseParsing:
    """Tests validating that data is parsed as a LIST (not dict)."""

    def test_data_as_list_completed_status(self, dify_client):
        """Status 'completed' in data[0] (list format) returns True immediately."""
        # Real Dify API returns data as a list
        dify_client.get_indexing_status.return_value = {
            "data": [
                {
                    "indexing_status": "completed",
                    "id": "doc-uuid-1",
                    "error": None,
                }
            ]
        }

        result = _poll_indexing_status(dify_client, "batch-123", max_wait=60)

        assert result is True
        assert dify_client.get_indexing_status.call_count == 1

    def test_data_as_list_multiple_items_uses_first(self, dify_client):
        """If data list has multiple items, use first one only."""
        dify_client.get_indexing_status.return_value = {
            "data": [
                {"indexing_status": "completed", "id": "doc-1"},
                {"indexing_status": "indexing", "id": "doc-2"},  # ignored
                {"indexing_status": "indexing", "id": "doc-3"},  # ignored
            ]
        }

        result = _poll_indexing_status(dify_client, "batch-123", max_wait=60)

        assert result is True
        assert dify_client.get_indexing_status.call_count == 1

    def test_empty_data_list_continues_polling(self, dify_client):
        """Empty data list ([]) treats as no status, continues polling."""
        dify_client.get_indexing_status.side_effect = [
            {"data": []},  # Empty list
            {"data": [{"indexing_status": "completed", "id": "doc-1"}]},
        ]

        result = _poll_indexing_status(dify_client, "batch-123", max_wait=60)

        assert result is True
        assert dify_client.get_indexing_status.call_count == 2

    def test_missing_data_key_continues_polling(self, dify_client):
        """Response missing 'data' key treats as no status, continues polling."""
        dify_client.get_indexing_status.side_effect = [
            {},  # No data key
            {"data": [{"indexing_status": "completed", "id": "doc-1"}]},
        ]

        result = _poll_indexing_status(dify_client, "batch-123", max_wait=60)

        assert result is True
        assert dify_client.get_indexing_status.call_count == 2

    def test_data_list_with_null_status_continues_polling(self, dify_client):
        """data[0] has null/missing indexing_status, continues polling."""
        dify_client.get_indexing_status.side_effect = [
            {"data": [{"id": "doc-1"}]},  # No indexing_status
            {"data": [{"indexing_status": "completed", "id": "doc-1"}]},
        ]

        result = _poll_indexing_status(dify_client, "batch-123", max_wait=60)

        assert result is True
        assert dify_client.get_indexing_status.call_count == 2


class TestPollIndexingStatusErrorTerminalStatus:
    """Tests validating that terminal failure status is 'error' (not 'failed')."""

    def test_error_status_returns_false_immediately(self, dify_client):
        """Status 'error' (not 'failed') on first call returns False immediately."""
        dify_client.get_indexing_status.return_value = {
            "data": [
                {
                    "indexing_status": "error",
                    "id": "doc-uuid-1",
                    "error": "Failed to parse document",
                }
            ]
        }

        result = _poll_indexing_status(dify_client, "batch-123", max_wait=60)

        assert result is False
        assert dify_client.get_indexing_status.call_count == 1

    def test_failed_status_does_not_return_false(self, dify_client):
        """Status 'failed' (old buggy value) should NOT be treated as terminal.
        This will timeout since it keeps polling indefinitely.
        """
        dify_client.get_indexing_status.return_value = {
            "data": [{"indexing_status": "failed", "id": "doc-1"}]
        }

        result = _poll_indexing_status(dify_client, "batch-123", max_wait=4)

        # Should timeout instead of immediately returning False
        assert result is False
        # Polling should happen multiple times before timeout
        assert dify_client.get_indexing_status.call_count >= 2

    def test_error_transitions_from_other_statuses(self, dify_client):
        """Transitions through intermediate statuses to 'error'."""
        dify_client.get_indexing_status.side_effect = [
            {"data": [{"indexing_status": "waiting", "id": "doc-1"}]},
            {"data": [{"indexing_status": "parsing", "id": "doc-1"}]},
            {
                "data": [
                    {"indexing_status": "error", "id": "doc-1", "error": "Parse error"}
                ]
            },
        ]

        result = _poll_indexing_status(dify_client, "batch-123", max_wait=60)

        assert result is False
        assert dify_client.get_indexing_status.call_count == 3


class TestPollIndexingStatusIntermediateStatuses:
    """Tests for valid intermediate (non-terminal) statuses."""

    def test_waiting_status_continues_polling(self, dify_client):
        """Status 'waiting' is intermediate, continues polling."""
        dify_client.get_indexing_status.side_effect = [
            {"data": [{"indexing_status": "waiting", "id": "doc-1"}]},
            {"data": [{"indexing_status": "completed", "id": "doc-1"}]},
        ]

        result = _poll_indexing_status(dify_client, "batch-123", max_wait=60)

        assert result is True
        assert dify_client.get_indexing_status.call_count == 2

    def test_parsing_status_continues_polling(self, dify_client):
        """Status 'parsing' is intermediate, continues polling."""
        dify_client.get_indexing_status.side_effect = [
            {"data": [{"indexing_status": "parsing", "id": "doc-1"}]},
            {"data": [{"indexing_status": "completed", "id": "doc-1"}]},
        ]

        result = _poll_indexing_status(dify_client, "batch-123", max_wait=60)

        assert result is True
        assert dify_client.get_indexing_status.call_count == 2

    def test_cleaning_status_continues_polling(self, dify_client):
        """Status 'cleaning' is intermediate, continues polling."""
        dify_client.get_indexing_status.side_effect = [
            {"data": [{"indexing_status": "cleaning", "id": "doc-1"}]},
            {"data": [{"indexing_status": "completed", "id": "doc-1"}]},
        ]

        result = _poll_indexing_status(dify_client, "batch-123", max_wait=60)

        assert result is True
        assert dify_client.get_indexing_status.call_count == 2

    def test_splitting_status_continues_polling(self, dify_client):
        """Status 'splitting' is intermediate, continues polling."""
        dify_client.get_indexing_status.side_effect = [
            {"data": [{"indexing_status": "splitting", "id": "doc-1"}]},
            {"data": [{"indexing_status": "completed", "id": "doc-1"}]},
        ]

        result = _poll_indexing_status(dify_client, "batch-123", max_wait=60)

        assert result is True
        assert dify_client.get_indexing_status.call_count == 2

    def test_indexing_status_continues_polling(self, dify_client):
        """Status 'indexing' is intermediate, continues polling."""
        dify_client.get_indexing_status.side_effect = [
            {"data": [{"indexing_status": "indexing", "id": "doc-1"}]},
            {"data": [{"indexing_status": "completed", "id": "doc-1"}]},
        ]

        result = _poll_indexing_status(dify_client, "batch-123", max_wait=60)

        assert result is True
        assert dify_client.get_indexing_status.call_count == 2


class TestPollIndexingStatusTimeout:
    """Tests for timeout behavior."""

    def test_timeout_with_never_completing(self, dify_client):
        """Polling never reaches completed, times out and returns False."""
        dify_client.get_indexing_status.return_value = {
            "data": [{"indexing_status": "indexing", "id": "doc-1"}]
        }

        result = _poll_indexing_status(dify_client, "batch-123", max_wait=6)

        assert result is False
        assert dify_client.get_indexing_status.call_count >= 2


class TestPollIndexingStatusRateLimiting:
    """Tests for rate limit error handling."""

    def test_rate_limit_with_backoff_succeeds(self, dify_client):
        """DifyRateLimitError triggers backoff, then succeeds."""
        dify_client.get_indexing_status.side_effect = [
            DifyRateLimitError("Rate limited (429). Retry after: unknown"),
            DifyRateLimitError("Rate limited (429). Retry after: unknown"),
            {"data": [{"indexing_status": "completed", "id": "doc-1"}]},
        ]

        result = _poll_indexing_status(dify_client, "batch-123", max_wait=60)

        assert result is True
        assert dify_client.get_indexing_status.call_count == 3

    def test_rate_limit_timeout(self, dify_client):
        """DifyRateLimitError repeatedly, eventually timeout."""
        dify_client.get_indexing_status.side_effect = DifyRateLimitError("Rate limited")

        result = _poll_indexing_status(dify_client, "batch-123", max_wait=10)

        assert result is False
        assert dify_client.get_indexing_status.call_count >= 2


class TestPollIndexingStatusInputValidation:
    """Tests for input validation."""

    def test_empty_batch_id_raises_error(self, dify_client):
        """Empty batch_id raises ValueError."""
        with pytest.raises(ValueError, match="batch_id cannot be empty"):
            _poll_indexing_status(dify_client, "", max_wait=60)

    def test_non_positive_max_wait_raises_error(self, dify_client):
        """Non-positive max_wait raises ValueError."""
        with pytest.raises(ValueError, match="max_wait must be positive"):
            _poll_indexing_status(dify_client, "batch-123", max_wait=0)

        with pytest.raises(ValueError, match="max_wait must be positive"):
            _poll_indexing_status(dify_client, "batch-123", max_wait=-1)


class TestPollIndexingStatusAPIErrors:
    """Tests for API error handling."""

    def test_api_error_non_rate_limit_propagates(self, dify_client):
        """DifyAPIError (non-rate-limit) propagates immediately."""
        dify_client.get_indexing_status.side_effect = DifyAPIError(
            "API error (500): Server error"
        )

        with pytest.raises(DifyAPIError):
            _poll_indexing_status(dify_client, "batch-123", max_wait=60)
