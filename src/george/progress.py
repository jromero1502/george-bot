"""Feedback inmediato al usuario mientras George trabaja.

Un solo mensaje de estado ("⏳ Revisando tu correo…") que se crea al primer
paso, se edita en los siguientes y se borra cuando llega la respuesta final.
El texto sale de las tools que de verdad se van a ejecutar (no de lo que el
modelo diga) y no cuesta ninguna llamada extra al modelo. Todo es best-effort:
un fallo de Telegram aquí nunca debe afectar el turno.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from george import telegram

logger = logging.getLogger("george.progress")

# Acuses del webhook, antes de encolar (no usan el modelo).
ACK_VOICE = "🎧 Voy a escuchar tu audio…"
ACK_PHOTO = "🖼️ Voy a revisar tu imagen…"
AFTER_TRANSCRIBE = "⏳ Ya lo procesé, estoy trabajando en tu mensaje…"

# Frase por tool. Las que no aparecen (get_current_datetime, confirm/cancel) son instantáneas: no muestran estado.
TOOL_STATUS: dict[str, str] = {
    "gmail_search": "📧 Revisando tu correo…",
    "gmail_read": "📧 Revisando tu correo…",
    "gmail_attachment_to_drive": "📁 Pasando los adjuntos a Drive…",
    "gmail_save_email_to_drive": "📁 Guardando el correo en Drive…",
    "calendar_list_events": "📅 Revisando tu agenda…",
    "drive_search": "📁 Buscando en Drive…",
    "drive_read": "📁 Leyendo el archivo de Drive…",
    "drive_send_file": "📎 Preparando el archivo para enviártelo…",
    "drive_create_folder": "📁 Creando la carpeta…",
    "search_clients": "👥 Consultando tus clientes…",
    "get_client": "👥 Consultando tus clientes…",
    "search_finance": "💰 Consultando los movimientos…",
    "get_client_balance": "💰 Consultando los movimientos…",
    "search_records": "🗂️ Consultando tus registros…",
    "summarize_records": "🗂️ Consultando tus registros…",
    "list_record_types": "🗂️ Consultando tus registros…",
    "list_reminders": "⏰ Revisando los recordatorios…",
    "list_pqrs": "📋 Revisando los PQRS…",
    "recall_chat_history": "💬 Releyendo la conversación de ese día…",
    "list_my_businesses": "🔄 Revisando tus negocios…",
    "switch_business": "🔄 Cambiando de negocio…",
    "switch_to_platform_admin": "🔄 Cambiando de modo…",
    "list_tenants": "🏢 Consultando la plataforma…",
    "get_chat_memberships": "🏢 Consultando la plataforma…",
    "get_default_reminder_templates": "🏢 Consultando la plataforma…",
    "propose_actions": "📝 Armando el plan…",
}


def describe(tool_names: Iterable[str]) -> Optional[str]:
    """Frase de estado para un grupo de tool calls (sin repetir), o None si ninguna la necesita."""
    phrases = list(dict.fromkeys(TOOL_STATUS[n] for n in tool_names if n in TOOL_STATUS))
    return " ".join(phrases) if phrases else None


class StatusMessage:
    def __init__(self, chat_id: str, reply_to: Optional[int] = None, message_id: Optional[int] = None):
        self.chat_id = chat_id
        self.reply_to = reply_to
        self.message_id = message_id  # el acuse del webhook, si lo hubo
        self._text: Optional[str] = None

    def show(self, text: str) -> None:
        if text == self._text:
            return
        try:
            if self.message_id is None:
                resp = telegram.send_message(self.chat_id, text, reply_to_message_id=self.reply_to)
                self.message_id = telegram.message_id_of(resp)
            else:
                telegram.edit_message_text(self.chat_id, self.message_id, text)
            self._text = text
        except Exception:  # noqa: BLE001 - el feedback nunca rompe el turno
            logger.exception("progress: no pude mostrar el estado en chat %s", self.chat_id)

    def on_tools(self, tool_names: Iterable[str]) -> None:
        text = describe(tool_names)
        if text:
            self.show(text)

    def clear(self) -> None:
        if self.message_id is None:
            return
        try:
            telegram.delete_message(self.chat_id, self.message_id)
        except Exception:  # noqa: BLE001
            logger.exception("progress: no pude borrar el mensaje de estado en chat %s", self.chat_id)
        self.message_id = None
        self._text = None
