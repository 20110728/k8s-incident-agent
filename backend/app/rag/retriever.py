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

        results = (
            self._vector_store
            .similarity_search_with_score(
                query=query,
                k=k,
            )
        )

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