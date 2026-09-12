"""Small utility tools available to every role."""
from __future__ import annotations

import json
from typing import Any
from zoneinfo import ZoneInfo

from george.config import settings
from george.roles import BUSINESS_ROLES, PENDING_SELECTION_ROLE, PLATFORM_ADMIN_ROLE
from george.tools.common import ToolContext, ToolSpec
from george.util import utc_now

_ALL_ROLES = (*BUSINESS_ROLES, PLATFORM_ADMIN_ROLE, PENDING_SELECTION_ROLE)


def _get_current_datetime(_input: dict[str, Any], _ctx: ToolContext) -> str:
    now_bogota = utc_now().astimezone(ZoneInfo(settings.default_timezone))
    return json.dumps(
        {
            "iso": now_bogota.isoformat(timespec="seconds"),
            "date": now_bogota.strftime("%Y-%m-%d"),
            "time": now_bogota.strftime("%H:%M"),
            "weekday": now_bogota.strftime("%A"),
            "timezone": settings.default_timezone,
        }
    )


TOOLS = [
    ToolSpec(
        name="get_current_datetime",
        description=f"Devuelve la fecha y hora actual en la zona horaria del negocio ({settings.default_timezone}).",
        input_schema={"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        allowed_roles=_ALL_ROLES,
        handler=_get_current_datetime,
    ),
]
