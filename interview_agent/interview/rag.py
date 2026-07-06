"""Resume ingestion and retrieval: PDF → markdown → chunks → Qdrant.

Chunks carry `conversation_id` in their payload; every search filters on it,
so one collection safely holds all interviews' resumes.
"""

from __future__ import annotations

import io
import uuid

from langchain_openai import OpenAIEmbeddings
from markitdown import MarkItDown
from pydantic import SecretStr
from qdrant_client import AsyncQdrantClient, models

from interview_agent.config import Settings

_MAX_CHUNK_CHARS = 1200

# One instance for the process: construction scans converter plugins, and
# convert() itself is stateless per call.
_MARKITDOWN = MarkItDown()


def pdf_to_markdown(data: bytes, filename: str = "resume.pdf") -> str:
    """Convert an uploaded PDF to markdown text.

    CPU-bound, pure-Python (pdfminer/pdfplumber): call it via
    anyio.to_thread.run_sync from async code or it stalls the event loop —
    ~0.2-1s for a normal resume, tens of seconds for a dense 10 MB PDF.
    """
    stream = io.BytesIO(data)
    stream.name = filename  # helps markitdown pick the PDF converter
    result = _MARKITDOWN.convert(stream)
    return result.text_content


def chunk_markdown(markdown: str) -> list[str]:
    """Split on markdown headings, then greedily merge paragraphs so each
    chunk stays under ~1200 chars. Resumes are tiny; no overlap needed."""
    # Break into blocks: a heading starts a new block boundary.
    blocks: list[str] = []
    current: list[str] = []
    for line in markdown.splitlines():
        if line.lstrip().startswith("#") and current:
            blocks.append("\n".join(current).strip())
            current = []
        current.append(line)
    if current:
        blocks.append("\n".join(current).strip())

    # Greedily merge consecutive blocks up to the size cap; split oversized
    # blocks on blank lines.
    chunks: list[str] = []
    buffer = ""
    for block in blocks:
        if not block:
            continue
        pieces = [block] if len(block) <= _MAX_CHUNK_CHARS else block.split("\n\n")
        for piece in pieces:
            piece = piece.strip()
            if not piece:
                continue
            if buffer and len(buffer) + len(piece) + 2 > _MAX_CHUNK_CHARS:
                chunks.append(buffer)
                buffer = piece
            else:
                buffer = f"{buffer}\n\n{piece}" if buffer else piece
    if buffer:
        chunks.append(buffer)
    return chunks


def build_embeddings(settings: Settings) -> OpenAIEmbeddings:
    return OpenAIEmbeddings(
        model=settings.embedding_model,
        api_key=SecretStr(settings.openai_api_key),
    )


async def ensure_collection(client: AsyncQdrantClient, settings: Settings) -> None:
    """Create the collection + the conversation_id payload index if missing."""
    if not await client.collection_exists(settings.qdrant_collection):
        await client.create_collection(
            collection_name=settings.qdrant_collection,
            vectors_config=models.VectorParams(
                size=settings.embedding_dimensions, distance=models.Distance.COSINE
            ),
        )
        await client.create_payload_index(
            collection_name=settings.qdrant_collection,
            field_name="conversation_id",
            field_schema=models.PayloadSchemaType.KEYWORD,
        )


async def index_resume(
    client: AsyncQdrantClient,
    embeddings: OpenAIEmbeddings,
    settings: Settings,
    conversation_id: uuid.UUID,
    resume_markdown: str,
) -> int:
    """Chunk, embed and upsert one resume. Returns the number of chunks."""
    chunks = chunk_markdown(resume_markdown)
    vectors = await embeddings.aembed_documents(chunks)
    await client.upsert(
        collection_name=settings.qdrant_collection,
        points=[
            models.PointStruct(
                id=str(uuid.uuid4()),
                vector=vector,
                payload={
                    "conversation_id": str(conversation_id),
                    "chunk_index": i,
                    "text": chunk,
                },
            )
            for i, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True))
        ],
    )
    return len(chunks)


async def search_resume_chunks(
    client: AsyncQdrantClient,
    embeddings: OpenAIEmbeddings,
    settings: Settings,
    conversation_id: uuid.UUID,
    query: str,
    limit: int = 4,
) -> list[str]:
    """Semantic search over one conversation's resume chunks."""
    vector = await embeddings.aembed_query(query)
    response = await client.query_points(
        collection_name=settings.qdrant_collection,
        query=vector,
        query_filter=models.Filter(
            must=[
                models.FieldCondition(
                    key="conversation_id",
                    match=models.MatchValue(value=str(conversation_id)),
                )
            ]
        ),
        limit=limit,
        with_payload=True,
    )
    return [
        str(point.payload["text"])
        for point in response.points
        if point.payload and "text" in point.payload
    ]


async def delete_resume_points(
    client: AsyncQdrantClient,
    settings: Settings,
    conversation_ids: list[uuid.UUID],
) -> None:
    """Remove the resume chunks of the given conversations (PII cleanup).

    Used both right after an evaluation (the chunks are only needed while
    the interviewer can call search_resume) and by the retention purge.
    """
    if not conversation_ids:
        return
    await client.delete(
        collection_name=settings.qdrant_collection,
        points_selector=models.FilterSelector(
            filter=models.Filter(
                must=[
                    models.FieldCondition(
                        key="conversation_id",
                        match=models.MatchAny(any=[str(c) for c in conversation_ids]),
                    )
                ]
            )
        ),
    )
