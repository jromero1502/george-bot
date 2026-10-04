"""OCR for Telegram photos via Azure AI Document Intelligence (`prebuilt-read`).
Used to turn a photo (a receipt, a handwritten list, a note) into plain text
before handing it to the Haiku agent — same role as groq_stt.py for voice
notes: this module only extracts raw text, George does all the interpreting
(amounts, items, dates). Deliberately `prebuilt-read` and not
`prebuilt-receipt`: the platform is generic across business types (see
CLAUDE.md "Esquema de negocio genérico"), so a model specialized for one
document shape would fail on everything else a business might photograph.
"""
from __future__ import annotations

import io
import logging
import time
from typing import Optional

from azure.ai.documentintelligence import DocumentIntelligenceClient
from azure.identity import DefaultAzureCredential

from george.config import settings

logger = logging.getLogger("george.document_intelligence")

_client: Optional[DocumentIntelligenceClient] = None


def _get_client() -> DocumentIntelligenceClient:
    global _client
    if _client is None:
        _client = DocumentIntelligenceClient(
            settings.document_intelligence_endpoint, DefaultAzureCredential()
        )
    return _client


def extract_text(image_bytes: bytes) -> tuple[str, dict[str, float]]:
    """Returns (extracted_text, {"latencyMs": ..., "pages": ...}). The text is
    raw OCR output in reading order — may contain recognition errors (a
    smudged digit, a cut-off line); the agent prompt treats it accordingly
    rather than as already-confirmed data."""
    start = time.monotonic()
    client = _get_client()
    poller = client.begin_analyze_document("prebuilt-read", io.BytesIO(image_bytes))
    result = poller.result()
    latency_ms = (time.monotonic() - start) * 1000

    text = (result.content or "").strip()
    logger.info(
        "document_intelligence: %d bytes -> %d chars (%d pages) in %.0fms",
        len(image_bytes),
        len(text),
        len(result.pages or []),
        latency_ms,
    )
    return text, {"latencyMs": round(latency_ms, 1), "pages": len(result.pages or [])}
