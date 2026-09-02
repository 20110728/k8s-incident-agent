from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from langchain_core.documents import Document
from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)


HEADERS = [
    ("#", "title"),
    ("##", "section"),
]


def load_runbooks(
    root: Path,
) -> tuple[list[Document], list[str]]:
    if not root.exists():
        raise FileNotFoundError(
            f"runbook directory not found: {root}"
        )

    header_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=HEADERS,
        strip_headers=False,
    )

    recursive_splitter = RecursiveCharacterTextSplitter(
        chunk_size=800,
        chunk_overlap=100,
        separators=[
            "\n## ",
            "\n\n",
            "\n",
            "。",
            "；",
            "，",
            " ",
            "",
        ],
        length_function=len,
    )

    documents: list[Document] = []
    document_ids: list[str] = []

    for path in sorted(root.rglob("*.md")):
        relative_path = path.relative_to(root)
        category = path.parent.name
        runbook_id = path.stem
        content = path.read_text(encoding="utf-8")

        if not content.strip():
            continue

        header_documents = header_splitter.split_text(
            content
        )
        chunks = recursive_splitter.split_documents(
            header_documents
        )

        for index, chunk in enumerate(chunks):
            content_hash = sha256(
                chunk.page_content.encode("utf-8")
            ).hexdigest()[:16]

            document_id = str(
                uuid5(
                    NAMESPACE_URL,
                    (
                        f"{relative_path}:"
                        f"{index}:{content_hash}"
                    ),
                )
            )

            chunk.metadata.update(
                {
                    "document_id": document_id,
                    "runbook_id": runbook_id,
                    "category": category,
                    "source": str(relative_path),
                    "chunk_index": index,
                }
            )

            documents.append(chunk)
            document_ids.append(document_id)

    if not documents:
        raise ValueError("no runbook chunks were loaded")

    return documents, document_ids