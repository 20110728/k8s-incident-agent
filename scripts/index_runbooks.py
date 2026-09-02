from pathlib import Path

from backend.app.rag.loader import load_runbooks
from backend.app.rag.settings import get_rag_settings
from backend.app.rag.vector_store import (
    build_vector_store,
)


RUNBOOK_ROOT = Path("knowledge/runbooks")


def main() -> None:
    settings = get_rag_settings()
    documents, document_ids = load_runbooks(
        RUNBOOK_ROOT
    )

    vector_store = build_vector_store(settings)

    result_ids = vector_store.add_documents(
        documents=documents,
        ids=document_ids,
    )

    print(f"runbook files: {len(list(RUNBOOK_ROOT.rglob('*.md')))}")
    print(f"indexed chunks: {len(result_ids)}")
    print(f"collection: {settings.runbook_collection}")


if __name__ == "__main__":
    main()