"""Planes de acción pendientes y locks por tenant.

Ambos viven en el contenedor `platformConfig` (partition key /id) para no
tocar la infraestructura: son documentos pequeños y de vida corta, y siempre
se leen por id exacto (point-read), nunca por query.

- `plan::<chatId>`: el plan que George propuso y espera confirmación. Uno por
  chat; proponer otro reemplaza el anterior. Estados:
  pending -> confirmed -> running -> done | (cancelled desde pending).
- `lock::<clave>`: lock con expiración (lease) para procesar de a un mensaje
  por tenant. Si el proceso que lo tiene muere, vence solo.
"""
from __future__ import annotations

import contextlib
import logging
import time
from datetime import datetime, timedelta
from typing import Any, Iterator, Optional

from azure.core import MatchConditions
from azure.cosmos import exceptions

from george.repositories.cosmos import get_container
from george.util import new_id, utc_now, utc_now_iso

logger = logging.getLogger("george.plans")

PLAN_TTL = timedelta(hours=2)
LOCK_TTL_SECONDS = 300  # > duración máxima razonable de un turno (functionTimeout 10 min es el techo duro)
LOCK_WAIT_SECONDS = 150
_LOCK_POLL_SECONDS = 1.5


def _container():
    return get_container("platformConfig")


# ---------------------------------------------------------------- plans
def _plan_id(chat_id: str) -> str:
    return f"plan::{chat_id}"


def save_plan(
    chat_id: str,
    tenant_id: Optional[str],
    role: str,
    user_name: str,
    summary: str,
    actions: list[dict[str, Any]],
) -> dict[str, Any]:
    now = utc_now()
    doc = {
        "id": _plan_id(chat_id),
        "docType": "plan",
        "planId": new_id(),
        "chatId": chat_id,
        "tenantId": tenant_id,
        "role": role,
        "userName": user_name,
        "summary": summary,
        "actions": actions,
        "status": "pending",
        "createdAt": now.isoformat(timespec="seconds"),
        "expiresAt": (now + PLAN_TTL).isoformat(timespec="seconds"),
    }
    return _container().upsert_item(doc)


def get_plan(chat_id: str) -> Optional[dict[str, Any]]:
    try:
        return _container().read_item(item=_plan_id(chat_id), partition_key=_plan_id(chat_id))
    except exceptions.CosmosResourceNotFoundError:
        return None


def is_expired(plan: dict[str, Any]) -> bool:
    try:
        return datetime.fromisoformat(plan["expiresAt"]) < utc_now()
    except (KeyError, ValueError):
        return True


def get_pending_plan(chat_id: str) -> Optional[dict[str, Any]]:
    """El plan que espera confirmación, o None si no hay / venció."""
    plan = get_plan(chat_id)
    if plan is None or plan.get("status") != "pending" or is_expired(plan):
        return None
    return plan


def transition(plan: dict[str, Any], status: str, **fields: Any) -> Optional[dict[str, Any]]:
    """Cambia el estado con ETag: devuelve el doc nuevo, o None si otro
    escritor se adelantó (así un plan nunca se ejecuta dos veces)."""
    merged = {**plan, **fields, "status": status, "updatedAt": utc_now_iso()}
    try:
        return _container().replace_item(
            item=plan["id"], body=merged, etag=plan.get("_etag"), match_condition=MatchConditions.IfNotModified
        )
    except exceptions.CosmosAccessConditionFailedError:
        return None


# ---------------------------------------------------------------- locks
class LockBusy(Exception):
    """No se pudo tomar el lock del tenant dentro del tiempo de espera."""


def _lock_id(key: str) -> str:
    return f"lock::{key}"


def _try_acquire(key: str, owner: str) -> bool:
    expires = time.time() + LOCK_TTL_SECONDS
    doc = {"id": _lock_id(key), "docType": "lock", "owner": owner, "expiresAtEpoch": expires}
    try:
        _container().create_item(doc)
        return True
    except exceptions.CosmosResourceExistsError:
        pass
    try:
        current = _container().read_item(item=_lock_id(key), partition_key=_lock_id(key))
    except exceptions.CosmosResourceNotFoundError:
        return False  # lo liberaron justo ahora; el siguiente intento lo toma
    if current.get("expiresAtEpoch", 0) >= time.time():
        return False
    # Lease vencido (el dueño murió): lo toma quien gane el reemplazo con ETag.
    try:
        _container().replace_item(
            item=current["id"],
            body={**current, "owner": owner, "expiresAtEpoch": expires},
            etag=current.get("_etag"),
            match_condition=MatchConditions.IfNotModified,
        )
        logger.warning("lock %s: lease vencido de %s, tomado por %s", key, current.get("owner"), owner)
        return True
    except exceptions.CosmosAccessConditionFailedError:
        return False


def _release(key: str, owner: str) -> None:
    try:
        current = _container().read_item(item=_lock_id(key), partition_key=_lock_id(key))
        if current.get("owner") == owner:
            _container().delete_item(
                item=current["id"],
                partition_key=current["id"],
                etag=current.get("_etag"),
                match_condition=MatchConditions.IfNotModified,
            )
    except (exceptions.CosmosResourceNotFoundError, exceptions.CosmosAccessConditionFailedError):
        pass  # ya no era nuestro, o ya no existe
    except Exception:  # noqa: BLE001 - liberar nunca debe romper el turno; el lease vence solo
        logger.exception("lock %s: no pude liberarlo (vencerá solo)", key)


@contextlib.contextmanager
def tenant_lock(key: str, wait_seconds: float = LOCK_WAIT_SECONDS) -> Iterator[None]:
    """Serializa el trabajo de un mismo tenant (mensajes entrantes y ejecución
    de planes). Espera hasta `wait_seconds`; si no lo logra lanza LockBusy y el
    mensaje vuelve a la cola por la vía normal de reintento."""
    owner = new_id()
    deadline = time.monotonic() + wait_seconds
    while not _try_acquire(key, owner):
        if time.monotonic() >= deadline:
            raise LockBusy(key)
        time.sleep(_LOCK_POLL_SECONDS)
    try:
        yield
    finally:
        _release(key, owner)
