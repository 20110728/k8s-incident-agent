from pathlib import Path

import pytest
from langchain_core.documents import Document

from backend.app.agent.state import IncidentState
from backend.app.rag.loader import load_runbooks
from backend.app.rag.query_builder import (
    MAX_QUERY_CHARACTERS,
    build_retrieval_query,
)
from backend.app.rag.retriever import (
    PGVectorRunbookRetriever,
)


def test_load_runbooks_generates_stable_ids(
    tmp_path: Path,
):
    runbook_dir = (
        tmp_path
        / "runbooks"
        / "readiness"
    )
    runbook_dir.mkdir(parents=True)

    path = runbook_dir / "wrong-http-path.md"
    path.write_text(
        (
            "# Readiness Probe路径错误\n\n"
            "## 典型症状\n\n"
            "Pod处于Running但没有Ready。\n\n"
            "## 判断规则\n\n"
            "检查HTTP Probe返回状态码。\n"
        ),
        encoding="utf-8",
    )

    documents_1, ids_1 = load_runbooks(
        tmp_path / "runbooks"
    )
    documents_2, ids_2 = load_runbooks(
        tmp_path / "runbooks"
    )

    assert documents_1
    assert ids_1 == ids_2
    assert len(documents_1) == len(ids_1)

    metadata = documents_1[0].metadata

    assert metadata["runbook_id"] == (
        "wrong-http-path"
    )
    assert metadata["category"] == "readiness"
    assert metadata["source"] == (
        "readiness/wrong-http-path.md"
    )


def test_load_runbooks_rejects_missing_directory(
    tmp_path: Path,
):
    with pytest.raises(
        FileNotFoundError,
        match="runbook directory not found",
    ):
        load_runbooks(
            tmp_path / "does-not-exist"
        )


def test_retrieval_query_contains_relevant_evidence():
    state: IncidentState = {
        "request": {
            "namespace": "agent-demo",
            "service_name": "order-service",
            "description": "Pod无法Ready",
        },
        "evidence": [
            {
                "evidence_id": "ev-test-001",
                "resource_type": "PodStatus",
                "resource_name": "order-service-abc",
                "data": {
                    "phase": "Running",
                    "ready": False,
                },
            },
            {
                "evidence_id": "ev-test-002",
                "resource_type": "UnsupportedType",
                "resource_name": "ignored",
                "data": {
                    "value": "should-not-appear",
                },
            },
        ],
    }

    query = build_retrieval_query(state)

    assert "Pod无法Ready" in query
    assert "agent-demo/order-service" in query
    assert "PodStatus" in query
    assert '"ready": false' in query
    assert "should-not-appear" not in query
    assert len(query) <= MAX_QUERY_CHARACTERS


class FakeVectorStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def similarity_search_with_score(
        self,
        *,
        query: str,
        k: int,
    ):
        self.calls.append((query, k))

        return [
            (
                Document(
                    page_content="检查Readiness Probe路径。",
                    metadata={
                        "document_id": "doc-001",
                        "runbook_id": (
                            "wrong-http-path"
                        ),
                        "category": "readiness",
                        "source": (
                            "readiness/"
                            "wrong-http-path.md"
                        ),
                        "chunk_index": 0,
                    },
                ),
                0.12,
            )
        ]


def test_retriever_maps_vector_store_result():
    vector_store = FakeVectorStore()
    retriever = PGVectorRunbookRetriever(
        vector_store
    )

    result = retriever.retrieve(
        query="Pod无法Ready",
        k=3,
    )

    assert vector_store.calls == [
        ("Pod无法Ready", 3)
    ]
    assert len(result) == 1
    assert result[0]["runbook_id"] == (
        "wrong-http-path"
    )
    assert result[0]["score"] == 0.12
    assert "Readiness Probe" in result[0]["content"]