from langchain_openai import OpenAIEmbeddings

from backend.app.rag.settings import RagSettings


def build_embeddings(
    settings: RagSettings,
) -> OpenAIEmbeddings:
    from backend.app.runtime.budget import CURRENT
    budgeted = CURRENT.get() is not None
    return OpenAIEmbeddings(
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        api_key=(
            settings.dashscope_api_key.get_secret_value()
        ),
        base_url=settings.dashscope_base_url,
        chunk_size=20,
        max_retries=0 if budgeted else 2,
        **({"request_timeout": 25} if budgeted else {}),
        check_embedding_ctx_length=False,
    )
