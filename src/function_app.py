"""George — all Azure Functions triggers (Python v2 programming model).

Flow: telegram_webhook (HTTP) validates + enqueues -> process_update (queue)
runs the agent and replies -> reminder_scheduler (timer) finds due
reminders and enqueues them -> reminder_worker (queue) sends them.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, List, Optional

import azure.functions as func

from george import telegram
from george.agent import generate_reminder_message, run_agent
from george.config import estimate_cost_usd, settings
from george.groq_stt import transcribe
from george import roles
from george.repositories import chats as chats_repo
from george.repositories import conversations as conversations_repo
from george.repositories import reminders as reminders_repo
from george.repositories import tenants as tenants_repo
from george.repositories.cosmos import get_database
from george.scheduling import next_state_after_fire
from george.tools.common import ToolContext
from george.util import new_id, utc_now, utc_now_iso

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("george")

app = func.FunctionApp()

_POISON_QUEUE = f"{settings.queue_incoming}-poison"
_STALE_REMINDER_SECONDS = 30 * 60


# ---------------------------------------------------------------------------
# Telegram webhook — HTTP-triggered, must return fast. Does auth + whitelist
# checks only, then hands off to the queue.
# ---------------------------------------------------------------------------
@app.route(route="telegram/{path}", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
@app.queue_output(arg_name="outMsg", queue_name=settings.queue_incoming, connection="AzureWebJobsStorage")
def telegram_webhook(req: func.HttpRequest, outMsg: func.Out[str]) -> func.HttpResponse:
    path = req.route_params.get("path")
    if path != settings.telegram_webhook_path:
        logger.warning("telegram_webhook: unknown path segment %r", path)
        return func.HttpResponse(status_code=404)

    if not telegram.verify_webhook_secret(req.headers.get("X-Telegram-Bot-Api-Secret-Token")):
        logger.warning("telegram_webhook: missing or invalid secret token header")
        return func.HttpResponse(status_code=401)

    try:
        update = req.get_json()
    except ValueError:
        logger.warning("telegram_webhook: request body is not valid JSON")
        return func.HttpResponse(status_code=400)

    message = update.get("message") or update.get("edited_message")
    if message is None:
        # Some other update type (callback_query, my_chat_member, ...) — ack, ignore.
        return func.HttpResponse(status_code=200)

    chat_id = str(message["chat"]["id"])
    if chats_repo.is_authorized(chat_id) is None:
        logger.info("telegram_webhook: chat %s is not on the whitelist, dropping", chat_id)
        # Still 200: Telegram would otherwise retry delivery of an update we
        # will never process.
        return func.HttpResponse(status_code=200)

    # Fire immediately so the user sees "escribiendo..." right away instead of
    # silence while the message sits in the queue — process_update sends it
    # again once it actually starts working, since Telegram's typing
    # indicator only lasts ~5s on its own. Best-effort: never let a Telegram
    # hiccup here stop the message from being enqueued.
    try:
        telegram.send_chat_action(chat_id, "typing")
    except Exception:
        logger.exception("telegram_webhook: failed to send early typing action to chat %s", chat_id)

    queue_payload = {
        "updateId": update.get("update_id"),
        "chatId": chat_id,
        "message": message,
    }
    # Test hook (scripts/simulate_update.py --voice): a synthetic update can
    # carry a local file path for the voice note, bypassing Telegram's
    # getFile/download entirely — see process_update below.
    if message.get("localAudioPath"):
        queue_payload["localAudioPath"] = message["localAudioPath"]

    outMsg.set(json.dumps(queue_payload))
    return func.HttpResponse(status_code=200)


# ---------------------------------------------------------------------------
# Background processing of one inbound Telegram message.
# ---------------------------------------------------------------------------
def _resolve_role_and_tenant(
    chat_id: str, chat: dict[str, Any]
) -> tuple[Optional[str], Optional[str], Optional[dict[str, Any]], Optional[str]]:
    """Returns (role, tenant_id, tenant, error_message). When error_message
    is not None, the caller should send it to the chat and stop — the other
    three fields are meaningless in that case.

    A chat can belong to several tenants; this resolves which one the
    conversation is operating against right now:
      - isPlatformAdmin -> role=platform_admin, no tenant, UNLESS this chat
        also has a membership of its own and explicitly switched into it
        (activeTenantId set) — dual role, see tools/membership.py's
        switch_business / switch_to_platform_admin. Default stays platform
        mode; a platform_admin chat never auto-activates a business.
      - No memberships -> not provisioned into any business yet.
      - Exactly one membership -> auto-selected (and persisted), no
        disambiguation needed — this keeps the common case frictionless.
      - Several memberships, none active yet -> PENDING_SELECTION_ROLE; the
        agent's job for this turn is just to ask which one and call
        switch_business (see prompts.py / tools/membership.py).
    """
    if chat.get("isPlatformAdmin"):
        active_tenant_id = chat.get("activeTenantId")
        if active_tenant_id:
            active_membership = chats_repo.get_membership(chat, active_tenant_id)
            if active_membership is not None:
                tenant = tenants_repo.get_active_tenant(active_membership["tenantId"])
                if tenant is not None:
                    return active_membership["role"], active_membership["tenantId"], tenant, None
                # The switched-to tenant is gone/suspended — fall back to
                # platform mode below rather than error out a platform_admin,
                # who always has a valid identity to fall back to.
        return roles.PLATFORM_ADMIN_ROLE, None, None, None

    memberships = chat.get("memberships") or []
    if not memberships:
        return (
            None,
            None,
            None,
            "Tu chat no está asociado a ningún negocio todavía. Contacta al administrador de la plataforma.",
        )

    active_tenant_id = chat.get("activeTenantId")
    active_membership = chats_repo.get_membership(chat, active_tenant_id) if active_tenant_id else None

    if active_membership is None and len(memberships) == 1:
        active_membership = memberships[0]
        try:
            chats_repo.set_active_tenant(chat_id, active_membership["tenantId"])
        except ValueError:
            logger.exception("process_update: failed to auto-set active tenant for chat %s", chat_id)

    if active_membership is None:
        return roles.PENDING_SELECTION_ROLE, None, None, None

    tenant = tenants_repo.get_active_tenant(active_membership["tenantId"])
    if tenant is None:
        return (
            None,
            None,
            None,
            "El negocio activo de tu chat ya no está disponible (fue suspendido o eliminado). "
            "Contacta al administrador de la plataforma.",
        )

    return active_membership["role"], active_membership["tenantId"], tenant, None


def _history_to_messages(history_docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turns stored `conversations` docs (inbound turns only) into an
    alternating user/assistant message list for the Anthropic API."""
    messages: list[dict[str, Any]] = []
    for doc in history_docs:
        if doc.get("direction") == "outbound":
            continue
        user_text = (doc.get("input") or {}).get("transcript") or (doc.get("input") or {}).get("text")
        assistant_text = (doc.get("output") or {}).get("text")
        if user_text:
            messages.append({"role": "user", "content": user_text})
        if assistant_text:
            messages.append({"role": "assistant", "content": assistant_text})
    return messages


@app.queue_trigger(arg_name="msg", queue_name=settings.queue_incoming, connection="AzureWebJobsStorage")
def process_update(msg: func.QueueMessage) -> None:
    payload = json.loads(msg.get_body().decode("utf-8"))
    chat_id = payload["chatId"]
    update_id = payload.get("updateId")
    message = payload["message"]
    correlation_id = new_id()

    if update_id is not None and conversations_repo.already_processed(chat_id, update_id):
        logger.info("process_update: update %s for chat %s already processed, skipping", update_id, chat_id)
        return

    chat = chats_repo.is_authorized(chat_id)
    if chat is None:
        logger.warning("process_update: chat %s no longer authorized, dropping", chat_id)
        return

    role, tenant_id, tenant, error_message = _resolve_role_and_tenant(chat_id, chat)
    if error_message:
        logger.warning("process_update: chat %s could not resolve role/tenant: %s", chat_id, error_message)
        telegram.send_message(chat_id, error_message)
        return

    user_name = (message.get("from") or {}).get("first_name") or chat.get("name") or chat_id
    ctx = ToolContext(
        chat_id=chat_id,
        role=role,
        user_name=user_name,
        correlation_id=correlation_id,
        tenant_id=tenant_id,
        tenant=tenant,
        is_platform_admin=bool(chat.get("isPlatformAdmin")),
    )

    telegram.send_chat_action(chat_id, "typing")

    input_record: dict[str, Any]
    stt_record: Optional[dict[str, Any]] = None
    user_text: Optional[str]

    if "voice" in message:
        voice = message["voice"]
        input_record = {
            "type": "voice",
            "text": None,
            "audio": {
                "fileId": voice.get("file_id"),
                "durationSec": voice.get("duration"),
                "sizeBytes": voice.get("file_size"),
            },
        }
        # Test hook: scripts/simulate_update.py can point at a local audio
        # file directly, bypassing Telegram's getFile/download entirely.
        local_path = payload.get("localAudioPath")
        if local_path:
            with open(local_path, "rb") as f:
                audio_bytes = f.read()
        else:
            file_path = telegram.get_file(voice["file_id"])
            audio_bytes = telegram.download_file(file_path)

        user_text, stt_meta = transcribe(audio_bytes)
        stt_record = {
            "provider": "groq",
            "model": settings.groq_stt_model,
            "durationSec": voice.get("duration"),
            **stt_meta,
        }
        input_record["transcript"] = user_text
    elif "text" in message:
        user_text = message["text"]
        input_record = {"type": "text", "text": user_text, "transcript": None}
    else:
        telegram.send_message(chat_id, "Por ahora solo puedo procesar mensajes de texto o notas de voz.")
        return

    if not user_text or not user_text.strip():
        telegram.send_message(chat_id, "No logré entender el mensaje, ¿puedes intentarlo de nuevo?")
        return

    history_docs = conversations_repo.get_recent(chat_id, settings.history_turns)
    history_messages = _history_to_messages(history_docs)

    result = run_agent(ctx, history_messages, user_text)

    telegram.send_message(chat_id, result.reply_text, reply_to_message_id=message.get("message_id"))

    conversations_repo.create_conversation(
        chat_id,
        tenantId=tenant_id,
        userId=str((message.get("from") or {}).get("id", chat_id)),
        userName=user_name,
        role=role,
        direction="inbound",
        input=input_record,
        output={"text": result.reply_text},
        llm={
            "provider": "anthropic",
            "model": settings.anthropic_model,
            "inputTokens": result.input_tokens,
            "outputTokens": result.output_tokens,
            "cacheReadTokens": 0,
            "cacheCreationTokens": 0,
            "turns": result.turns,
            "stopReason": result.stop_reason,
            "latencyMs": round(result.latency_ms, 1),
            "costUsd": round(estimate_cost_usd(result.input_tokens, result.output_tokens), 6),
        },
        stt=stt_record,
        toolCalls=result.tool_calls,
        trace={
            "correlationId": correlation_id,
            "updateId": update_id,
            "attempt": msg.dequeue_count,
        },
        error=None,
    )


# ---------------------------------------------------------------------------
# Reminder scheduler — timer trigger, runs every 10 minutes. Finds reminders
# whose nextRunAt has passed, advances each one exactly once (ETag-guarded),
# and hands off the actual send to reminder_worker via the queue.
# ---------------------------------------------------------------------------
def _run_reminder_scan(out: func.Out) -> int:
    now_iso = utc_now_iso()
    due = reminders_repo.get_due(now_iso)
    queued: list[str] = []

    for reminder in due:
        try:
            updates = next_state_after_fire(reminder)
        except ValueError:
            logger.exception("reminder_scheduler: invalid schedule on reminder %s, skipping", reminder["id"])
            continue

        advanced = reminders_repo.try_claim_and_advance(reminder, updates)
        if advanced is None:
            # Another concurrent scan already claimed this firing — skip.
            continue

        queued.append(
            json.dumps(
                {
                    "reminderId": reminder["id"],
                    "tenantId": reminder.get("tenantId"),
                    "name": reminder.get("name"),
                    "kind": reminder.get("kind"),
                    "body": reminder.get("body"),
                    "target": reminder.get("target"),
                    "scheduledFor": now_iso,
                }
            )
        )

    if queued:
        out.set(queued)
    return len(queued)


@app.timer_trigger(schedule="0 */10 * * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
@app.queue_output(arg_name="outMsgs", queue_name=settings.queue_reminders, connection="AzureWebJobsStorage")
def reminder_scheduler(timer: func.TimerRequest, outMsgs: func.Out[List[str]]) -> None:
    count = _run_reminder_scan(outMsgs)
    if count:
        logger.info("reminder_scheduler: queued %d reminder(s)", count)


@app.route(route="ops/run-reminders", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
@app.queue_output(arg_name="outMsgs", queue_name=settings.queue_reminders, connection="AzureWebJobsStorage")
def run_reminders_now(req: func.HttpRequest, outMsgs: func.Out[List[str]]) -> func.HttpResponse:
    count = _run_reminder_scan(outMsgs)
    return func.HttpResponse(json.dumps({"queued": count}), mimetype="application/json")


# ---------------------------------------------------------------------------
# Reminder worker — actually sends a due reminder to its target chat(s).
# ---------------------------------------------------------------------------
def _resolve_target(tenant_id: Optional[str], target: dict[str, Any]) -> list[str]:
    if target.get("type") == "chat" and target.get("chatId"):
        return [target["chatId"]]
    if target.get("type") == "role" and target.get("role") and tenant_id:
        return [c["chatId"] for c in chats_repo.list_chats_by_role(tenant_id, target["role"])]
    return []


@app.queue_trigger(arg_name="msg", queue_name=settings.queue_reminders, connection="AzureWebJobsStorage")
def reminder_worker(msg: func.QueueMessage) -> None:
    payload = json.loads(msg.get_body().decode("utf-8"))
    reminder_id = payload["reminderId"]
    scheduled_for = payload.get("scheduledFor")

    if scheduled_for:
        fired_at = datetime.fromisoformat(scheduled_for.replace("Z", "+00:00"))
        if (utc_now() - fired_at).total_seconds() > _STALE_REMINDER_SECONDS:
            logger.warning(
                "reminder_worker: dropping stale reminder %s (scheduled for %s)", reminder_id, scheduled_for
            )
            return

    chat_ids = _resolve_target(payload.get("tenantId"), payload.get("target", {}))
    if not chat_ids:
        logger.warning(
            "reminder_worker: no chats resolved for reminder %s (target=%s)", reminder_id, payload.get("target")
        )
        reminders_repo.record_run_result(reminder_id, ok=False)
        return

    body = payload.get("body", "")
    if payload.get("kind") == "prompt":
        tenant = tenants_repo.get_tenant(payload["tenantId"]) if payload.get("tenantId") else None
        text = generate_reminder_message(body, tenant)
    else:
        text = body

    ok = True
    for chat_id in chat_ids:
        try:
            telegram.send_message(chat_id, text)
            conversations_repo.create_conversation(
                chat_id,
                tenantId=payload.get("tenantId"),
                userId="system",
                userName="George",
                role="system",
                direction="outbound",
                input={"type": "reminder", "text": None, "transcript": None},
                output={"text": text},
                trace={"correlationId": new_id(), "reminderId": reminder_id},
            )
        except Exception:
            logger.exception("reminder_worker: failed sending reminder %s to chat %s", reminder_id, chat_id)
            ok = False

    reminders_repo.record_run_result(reminder_id, ok=ok)


# ---------------------------------------------------------------------------
# Poison queue handler — a message that failed 3 times (host.json
# maxDequeueCount) lands here. Notify the owner so it doesn't just vanish.
# ---------------------------------------------------------------------------
@app.queue_trigger(arg_name="msg", queue_name=_POISON_QUEUE, connection="AzureWebJobsStorage")
def poison_handler(msg: func.QueueMessage) -> None:
    body = msg.get_body().decode("utf-8", errors="replace")
    logger.error("poison_handler: message dead-lettered after retries: %s", body)

    try:
        payload = json.loads(body)
        chat_id = payload.get("chatId", "desconocido")
    except (ValueError, AttributeError):
        chat_id = "desconocido"

    telegram.send_message(
        settings.platform_admin_chat_id,
        f"⚠️ Un mensaje del chat {chat_id} falló al procesarse después de varios intentos. Revisa los logs.",
    )


# ---------------------------------------------------------------------------
# Health check.
# ---------------------------------------------------------------------------
@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def health(req: func.HttpRequest) -> func.HttpResponse:
    cosmos_ok = True
    try:
        get_database().read()
    except Exception:
        logger.exception("health: Cosmos DB check failed")
        cosmos_ok = False

    status = "ok" if cosmos_ok else "degraded"
    return func.HttpResponse(
        json.dumps({"status": status, "cosmos": cosmos_ok}),
        status_code=200 if cosmos_ok else 503,
        mimetype="application/json",
    )
