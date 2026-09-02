from langchain_postgres import PGVector

from backend.app.rag.embeddings import build_embeddings
from backend.app.rag.settings import RagSettings


def build_vector_store(
    settings: RagSettings,
) -> PGVector:
    embeddings = build_embeddings(settings)

    return PGVector(
        embeddings=embeddings,
        collection_name=settings.runbook_collection,
        connection=settings.pgvector_url,
        use_jsonb=True,
    )