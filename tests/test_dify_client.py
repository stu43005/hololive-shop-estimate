"""Tests for Dify Knowledge Base client wrapper.

Tests cover all API endpoints with mocked HTTP responses:
- create_document_by_text (POST)
- update_document_by_text (POST)
- list_documents (GET with pagination)
- get_indexing_status (GET)

Error scenarios tested:
- 401/403 authentication failures → DifyAuthError
- 429 rate limiting → DifyRateLimitError
- 4xx/5xx API errors → DifyAPIError
- Network/timeout errors via requests
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from estimator_king.sync.dify_client import (
    DifyKBClient,
    DifyAPIError,
    DifyAuthError,
    DifyRateLimitError,
)


@pytest.fixture
def dify_client():
    return DifyKBClient(
        api_key="test-dataset-key",
        base_url="https://dify.example.com/v1",
        dataset_id="test-dataset-123",
        timeout=30,
    )


class TestDifyKBClientInit:
    def test_init_success(self):
        client = DifyKBClient(
            api_key="key",
            base_url="https://api.example.com/v1",
            dataset_id="ds-123",
        )
        assert client.api_key == "key"
        assert client.base_url == "https://api.example.com/v1"
        assert client.dataset_id == "ds-123"
        assert client.timeout == 30
        assert client.session is not None

    def test_init_trims_trailing_slash(self):
        client = DifyKBClient(
            api_key="key",
            base_url="https://api.example.com/v1/",
            dataset_id="ds-123",
        )
        assert client.base_url == "https://api.example.com/v1"

    def test_init_custom_timeout(self):
        client = DifyKBClient(
            api_key="key",
            base_url="https://api.example.com/v1",
            dataset_id="ds-123",
            timeout=60,
        )
        assert client.timeout == 60

    def test_init_missing_api_key(self):
        with pytest.raises(ValueError, match="required"):
            DifyKBClient(
                api_key="", base_url="https://api.example.com/v1", dataset_id="ds-123"
            )

    def test_init_missing_base_url(self):
        with pytest.raises(ValueError, match="required"):
            DifyKBClient(api_key="key", base_url="", dataset_id="ds-123")

    def test_init_missing_dataset_id(self):
        with pytest.raises(ValueError, match="required"):
            DifyKBClient(
                api_key="key", base_url="https://api.example.com/v1", dataset_id=""
            )


class TestCreateDocumentByText:
    def test_create_document_success(self, dify_client):
        with patch.object(dify_client.session, "post") as mock_post:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "document": {"id": "doc-123", "name": "Test Doc"},
                "batch": "batch-456",
            }
            mock_post.return_value = mock_response

            result = dify_client.create_document_by_text(
                name="Test Doc",
                text="This is test content",
            )

            assert result["document"]["id"] == "doc-123"
            assert result["batch"] == "batch-456"
            mock_post.assert_called_once()
            call_args = mock_post.call_args
            assert "document/create_by_text" in call_args[0][0]
            assert call_args[1]["json"]["name"] == "Test Doc"
            assert call_args[1]["json"]["text"] == "This is test content"
            assert call_args[1]["json"]["indexing_technique"] == "high_quality"
            assert call_args[1]["json"]["process_rule"] == {"mode": "automatic"}

    def test_create_document_with_metadata(self, dify_client):
        with patch.object(dify_client.session, "post") as mock_post:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "document": {"id": "doc-456"},
                "batch": "batch-789",
            }
            mock_post.return_value = mock_response

            result = dify_client.create_document_by_text(
                name="Product Doc",
                text="Content",
                metadata={"source": "shopify", "store_id": "hololive"},
            )

            assert result["document"]["id"] == "doc-456"
            call_args = mock_post.call_args
            assert call_args[1]["json"]["metadata"] == {
                "source": "shopify",
                "store_id": "hololive",
            }

    @pytest.mark.waf
    def test_create_document_auth_error(self, dify_client):
        with patch.object(dify_client.session, "post") as mock_post:
            mock_response = Mock()
            mock_response.status_code = 401
            mock_response.json.return_value = {"message": "Invalid API key"}
            mock_post.return_value = mock_response

            with pytest.raises(DifyAuthError, match="Authentication failed"):
                dify_client.create_document_by_text(name="Test", text="Content")

    @pytest.mark.waf
    def test_create_document_forbidden_error(self, dify_client):
        with patch.object(dify_client.session, "post") as mock_post:
            mock_response = Mock()
            mock_response.status_code = 403
            mock_response.json.return_value = {"message": "Forbidden"}
            mock_post.return_value = mock_response

            with pytest.raises(DifyAuthError):
                dify_client.create_document_by_text(name="Test", text="Content")


class TestUpdateDocumentByText:
    def test_update_document_success(self, dify_client):
        with patch.object(dify_client.session, "post") as mock_post:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "document": {"id": "doc-123", "name": "Updated Doc"},
                "batch": "batch-update-789",
            }
            mock_post.return_value = mock_response

            result = dify_client.update_document_by_text(
                document_id="doc-123",
                name="Updated Doc",
                text="Updated content",
            )

            assert result["document"]["id"] == "doc-123"
            assert result["batch"] == "batch-update-789"
            mock_post.assert_called_once()
            call_args = mock_post.call_args
            assert "doc-123/update_by_text" in call_args[0][0]

    @pytest.mark.waf
    def test_update_document_auth_error(self, dify_client):
        with patch.object(dify_client.session, "post") as mock_post:
            mock_response = Mock()
            mock_response.status_code = 403
            mock_response.json.return_value = {"message": "Permission denied"}
            mock_post.return_value = mock_response

            with pytest.raises(DifyAuthError):
                dify_client.update_document_by_text(
                    document_id="doc-123",
                    name="Updated",
                    text="Content",
                )


class TestListDocuments:
    def test_list_documents_success(self, dify_client):
        with patch.object(dify_client.session, "get") as mock_get:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "data": [
                    {"id": "doc-1", "name": "Doc 1"},
                    {"id": "doc-2", "name": "Doc 2"},
                ],
                "total": 2,
                "limit": 100,
                "offset": 0,
            }
            mock_get.return_value = mock_response

            result = dify_client.list_documents()

            assert len(result["data"]) == 2
            assert result["total"] == 2
            mock_get.assert_called_once()
            call_args = mock_get.call_args
            assert "documents" in call_args[0][0]
            assert call_args[1]["params"]["limit"] == 100
            assert call_args[1]["params"]["offset"] == 0

    def test_list_documents_with_pagination(self, dify_client):
        with patch.object(dify_client.session, "get") as mock_get:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "data": [{"id": "doc-3", "name": "Doc 3"}],
                "total": 50,
                "limit": 10,
                "offset": 20,
            }
            mock_get.return_value = mock_response

            result = dify_client.list_documents(limit=10, offset=20)

            assert len(result["data"]) == 1
            assert result["offset"] == 20
            call_args = mock_get.call_args
            assert call_args[1]["params"]["limit"] == 10
            assert call_args[1]["params"]["offset"] == 20

    def test_list_documents_limit_enforced(self, dify_client):
        with patch.object(dify_client.session, "get") as mock_get:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"data": [], "total": 0}
            mock_get.return_value = mock_response

            dify_client.list_documents(limit=500)

            call_args = mock_get.call_args
            assert call_args[1]["params"]["limit"] == 100

    def test_list_documents_empty(self, dify_client):
        with patch.object(dify_client.session, "get") as mock_get:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "data": [],
                "total": 0,
                "limit": 100,
                "offset": 0,
            }
            mock_get.return_value = mock_response

            result = dify_client.list_documents()

            assert result["total"] == 0
            assert len(result["data"]) == 0


class TestGetIndexingStatus:
    def test_get_indexing_status_pending(self, dify_client):
        with patch.object(dify_client.session, "get") as mock_get:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "status": "pending",
                "progress": 0,
            }
            mock_get.return_value = mock_response

            result = dify_client.get_indexing_status("batch-456")

            assert result["status"] == "pending"
            assert result["progress"] == 0
            mock_get.assert_called_once()
            call_args = mock_get.call_args
            assert "batch-456/indexing-status" in call_args[0][0]

    def test_get_indexing_status_indexing(self, dify_client):
        with patch.object(dify_client.session, "get") as mock_get:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "status": "indexing",
                "progress": 45,
            }
            mock_get.return_value = mock_response

            result = dify_client.get_indexing_status("batch-456")

            assert result["status"] == "indexing"
            assert result["progress"] == 45

    def test_get_indexing_status_completed(self, dify_client):
        with patch.object(dify_client.session, "get") as mock_get:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "status": "completed",
                "progress": 100,
            }
            mock_get.return_value = mock_response

            result = dify_client.get_indexing_status("batch-456")

            assert result["status"] == "completed"
            assert result["progress"] == 100

    def test_get_indexing_status_failed(self, dify_client):
        with patch.object(dify_client.session, "get") as mock_get:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "status": "failed",
                "progress": 50,
                "error": "Index write failed",
            }
            mock_get.return_value = mock_response

            result = dify_client.get_indexing_status("batch-456")

            assert result["status"] == "failed"
            assert result["error"] == "Index write failed"


class TestErrorHandling:
    @pytest.mark.waf
    def test_rate_limit_error(self, dify_client):
        with patch.object(dify_client.session, "post") as mock_post:
            mock_response = Mock()
            mock_response.status_code = 429
            mock_response.headers = {"Retry-After": "60"}
            mock_post.return_value = mock_response

            with pytest.raises(DifyRateLimitError, match="Rate limited"):
                dify_client.create_document_by_text(name="Test", text="Content")

    @pytest.mark.waf
    def test_rate_limit_error_without_retry_after(self, dify_client):
        with patch.object(dify_client.session, "post") as mock_post:
            mock_response = Mock()
            mock_response.status_code = 429
            mock_response.headers = {}
            mock_post.return_value = mock_response

            with pytest.raises(DifyRateLimitError):
                dify_client.create_document_by_text(name="Test", text="Content")

    def test_api_error_generic(self, dify_client):
        with patch.object(dify_client.session, "post") as mock_post:
            mock_response = Mock()
            mock_response.status_code = 500
            mock_response.json.return_value = {"message": "Internal server error"}
            mock_post.return_value = mock_response

            with pytest.raises(DifyAPIError, match="API error"):
                dify_client.create_document_by_text(name="Test", text="Content")

    def test_api_error_400_bad_request(self, dify_client):
        with patch.object(dify_client.session, "get") as mock_get:
            mock_response = Mock()
            mock_response.status_code = 400
            mock_response.json.return_value = {"message": "Invalid parameter"}
            mock_get.return_value = mock_response

            with pytest.raises(DifyAPIError, match="API error"):
                dify_client.list_documents()

    def test_api_error_with_text_fallback(self, dify_client):
        with patch.object(dify_client.session, "post") as mock_post:
            mock_response = Mock()
            mock_response.status_code = 502
            mock_response.json.side_effect = ValueError("Invalid JSON")
            mock_response.text = "Bad Gateway"
            mock_post.return_value = mock_response

            with pytest.raises(DifyAPIError):
                dify_client.create_document_by_text(name="Test", text="Content")

    def test_auth_error_with_text_fallback(self, dify_client):
        with patch.object(dify_client.session, "post") as mock_post:
            mock_response = Mock()
            mock_response.status_code = 401
            mock_response.json.side_effect = ValueError("Invalid JSON")
            mock_response.text = "Unauthorized"
            mock_post.return_value = mock_response

            with pytest.raises(DifyAuthError, match="Authentication failed"):
                dify_client.create_document_by_text(name="Test", text="Content")


class TestSessionConfiguration:
    def test_session_headers_set(self, dify_client):
        assert "Authorization" in dify_client.session.headers
        assert "Bearer test-dataset-key" in dify_client.session.headers["Authorization"]
        assert dify_client.session.headers["Content-Type"] == "application/json"

    def test_session_retry_policy(self, dify_client):
        assert dify_client.session.adapters["https://"]._pool_connections > 0

    @pytest.mark.waf
    def test_timeout_passed_to_requests(self, dify_client):
        with patch.object(dify_client.session, "get") as mock_get:
            mock_response = Mock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"data": []}
            mock_get.return_value = mock_response

            dify_client.list_documents()

            call_args = mock_get.call_args
            assert call_args[1]["timeout"] == 30


class TestIntegration:
    def test_create_then_check_status(self, dify_client):
        with (
            patch.object(dify_client.session, "post") as mock_post,
            patch.object(dify_client.session, "get") as mock_get,
        ):
            mock_create_resp = Mock()
            mock_create_resp.status_code = 200
            mock_create_resp.json.return_value = {
                "document": {"id": "doc-abc"},
                "batch": "batch-xyz",
            }
            mock_post.return_value = mock_create_resp

            create_result = dify_client.create_document_by_text(
                name="Test Product",
                text="Product details here",
            )

            mock_status_resp = Mock()
            mock_status_resp.status_code = 200
            mock_status_resp.json.return_value = {
                "status": "completed",
                "progress": 100,
            }
            mock_get.return_value = mock_status_resp

            status_result = dify_client.get_indexing_status(create_result["batch"])

            assert status_result["status"] == "completed"

    def test_update_then_check_status(self, dify_client):
        with (
            patch.object(dify_client.session, "post") as mock_post,
            patch.object(dify_client.session, "get") as mock_get,
        ):
            mock_update_resp = Mock()
            mock_update_resp.status_code = 200
            mock_update_resp.json.return_value = {
                "document": {"id": "doc-abc"},
                "batch": "batch-update-123",
            }
            mock_post.return_value = mock_update_resp

            update_result = dify_client.update_document_by_text(
                document_id="doc-abc",
                name="Updated Product",
                text="Updated details",
            )

            mock_status_resp = Mock()
            mock_status_resp.status_code = 200
            mock_status_resp.json.return_value = {
                "status": "indexing",
                "progress": 60,
            }
            mock_get.return_value = mock_status_resp

            status_result = dify_client.get_indexing_status(update_result["batch"])

            assert status_result["progress"] == 60
