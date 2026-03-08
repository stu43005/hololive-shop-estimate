"""Dify Knowledge Base client wrapper for document operations.

This module provides a simple, robust wrapper around the Dify Knowledge Base API
for managing documents (create, update, list, indexing status).

Key design decisions:
- Uses requests.Session() for connection pooling and header management
- Custom exceptions for different error types (auth, rate limit, API errors)
- Implements exponential backoff retry for 5xx errors (max 3 attempts)
- Does NOT retry on 4xx client errors
- Configurable timeout (default 30 seconds)
"""

import time
from typing import Any, Dict, List, Optional
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class DifyAPIError(Exception):
    """Base exception for Dify API errors."""

    pass


class DifyAuthError(DifyAPIError):
    """Raised when API authentication fails (401/403)."""

    pass


class DifyRateLimitError(DifyAPIError):
    """Raised when rate limit is hit (429). Caller should implement backoff."""

    pass


class DifyKBClient:
    """Client for Dify Knowledge Base API (dataset document operations).

    Manages document CRUD operations in a Dify Knowledge Base dataset.
    Handles authentication, retries, and rate limiting.

    Attributes:
        api_key: Bearer token for authentication (dataset API key)
        base_url: Base URL of Dify API (e.g., https://dify.example.com/v1)
        dataset_id: UUID of the target knowledge base dataset
        session: requests.Session with configured headers and retry policy
        timeout: Default timeout for all requests (seconds)
    """

    def __init__(self, api_key: str, base_url: str, dataset_id: str, timeout: int = 30):
        """Initialize the Dify KB client.

        Args:
            api_key: Bearer token (dataset-xxx format for KB operations)
            base_url: Base URL for Dify API (should end with /v1)
            dataset_id: UUID of the knowledge base dataset
            timeout: Request timeout in seconds (default: 30)

        Raises:
            ValueError: If required parameters are empty
        """
        if not api_key or not base_url or not dataset_id:
            raise ValueError("api_key, base_url, and dataset_id are required")

        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.dataset_id = dataset_id
        self.timeout = timeout

        # Create session with retry policy for 5xx errors
        self.session = requests.Session()

        # Set up retry strategy: retry on 5xx, exponential backoff
        retry_strategy = Retry(
            total=3,  # Max 3 total attempts (1 initial + 2 retries)
            backoff_factor=0.5,  # 0.5s, 1s, 2s between retries
            status_forcelist=[500, 502, 503, 504],  # Only retry on 5xx
            allowed_methods=["GET", "POST", "PUT", "DELETE"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        # Set default headers for all requests
        self.session.headers["Authorization"] = f"Bearer {api_key}"  # type: ignore[assignment]
        self.session.headers["Content-Type"] = "application/json"  # type: ignore[assignment]

    def create_document_by_text(
        self,
        name: str,
        text: str,
        metadata: Optional[Dict[str, Any]] = None,
        indexing_technique: str = "high_quality",
        process_rule: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create a document in the knowledge base from text.

        This operation is async - the API returns immediately with a batch_id.
        Use get_indexing_status(batch_id) to poll for completion.

        API Endpoint: POST /v1/datasets/{dataset_id}/document/create_by_text

        Args:
            name: Document name/title
            text: Full document content
            metadata: Optional metadata dict (e.g., {"source": "shopify", "store_id": "123"})
            indexing_technique: Indexing mode - "high_quality" (embedding, default) or "economy" (keyword)
            process_rule: Chunking/processing rules. Defaults to {"mode": "automatic"} if not provided.

        Returns:
            Response dict containing:
            - document: {"id": str, "name": str, ...}
            - batch: str (batch_id for polling indexing status)

        Raises:
            DifyAuthError: On 401/403 authentication failure
            DifyRateLimitError: On 429 rate limit (caller should backoff)
            DifyAPIError: On other 4xx/5xx errors
        """
        url = f"{self.base_url}/datasets/{self.dataset_id}/document/create_by_text"

        if process_rule is None:
            process_rule = {"mode": "automatic"}

        payload: Dict[str, Any] = {
            "name": name,
            "text": text,
            "indexing_technique": indexing_technique,
            "process_rule": process_rule,
        }
        if metadata:
            payload["metadata"] = metadata

        response = self.session.post(
            url,
            json=payload,
            timeout=self.timeout,
        )

        return self._handle_response(response)

    def update_document_by_text(self, document_id: str, name: str, text: str) -> Dict:
        """Update an existing document in the knowledge base.

        This operation is async - returns batch_id for polling.

        API Endpoint: POST /v1/datasets/{dataset_id}/documents/{document_id}/update_by_text

        Args:
            document_id: UUID of the document to update
            name: Updated document name
            text: Updated document content

        Returns:
            Response dict containing:
            - document: {"id": str, "name": str, ...}
            - batch: str (batch_id for polling indexing status)

        Raises:
            DifyAuthError: On 401/403 authentication failure
            DifyRateLimitError: On 429 rate limit
            DifyAPIError: On other 4xx/5xx errors
        """
        url = f"{self.base_url}/datasets/{self.dataset_id}/documents/{document_id}/update_by_text"

        payload = {
            "name": name,
            "text": text,
        }

        response = self.session.post(
            url,
            json=payload,
            timeout=self.timeout,
        )

        return self._handle_response(response)

    def list_documents(self, limit: int = 100, offset: int = 0) -> Dict:
        """List documents in the knowledge base with pagination.

        API Endpoint: GET /v1/datasets/{dataset_id}/documents

        Args:
            limit: Number of documents to return (default: 100, max: 100)
            offset: Pagination offset (default: 0)

        Returns:
            Response dict containing:
            - data: List of document dicts with id, name, created_at, updated_at, etc.
            - total: int (total document count)
            - limit: int (requested limit)
            - offset: int (requested offset)

        Raises:
            DifyAuthError: On 401/403 authentication failure
            DifyRateLimitError: On 429 rate limit
            DifyAPIError: On other 4xx/5xx errors
        """
        url = f"{self.base_url}/datasets/{self.dataset_id}/documents"

        params = {
            "limit": min(limit, 100),  # Enforce max limit
            "offset": offset,
        }

        response = self.session.get(
            url,
            params=params,
            timeout=self.timeout,
        )

        return self._handle_response(response)

    def get_indexing_status(self, batch_id: str) -> Dict:
        """Get the indexing status of a document batch.

        Use this to poll after create_document_by_text() or update_document_by_text()
        until status is "completed" or "failed".

        API Endpoint: GET /v1/datasets/{dataset_id}/documents/{batch_id}/indexing-status

        Args:
            batch_id: Batch ID returned from create/update operations

        Returns:
            Response dict containing:
            - status: str (one of "pending", "indexing", "completed", "failed")
            - progress: int (percentage 0-100)
            - error: str (if status == "failed")

        Raises:
            DifyAuthError: On 401/403 authentication failure
            DifyRateLimitError: On 429 rate limit
            DifyAPIError: On other 4xx/5xx errors
        """
        url = f"{self.base_url}/datasets/{self.dataset_id}/documents/{batch_id}/indexing-status"

        response = self.session.get(
            url,
            timeout=self.timeout,
        )

        return self._handle_response(response)

    def delete_document(self, document_id: str) -> None:
        """Delete a document from the knowledge base.

        API Endpoint: DELETE /v1/datasets/{dataset_id}/documents/{document_id}

        Args:
            document_id: UUID of the document to delete

        Returns:
            None

        Raises:
            DifyAuthError: On 401/403 authentication failure
            DifyRateLimitError: On 429 rate limit
            DifyAPIError: On other 4xx/5xx errors
        """
        url = f"{self.base_url}/datasets/{self.dataset_id}/documents/{document_id}"

        response = self.session.delete(
            url,
            timeout=self.timeout,
        )

        self._handle_response(response)

    def _handle_response(self, response: requests.Response) -> Dict:
        """Handle HTTP response and raise appropriate exceptions.

        Args:
            response: requests.Response object

        Returns:
            Parsed JSON response dict

        Raises:
            DifyAuthError: On 401/403
            DifyRateLimitError: On 429
            DifyAPIError: On other errors
        """
        # Success case
        if response.status_code < 400:
            return response.json()

        # Authentication errors
        if response.status_code in (401, 403):
            try:
                error_msg = response.json().get("message", "Unknown error")
            except Exception:
                error_msg = response.text or "Unknown error"
            raise DifyAuthError(
                f"Authentication failed ({response.status_code}): {error_msg}"
            )

        # Rate limiting
        if response.status_code == 429:
            # Extract Retry-After header if available
            retry_after = response.headers.get("Retry-After", "unknown")
            raise DifyRateLimitError(f"Rate limited (429). Retry after: {retry_after}")

        # Other client/server errors
        try:
            error_data = response.json()
            error_msg = error_data.get("message", str(error_data))
        except Exception:
            error_msg = response.text or "Unknown error"

        raise DifyAPIError(f"API error ({response.status_code}): {error_msg}")
