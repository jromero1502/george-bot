import json

import pytest

from george.tools import finance, registry
from george.tools.common import ToolContext, ToolError


def _ctx(role="owner", tenant_id="t1"):
    return ToolContext(chat_id="1", role=role, user_name="Test", correlation_id="c", tenant_id=tenant_id)


@pytest.fixture
def repo(monkeypatch):
    store = {
        ("t1", "e1"): {"id": "e1", "type": "expense", "concept": "Mercado: Huevo", "amount": 13490, "period": "2026-10"},
        ("t1", "c1"): {"id": "c1", "type": "charge", "concept": "Paseo", "amount": 50000, "period": "2026-10"},
    }
    deleted = []
    monkeypatch.setattr(finance.finance_repo, "get_movement", lambda t, i: store.get((t, i)))
    monkeypatch.setattr(finance.finance_repo, "delete_movement", lambda t, i: deleted.append((t, i)) or True)
    return deleted


def test_deletes_an_expense(repo):
    out = json.loads(finance._delete_finance_entry({"entry_id": "e1"}, _ctx()))
    assert out["deleted"] is True and out["amount"] == 13490
    assert repo == [("t1", "e1")]


def test_refuses_charges_because_they_carry_client_balance(repo):
    with pytest.raises(ToolError, match="Solo se pueden eliminar gastos"):
        finance._delete_finance_entry({"entry_id": "c1"}, _ctx())
    assert repo == []


def test_unknown_id_and_other_tenants_entries_are_not_found(repo):
    with pytest.raises(ToolError, match="No existe"):
        finance._delete_finance_entry({"entry_id": "nope"}, _ctx())
    with pytest.raises(ToolError, match="No existe"):
        finance._delete_finance_entry({"entry_id": "e1"}, _ctx(tenant_id="t2"))
    assert repo == []


@pytest.mark.parametrize("role", ["walker", "viewer"])
def test_only_owner_and_admin_may_delete(repo, role):
    with pytest.raises(ToolError):
        finance._delete_finance_entry({"entry_id": "e1"}, _ctx(role=role))
    assert repo == []


def test_tool_is_registered_and_schema_is_strict_about_extra_fields():
    spec = registry._BY_NAME["delete_finance_entry"]
    assert spec.allowed_roles == ("owner", "admin")
    assert spec.input_schema["additionalProperties"] is False
