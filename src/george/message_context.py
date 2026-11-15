"""What a Telegram message REFERS to: the message it replies to (or quotes) and
forwarded-message provenance. Pure (no Cosmos/Telegram), like scheduling.py.

Why: people answer "sí", "ese no", "el de arriba" by replying to a specific
message. Telegram sends that message inside the update (`reply_to_message`), but
the bot ignored it, so George saw a bare "sí" and had to guess which of the last
12 turns it meant (or, in a busy group, which of several people's messages).

The result is a short prefix for what the model reads — never for what is
stored as `input.text`; the rendered context is saved separately so the history
sent to the model on later turns keeps the same meaning.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

MAX_QUOTED_CHARS = 1200

# Telegram media keys -> how we call them to the model.
_KINDS = (
    ("voice", "nota de voz"),
    ("photo", "foto"),
    ("document", "archivo"),
    ("audio", "audio"),
    ("video", "video"),
    ("video_note", "video"),
    ("sticker", "sticker"),
)


def kind_of(message: dict[str, Any]) -> str:
    for key, label in _KINDS:
        if key in message:
            return label
    return "texto"


def needs_lookup(message: dict[str, Any]) -> bool:
    """Voice/photo carry no text in Telegram's payload — their transcript/OCR only
    exists in our own `conversations` (looked up by message id)."""
    return "voice" in message or "photo" in message


def _author(message: dict[str, Any]) -> str:
    sender = message.get("from") or {}
    if sender.get("is_bot"):
        return "George (tú)"
    return sender.get("first_name") or (message.get("sender_chat") or {}).get("title") or "alguien"


def _when(unix: Optional[int], tz_name: str) -> Optional[str]:
    if not unix:
        return None
    try:
        zone = ZoneInfo(tz_name)
    except Exception:
        zone = timezone.utc
    return datetime.fromtimestamp(int(unix), tz=timezone.utc).astimezone(zone).strftime("%Y-%m-%d %H:%M")


def _clip(text: str) -> str:
    text = " ".join(text.split())  # one line: a quoted message can't fake a new section
    return text if len(text) <= MAX_QUOTED_CHARS else text[:MAX_QUOTED_CHARS] + "…"


def _forward_source(message: dict[str, Any]) -> Optional[str]:
    origin = message.get("forward_origin")
    if isinstance(origin, dict):
        sender = origin.get("sender_user") or {}
        return (
            sender.get("first_name")
            or origin.get("sender_user_name")
            or (origin.get("chat") or {}).get("title")
            or (origin.get("sender_chat") or {}).get("title")
            or "alguien (oculto)"
        )
    # legacy fields, still sent by some clients
    legacy = message.get("forward_from") or {}
    return (
        legacy.get("first_name")
        or message.get("forward_sender_name")
        or (message.get("forward_from_chat") or {}).get("title")
        or (None if not any(k.startswith("forward_") for k in message) else "alguien (oculto)")
    )


def reference_context(
    message: dict[str, Any], tz_name: str = "America/Bogota", looked_up_text: Optional[str] = None
) -> Optional[str]:
    """One line describing what `message` points at, or None if it points at nothing.

    `looked_up_text` is the transcript/OCR of the replied-to message when it was a
    voice note or photo we processed earlier (see conversations_repo.find_by_message_id)."""
    parts: list[str] = []

    source = _forward_source(message)
    if source:
        parts.append(f"Mensaje reenviado, originalmente de {source}")

    replied = message.get("reply_to_message")
    if isinstance(replied, dict):
        kind = kind_of(replied)
        # our stored transcript/OCR beats Telegram's payload: for a photo it has
        # the caption AND the OCR text, for a voice note it is the only text there is
        text = looked_up_text or replied.get("text") or replied.get("caption")
        when = _when(replied.get("date"), tz_name)
        head = f"Responde a un mensaje de {_author(replied)}" + (f" del {when}" if when else "") + f" ({kind})"
        quote = (message.get("quote") or {}).get("text")
        if quote:
            head += f"; citó este fragmento: «{_clip(quote)}»"
        elif text:
            head += f": «{_clip(text)}»"
        elif kind != "texto":
            head += ", sin texto disponible"
        parts.append(head)

    if not parts:
        return None
    return "[Contexto: " + ". ".join(parts) + "]"


def with_context(text: str, context: Optional[str]) -> str:
    return f"{context}\n{text}" if context else text
