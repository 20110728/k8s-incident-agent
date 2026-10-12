from typing import Any, Protocol

from langchain_postgres import PGVector


class RunbookRetrieverPort(Protocol):
    def retrieve(
        self,
        query: str,
        k: int = 3,
    ) -> list[dict[str, Any]]:
        ...


class PGVectorRunbookRetriever:
    def __init__(
        self,
        vector_store: PGVector,
    ) -> None:
        self._vector_store = vector_store

    def retrieve(
        self,
        query: str,
        k: int = 3,
    ) -> list[dict[str, Any]]:
        if not query.strip():
            raise ValueError(
                "retrieval query must not be blank"
            )

        from backend.app.runtime.budget import CURRENT
        budget = CURRENT.get()
        estimate = len(query.encode("utf-8")) + 512
        ticket = budget.reserve("embedding", tokens=estimate,
            metadata={"estimate_source": "utf8_bytes_plus_512", "usage_unavailable": True}) if budget else None
        try:
            results = self._vector_store.similarity_search_with_score(query=query, k=k)
        finally:
            if budget:
                budget.settle(ticket, 0, status="usage_unavailable")

        return [
            {
                "document_id": document.metadata.get(
                    "document_id"
                ),
                "runbook_id": document.metadata.get(
                    "runbook_id"
                ),
                "category": document.metadata.get(
                    "category"
                ),
                "title": document.metadata.get("title"),
                "section": document.metadata.get(
                    "section"
                ),
                "source": document.metadata.get("source"),
                "chunk_index": document.metadata.get(
                    "chunk_index"
                ),
                "content": document.page_content,
                "score": float(score),
            }
            for document, score in results
        ]
