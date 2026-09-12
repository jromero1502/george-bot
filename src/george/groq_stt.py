"""Speech-to-text via Groq's hosted whisper-large-v3-turbo. Used to transcribe
Telegram voice notes before handing the text to the Haiku agent.
"""
from __future__ import annotations

import logging
import time

import httpx

from george.config import settings

logger = logging.getLogger("george.groq_stt")

_TRANSCRIPTION_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


def transcribe(
    audio_bytes: bytes,
    filename: str = "voice.ogg",
    mime_type: str = "audio/ogg",
    language: str = "es",
) -> tuple[str, dict[str, float]]:
    """Returns (transcript, {"latencyMs": ...})."""
    headers = {"Authorization": f"Bearer {settings.groq_api_key}"}
    files = {"file": (filename, audio_bytes, mime_type)}
    data = {
        "model": settings.groq_stt_model,
        "language": language,
        "response_format": "json",
        "temperature": 0,
    }

    start = time.monotonic()
    with httpx.Client(timeout=60.0) as client:
        response = client.post(_TRANSCRIPTION_URL, headers=headers, files=files, data=data)
        response.raise_for_status()
        payload = response.json()
    latency_ms = (time.monotonic() - start) * 1000

    transcript = (payload.get("text") or "").strip()
    logger.info(
        "groq_stt: %d bytes -> %d chars in %.0fms", len(audio_bytes), len(transcript), latency_ms
    )
    return transcript, {"latencyMs": round(latency_ms, 1)}
