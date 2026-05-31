"""Embedded ChromaDB PersistentClient wrapper using precomputed embeddings."""

from dataclasses import dataclass
from typing import Any

import chromadb
import chromadb.api
import chromadb.api.models.Collection


@dataclass
class QueryHit:
    id: str
    document: str
    metadata: dict[str, Any]
    distance: float


class VectorStore:
    """One persistent ChromaDB collection 'products' with cosine distance.

    Embeddings are computed by EmbeddingProvider and passed in; this class does
    not configure a Chroma embedding function. The doc id is the product
    external_key, so upsert handles both create and update.
    """

    COLLECTION: str = "products"

    _client: chromadb.api.ClientAPI
    _collection: chromadb.api.models.Collection.Collection

    def __init__(self, path: str) -> None:
        self._client = chromadb.PersistentClient(path=path)
        self._collection = self._client.get_or_create_collection(
            name=self.COLLECTION,
            metadata={"hnsw:space": "cosine"},
        )

    def upsert(
        self,
        id: str,
        document: str,
        embedding: list[float],
        metadata: dict[str, Any],
    ) -> None:
        self._collection.upsert(
            ids=[id],
            documents=[document],
            embeddings=[embedding],
            metadatas=[metadata],
        )

    def delete(self, ids: list[str]) -> None:
        if ids:
            _ = self._collection.delete(ids=ids)

    def get_by_product(self, store_id: str, product_id: str) -> list[QueryHit]:
        result = self._collection.get(
            where={"$and": [{"store_id": store_id}, {"product_id": product_id}]},
            include=["documents", "metadatas"],
        )
        ids = result.get("ids") or []
        documents = result.get("documents") or []
        metadatas = result.get("metadatas") or []
        return [
            QueryHit(
                id=ids[i],
                document=(documents[i] if i < len(documents) else "") or "",
                metadata=dict(metadatas[i] or {}) if i < len(metadatas) else {},
                distance=0.0,
            )
            for i in range(len(ids))
        ]

    def query(
        self,
        embedding: list[float],
        n_results: int,
        where: dict[str, Any] | None = None,
    ) -> list[QueryHit]:
        kwargs: dict[str, Any] = {
            "query_embeddings": [embedding],
            "n_results": n_results,
            "include": ["documents", "metadatas", "distances"],
        }
        if where is not None:
            kwargs["where"] = where
        result = self._collection.query(**kwargs)
        ids = (result.get("ids") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        return [
            QueryHit(
                id=ids[i],
                document=documents[i] or "",
                metadata=dict(metadatas[i] or {}),
                distance=float(distances[i]),
            )
            for i in range(len(ids))
        ]
