import uuid
import os
import asyncio
import aiofiles
import PyPDF2
from io import BytesIO
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from app.database import SessionLocal
from app.models import DocumentData
from app.utils.llm_chat import ask_llm
from app.logger import setup_logger

logger = setup_logger(__name__)


async def process_document(message: dict):
    """
    Main document processing pipeline:
    1. Extract file path, name, and role from message.
    2. Read file content (PDF).
    3. Chunk content into ~100-word blocks at sentence boundaries.
    4. Enrich each chunk with keywords and summary using LLM.
    5. Store enriched chunks in the database.
    """
    try:
        file_path = message["file_path"]
        original_name = message["original_name"]
        role = message.get("role_required", "Analyst")
    except KeyError as e:
        logger.error(f"Missing expected key in message: {e}")
        return

    logger.info(f"Started processing: {original_name}")

    try:
        content = await read_file_content(file_path)
        logger.info("File content read successfully.")
    except Exception as e:
        logger.exception(f"Failed to read file content: {e}")
        return

    chunks = await chunk_content(content)
    if not chunks:
        logger.warning("No chunks were created from the document.")
        return
    logger.info(f"Chunked content into {len(chunks)} blocks.")

    enriched_chunks = await enrich_chunks_with_llm(chunks)
    logger.info(f"Enriched {len(enriched_chunks)} chunks with LLM.")

    try:
        await store_chunks_in_db(enriched_chunks, original_name, role)
        logger.info(f"Stored chunks for '{original_name}' in database.")
    except Exception as e:
        logger.error(f"Failed to store chunks for '{original_name}': {e}")
        return

    logger.info(f"Completed processing: {original_name}")


async def read_file_content(file_path: str) -> str:
    """
    Reads PDF file content using PyPDF2.
    - Opens file in binary mode asynchronously.
    - Extracts and joins text from all pages.
    """
    try:
        async with aiofiles.open(file_path, 'rb') as f:
            data = await f.read()
    except FileNotFoundError:
        logger.error(f"File not found: {file_path}")
        raise
    except Exception as e:
        logger.error(f"Error reading file: {e}")
        raise

    try:
        reader = PyPDF2.PdfReader(BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as e:
        logger.error(f"Error parsing PDF content: {e}")
        raise


async def chunk_content(content: str) -> list[str]:
    """
    Splits content into chunks of ~100 words, ending at sentence boundaries.
    - Ensures each chunk ends at a '.' and contains at least 100 words.
    - Appends remaining text as a final chunk.
    """
    MIN_WORDS = 100
    chunks, current_chunk = [], []
    word_count = 0

    for word in content.split():
        current_chunk.append(word)
        word_count += 1

        if word.endswith('.') and word_count >= MIN_WORDS:
            chunks.append(" ".join(current_chunk).strip())
            current_chunk, word_count = [], 0

    if current_chunk:
        chunks.append(" ".join(current_chunk).strip())

    return chunks


async def enrich_chunks_with_llm(chunks: list[str]) -> list[dict]:
    """
    Enriches each chunk with keywords and a summary using LLM.
    - Handles individual chunk failures gracefully.
    - Returns a list of dicts with: content, keywords, summary.
    """
    enriched = []

    for i, chunk in enumerate(chunks, start=1):
        try:
            llm_response = await ask_llm(
                question="Generate 3 keywords and a 1-line summary.",
                context=chunk,
                response_format={
                    "keywords": ["k1", "k2", "k3"],
                    "summary": "one-line summary"
                }
            )
            enriched.append({
                "content": chunk,
                "keywords": ",".join(llm_response.get("keywords", [])),
                "summary": llm_response.get("summary", "")
            })
        except Exception as e:
            logger.error(f"LLM enrichment failed for chunk #{i}: {e}")
            enriched.append({
                "content": chunk,
                "keywords": "",
                "summary": ""
            })

    return enriched


async def store_chunks_in_db(enriched_chunks: list[dict], document_name: str, role: str):
    """
    Stores enriched chunks into the document_data table.
    - Commits all chunks in a single transaction.
    - Handles and logs database errors.
    """
    db: Session = SessionLocal()
    try:
        db_chunks = [
            DocumentData(
                chunk_id=uuid.uuid4(),
                document_name=document_name,
                role=role,
                chunk_number=i + 1,
                chunk_content=item["content"],
                keywords=item.get("keywords", ""),
                summary=item.get("summary", "")
            )
            for i, item in enumerate(enriched_chunks)
        ]

        db.add_all(db_chunks)
        db.commit()
    except SQLAlchemyError as db_err:
        db.rollback()
        logger.exception(f"Database error while storing chunks: {db_err}")
        raise
    except Exception as e:
        db.rollback()
        logger.exception(f"Unexpected error during DB insert: {e}")
        raise
    finally:
        db.close()
