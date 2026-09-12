from dataclasses import replace

import pytest

from george.tools import registry
from george.tools.common import ToolContext, ToolError, require_role, require_tenant

_TEST_TENANT_ID = "test-tenant"


def make_ctx(
    role: str, chat_id: str = "1", tenant_id: str | None = _TEST_TENANT_ID, is_platform_admin: bool = False
) -> ToolContext:
    """Defaults to a tenant-scoped context — pass tenant_id=None to simulate
    platform_admin (or a mis-provisioned chat with no tenant)."""
    return ToolContext(
        chat_id=chat_id,
        role=role,
        user_name="Test",
        correlation_id="corr-test",
        tenant_id=tenant_id,
        is_platform_admin=is_platform_admin,
    )


# ---------------------------------------------------------------------------
# require_role
# ---------------------------------------------------------------------------
def test_require_role_allows_permitted_role():
    require_role(make_ctx("owner"), ("owner", "admin"), "some_tool")  # must not raise


def test_require_role_rejects_disallowed_role():
    with pytest.raises(ToolError):
        require_role(make_ctx("viewer"), ("owner", "admin"), "some_tool")


# ---------------------------------------------------------------------------
# require_tenant
# ---------------------------------------------------------------------------
def test_require_tenant_returns_tenant_id_when_present():
    assert require_tenant(make_ctx("owner")) == _TEST_TENANT_ID


def test_require_tenant_rejects_missing_tenant():
    with pytest.raises(ToolError):
        require_tenant(make_ctx("owner", tenant_id=None))


# ---------------------------------------------------------------------------
# Registry plumbing
# ---------------------------------------------------------------------------
def test_all_tools_have_unique_names():
    names = [t.name for t in registry.ALL_TOOLS]
    assert len(names) == len(set(names))


def test_anthropic_tool_defs_shape():
    defs = registry.anthropic_tool_defs()
    assert len(defs) == len(registry.ALL_TOOLS)
    for tool_def in defs:
        assert set(tool_def.keys()) == {"name", "description", "input_schema", "strict"}
        assert tool_def["input_schema"]["additionalProperties"] is False
        assert "allowedRoles" not in tool_def["input_schema"]


def test_dispatch_unknown_tool_returns_error():
    result, is_error = registry.dispatch("does_not_exist", {}, make_ctx("owner"))
    assert is_error is True
    assert "desconocida" in result.lower()


def test_dispatch_never_raises_on_handler_bug(monkeypatch):
    def boom(_input, _ctx):
        raise RuntimeError("boom")

    # ToolSpec is a frozen dataclass — swap the registry entry rather than
    # mutating the tool in place.
    broken = replace(registry._BY_NAME["get_current_datetime"], handler=boom)
    monkeypatch.setitem(registry._BY_NAME, "get_current_datetime", broken)

    result, is_error = registry.dispatch("get_current_datetime", {}, make_ctx("owner"))
    assert is_error is True
    assert "boom" in result


# ---------------------------------------------------------------------------
# get_current_datetime — no I/O, safe to call for every role
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("role", ["owner", "admin", "walker", "viewer"])
def test_get_current_datetime_available_to_all_roles(role):
    result, is_error = registry.dispatch("get_current_datetime", {}, make_ctx(role))
    assert is_error is False
    assert '"timezone"' in result


# ---------------------------------------------------------------------------
# Role gating end-to-end through the registry (no Cosmos I/O: the
# permission check runs before any repository call for every tool below)
# ---------------------------------------------------------------------------
def test_upsert_chat_is_owner_only():
    result, is_error = registry.dispatch(
        "upsert_chat", {"chat_id": "999", "role": "admin"}, make_ctx("admin")
    )
    assert is_error is True
    assert "rol" in result.lower()


def test_search_clients_denied_for_viewer():
    result, is_error = registry.dispatch("search_clients", {"query": "maria"}, make_ctx("viewer"))
    assert is_error is True


def test_search_clients_permitted_role_passes_gate_before_hitting_cosmos():
    # walker is allowed to read clients; this will get past require_role and
    # only fail once it tries to reach Cosmos DB (which isn't running in
    # this test) — proving the gate itself isn't the blocker.
    result, is_error = registry.dispatch("search_clients", {"query": "maria"}, make_ctx("walker"))
    assert is_error is True
    assert "rol" not in result.lower()


def test_create_charge_denied_for_walker():
    result, is_error = registry.dispatch(
        "create_charge",
        {"client_id": "abc", "amount": 20000, "concept": "paseo"},
        make_ctx("walker"),
    )
    assert is_error is True
    assert "rol" in result.lower()


def test_create_expense_denied_for_walker():
    result, is_error = registry.dispatch(
        "create_expense", {"amount": 50000, "concept": "insumos"}, make_ctx("walker")
    )
    assert is_error is True
    assert "rol" in result.lower()


def test_create_expense_permitted_role_passes_gate_before_hitting_cosmos():
    result, is_error = registry.dispatch(
        "create_expense", {"amount": 50000, "concept": "insumos"}, make_ctx("owner")
    )
    assert is_error is True
    assert "rol" not in result.lower()


def test_list_pqrs_denied_for_viewer_but_create_pqr_allowed():
    result, is_error = registry.dispatch("list_pqrs", {}, make_ctx("viewer"))
    assert is_error is True

    # create_pqr is open to every role and has no required repo lookups
    # before the write — it will only fail past the gate on the Cosmos call.
    result, is_error = registry.dispatch(
        "create_pqr",
        {"type": "sugerencia", "title": "t", "description": "d"},
        make_ctx("viewer"),
    )
    assert is_error is True
    assert "rol" not in result.lower()


# ---------------------------------------------------------------------------
# Input validation that happens before any repository call
# ---------------------------------------------------------------------------
def test_upsert_reminder_rejects_invalid_cron():
    result, is_error = registry.dispatch(
        "upsert_reminder",
        {
            "name": "Prueba",
            "kind": "message",
            "body": "hola",
            "schedule": {"type": "cron", "cron": "not a cron"},
            "target": {"type": "role", "role": "owner"},
        },
        make_ctx("owner"),
    )
    assert is_error is True
    assert "horario" in result.lower()


def test_upsert_reminder_rejects_role_target_without_role():
    result, is_error = registry.dispatch(
        "upsert_reminder",
        {
            "name": "Prueba",
            "kind": "message",
            "body": "hola",
            "schedule": {"type": "cron", "cron": "0 20 * * *"},
            "target": {"type": "role"},
        },
        make_ctx("owner"),
    )
    assert is_error is True
    assert "target.role" in result


def test_upsert_reminder_rejects_chat_target_without_chat_id():
    result, is_error = registry.dispatch(
        "upsert_reminder",
        {
            "name": "Prueba",
            "kind": "message",
            "body": "hola",
            "schedule": {"type": "cron", "cron": "0 20 * * *"},
            "target": {"type": "chat"},
        },
        make_ctx("owner"),
    )
    assert is_error is True
    assert "target.chat_id" in result


def test_upsert_reminder_denied_for_admin_role_check_runs_first():
    # walker isn't allowed to manage reminders at all — the role gate should
    # fire before schedule validation even runs, regardless of how broken
    # the input is.
    result, is_error = registry.dispatch(
        "upsert_reminder",
        {
            "name": "Prueba",
            "kind": "message",
            "body": "hola",
            "schedule": {"type": "cron", "cron": "garbage"},
            "target": {"type": "role", "role": "owner"},
        },
        make_ctx("walker"),
    )
    assert is_error is True
    assert "rol" in result.lower()


# ---------------------------------------------------------------------------
# Multi-tenant isolation
# ---------------------------------------------------------------------------
def test_tenant_scoped_tool_rejects_missing_tenant_even_with_allowed_role():
    # 'owner' is allowed to search_clients, but a chat with no tenantId
    # (a mis-provisioned chat, or a platform_admin who mistakenly kept an
    # owner-shaped role) must be rejected before any Cosmos call — a missing
    # partition key here would mean "give me everyone's clients", not "give
    # me no one's".
    result, is_error = registry.dispatch(
        "search_clients", {"query": "maria"}, make_ctx("owner", tenant_id=None)
    )
    assert is_error is True
    assert "negocio" in result.lower()


@pytest.mark.parametrize("tool_name", ["search_clients", "search_finance", "list_reminders", "list_pqrs"])
def test_various_tenant_scoped_reads_reject_missing_tenant(tool_name):
    ctx = make_ctx("owner", tenant_id=None)
    input_ = {"query": "x"} if tool_name == "search_clients" else {}
    result, is_error = registry.dispatch(tool_name, input_, ctx)
    assert is_error is True
    assert "negocio" in result.lower()


def test_create_tenant_is_platform_admin_only():
    result, is_error = registry.dispatch(
        "create_tenant",
        {"name": "Nuevo Negocio", "business_type": "Plomeria", "owner_chat_id": "999"},
        make_ctx("owner"),
    )
    assert is_error is True
    assert "rol" in result.lower()


def test_create_tenant_permitted_role_passes_gate_before_hitting_cosmos():
    # platform_admin has no tenant_id (by design) — create_tenant must not
    # itself require one via require_tenant.
    result, is_error = registry.dispatch(
        "create_tenant",
        {"name": "Nuevo Negocio", "business_type": "Plomeria", "owner_chat_id": "999"},
        make_ctx("platform_admin", tenant_id=None),
    )
    assert is_error is True
    assert "rol" not in result.lower()
    assert "negocio" not in result.lower()


def test_list_tenants_is_platform_admin_only():
    result, is_error = registry.dispatch("list_tenants", {}, make_ctx("owner"))
    assert is_error is True
    assert "rol" in result.lower()


# ---------------------------------------------------------------------------
# Platform-configurable default reminder templates
# ---------------------------------------------------------------------------
def test_get_default_reminder_templates_is_platform_admin_only():
    result, is_error = registry.dispatch("get_default_reminder_templates", {}, make_ctx("owner"))
    assert is_error is True
    assert "rol" in result.lower()


def test_get_default_reminder_templates_permitted_role_passes_gate_before_hitting_cosmos():
    result, is_error = registry.dispatch(
        "get_default_reminder_templates", {}, make_ctx("platform_admin", tenant_id=None)
    )
    assert is_error is True
    assert "rol" not in result.lower()


def test_set_default_reminder_templates_is_platform_admin_only():
    result, is_error = registry.dispatch(
        "set_default_reminder_templates",
        {"templates": [{"name": "x", "kind": "message", "body": "hola", "schedule": {"cron": "0 6 * * *"}, "target_role": "owner"}]},
        make_ctx("owner"),
    )
    assert is_error is True
    assert "rol" in result.lower()


def test_set_default_reminder_templates_rejects_empty_list():
    result, is_error = registry.dispatch(
        "set_default_reminder_templates", {"templates": []}, make_ctx("platform_admin", tenant_id=None)
    )
    assert is_error is True
    assert "vacia" in result.lower()


def test_set_default_reminder_templates_rejects_invalid_cron_before_hitting_cosmos():
    result, is_error = registry.dispatch(
        "set_default_reminder_templates",
        {
            "templates": [
                {
                    "name": "Prueba",
                    "kind": "message",
                    "body": "hola",
                    "schedule": {"cron": "not a cron"},
                    "target_role": "owner",
                }
            ]
        },
        make_ctx("platform_admin", tenant_id=None),
    )
    assert is_error is True
    assert "horario" in result.lower()


# ---------------------------------------------------------------------------
# Multi-membership: a chat can belong to several tenants
# ---------------------------------------------------------------------------
def test_add_chat_to_tenant_is_platform_admin_only():
    result, is_error = registry.dispatch(
        "add_chat_to_tenant",
        {"chat_id": "999", "tenant_id": "t1", "role": "walker"},
        make_ctx("owner"),
    )
    assert is_error is True
    assert "rol" in result.lower()


def test_add_chat_to_tenant_permitted_role_passes_gate_before_hitting_cosmos():
    result, is_error = registry.dispatch(
        "add_chat_to_tenant",
        {"chat_id": "999", "tenant_id": "t1", "role": "walker"},
        make_ctx("platform_admin", tenant_id=None),
    )
    assert is_error is True
    assert "rol" not in result.lower()


@pytest.mark.parametrize(
    "role", ["owner", "admin", "walker", "viewer", "pending_business_selection", "platform_admin"]
)
def test_list_my_businesses_allowed_for_business_pending_and_platform_admin_roles(role):
    # platform_admin is allowed too: a platform_admin chat can also belong to
    # a business of its own (dual role) and needs this to see it.
    result, is_error = registry.dispatch("list_my_businesses", {}, make_ctx(role, tenant_id=None))
    # No Cosmos running in tests — but the gate must pass (no "rol" error).
    assert is_error is True
    assert "rol" not in result.lower()


@pytest.mark.parametrize(
    "role", ["owner", "admin", "walker", "viewer", "pending_business_selection", "platform_admin"]
)
def test_switch_business_allowed_for_business_pending_and_platform_admin_roles(role):
    result, is_error = registry.dispatch(
        "switch_business", {"tenant_id": "t1"}, make_ctx(role, tenant_id=None)
    )
    assert is_error is True
    assert "rol" not in result.lower()


# ---------------------------------------------------------------------------
# switch_to_platform_admin: gated on ctx.is_platform_admin, not ctx.role —
# a dual-role chat operating its own business still has role='owner' etc.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("role", ["owner", "admin", "walker", "viewer"])
def test_switch_to_platform_admin_denied_when_chat_is_not_platform_admin(role):
    result, is_error = registry.dispatch(
        "switch_to_platform_admin", {}, make_ctx(role, is_platform_admin=False)
    )
    assert is_error is True
    assert "administrador de la plataforma" in result.lower()


def test_switch_to_platform_admin_permitted_dual_role_passes_gate_before_hitting_cosmos():
    result, is_error = registry.dispatch(
        "switch_to_platform_admin", {}, make_ctx("owner", is_platform_admin=True)
    )
    assert is_error is True
    assert "administrador de la plataforma" not in result.lower()


def test_switch_to_platform_admin_is_noop_when_already_in_platform_mode():
    result, is_error = registry.dispatch(
        "switch_to_platform_admin", {}, make_ctx("platform_admin", tenant_id=None, is_platform_admin=True)
    )
    assert is_error is False
    assert "ya estabas" in result.lower()
