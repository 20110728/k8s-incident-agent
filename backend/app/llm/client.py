from langchain_openai import ChatOpenAI

from backend.app.rag.settings import RagSettings


def build_chat_model(
    settings: RagSettings,
) -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.llm_model,
        api_key=(
            settings.dashscope_api_key
            .get_secret_value()
        ),
        base_url=settings.dashscope_base_url,
        temperature=0,
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
        extra_body={
            "enable_thinking": False,
        },
    )