"""Async wrapper around DifyKBClient for non-blocking sync in async pipeline.

This module provides AsyncDifySync, which wraps the synchronous DifyKBClient
and runs its methods in a thread pool using asyncio.to_thread(). This allows
the sync Dify client to be called from async code without blocking the event loop.
"""

import asyncio
from typing import Any, Dict, Optional

from estimator_king.sync.dify_client import DifyKBClient


class AsyncDifySync:
    """Async wrapper around synchronous DifyKBClient.

    Delegates all operations to the underlying sync client, running them
    in a thread pool via asyncio.to_thread() to avoid blocking the event loop.

    Attributes:
        client: The underlying synchronous DifyKBClient instance.
    """

    def __init__(self, client: DifyKBClient):
        """Initialize the async wrapper.

        Args:
            client: A DifyKBClient instance to wrap.
        """
        self.client = client

    async def create_document(
        self,
        dataset_id: str,
        name: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create a document in the knowledge base asynchronously.

        Runs DifyKBClient.create_document_by_text() in a thread pool
        to avoid blocking the event loop.

        Args:
            dataset_id: UUID of the knowledge base dataset (not used, for API compatibility).
            name: Document name/title.
            content: Full document content.
            metadata: Optional metadata dict (e.g., {"source": "shopify"}).

        Returns:
            Response dict containing:
            - document: {"id": str, "name": str, ...}
            - batch: str (batch_id for polling indexing status)

        Raises:
            DifyAuthError: On 401/403 authentication failure
            DifyRateLimitError: On 429 rate limit
            DifyAPIError: On other 4xx/5xx errors
        """
        return await asyncio.to_thread(
            self.client.create_document_by_text,
            name=name,
            text=content,
            metadata=metadata,
        )

    async def update_document(
        self,
        dataset_id: str,
        doc_id: str,
        name: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Update an existing document in the knowledge base asynchronously.

        Runs DifyKBClient.update_document_by_text() in a thread pool
        to avoid blocking the event loop.

        Args:
            dataset_id: UUID of the knowledge base dataset (not used, for API compatibility).
            doc_id: UUID of the document to update.
            name: Updated document name.
            content: Updated document content.
            metadata: Optional metadata (currently unused, for API compatibility).

        Returns:
            Response dict containing:
            - document: {"id": str, "name": str, ...}
            - batch: str (batch_id for polling indexing status)

        Raises:
            DifyAuthError: On 401/403 authentication failure
            DifyRateLimitError: On 429 rate limit
            DifyAPIError: On other 4xx/5xx errors
        """
        return await asyncio.to_thread(
            self.client.update_document_by_text,
            document_id=doc_id,
            name=name,
            text=content,
        )

    async def delete_document(
        self,
        dataset_id: str,
        doc_id: str,
    ) -> None:
        """Delete a document from the knowledge base asynchronously.

        Runs DifyKBClient.delete_document() in a thread pool
        to avoid blocking the event loop.

        Args:
            dataset_id: UUID of the knowledge base dataset (not used, for API compatibility).
            doc_id: UUID of the document to delete.

        Returns:
            None

        Raises:
            DifyAuthError: On 401/403 authentication failure
            DifyRateLimitError: On 429 rate limit
            DifyAPIError: On other 4xx/5xx errors
        """
        return await asyncio.to_thread(
            self.client.delete_document,
            document_id=doc_id,
        )
