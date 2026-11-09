import json
import time

import pytest
from azure.core import MatchConditions
from azure.cosmos import exceptions

from george import agent, plan
from george.repositories import plans
from george.tools import plan_tools, registry
from george.tools.common import ToolContext, ToolError


def ctx(role="owner", tenant_id="t-1", chat_id="c-1"):
    return ToolContext(chat_id=chat_id, role=role, user_name="Ana", correlation_id="x", tenant_id=tenant_id, tenant={})


class FakeContainer:
    """platformConfig en memoria, con ETag como Cosmos."""

    def __init__(self):
        self.docs = {}
        self.n = 0

    def _stamp(self, doc):
        self.n += 1
        return {**doc, "_etag": f"e{self.n}"}

    def create_item(self, doc):
        if doc["id"] in self.docs:
            raise exceptions.CosmosResourceExistsError(status_code=409, message="exists")
        self.docs[doc["id"]] = self._stamp(doc)
        return self.docs[doc["id"]]

    def upsert_item(self, doc):
        self.docs[doc["id"]] = self._stamp(doc)
        return self.docs[doc["id"]]

    def read_item(self, item, partition_key):
        if item not in self.docs:
            raise exceptions.CosmosResourceNotFoundError(status_code=404, message="nf")
        return dict(self.docs[item])

    def replace_item(self, item, body, etag=None, match_condition=None):
        if match_condition == MatchConditions.IfNotModified and self.docs[item]["_etag"] != etag:
            raise exceptions.CosmosAccessConditionFailedError(status_code=412, message="etag")
        self.docs[item] = self._stamp(body)
        return dict(self.docs[item])

    def delete_item(self, item, partition_key, etag=None, match_condition=None):
        if item not in self.docs:
            raise exceptions.CosmosResourceNotFoundError(status_code=404, message="nf")
        if match_condition == MatchConditions.IfNotModified and self.docs[item]["_etag"] != etag:
            raise exceptions.CosmosAccessConditionFailedError(status_code=412, message="etag")
        del self.docs[item]


@pytest.fixture
def store(monkeypatch):
    c = FakeContainer()
    monkeypatch.setattr(plans, "_container", lambda: c)
    return c


# ------------------------------------------------------------ clasificación
def test_every_registered_tool_is_classified():
    names = set(registry._BY_NAME)
    classified = plan.WRITE_TOOLS | plan.IMMEDIATE_TOOLS
    assert names - classified == set(), "tool nueva sin clasificar en george/plan.py (¿escribe? -> WRITE_TOOLS)"
    assert classified - names == set(), "nombre en plan.py que no existe en el registry"
    assert not (plan.WRITE_TOOLS & plan.IMMEDIATE_TOOLS)


# ------------------------------------------------------------ validación
def test_validate_actions_ok_and_errors():
    ok = plan.validate_actions(
        [{"tool": "create_expense", "input": {"amount": 1, "concept": "x"}, "label": " Gasto "}], ctx()
    )
    assert ok[0]["label"] == "Gasto"
    with pytest.raises(ValueError, match="consulta"):
        plan.validate_actions([{"tool": "search_clients", "input": {}, "label": "x"}], ctx())
    with pytest.raises(ValueError, match="label"):
        plan.validate_actions([{"tool": "create_expense", "input": {"amount": 1, "concept": "x"}}], ctx())
    with pytest.raises(ValueError, match="faltan"):
        plan.validate_actions([{"tool": "create_expense", "input": {}, "label": "x"}], ctx())
    with pytest.raises(ValueError):
        plan.validate_actions([], ctx())
    with pytest.raises(ValueError, match="rol"):
        plan.validate_actions([{"tool": "add_business_rule", "input": {"text": "x"}, "label": "x"}], ctx("walker"))


# ------------------------------------------------------------ propose / confirm / cancel
def _propose(store, c=None):
    return json.loads(
        plan_tools._propose_actions(
            {
                "summary": "Gasto",
                "actions": [{"tool": "create_expense", "input": {"amount": 5, "concept": "x"}, "label": "Gasto 5"}],
            },
            c or ctx(),
        )
    )


def test_propose_saves_pending_and_does_not_execute(store):
    out = _propose(store)
    assert out["saved"] and out["executed"] is False
    assert plans.get_pending_plan("c-1")["actions"][0]["tool"] == "create_expense"


def test_confirm_flow_and_idempotency(store):
    with pytest.raises(ToolError, match="No hay"):
        plan_tools._confirm_pending({}, ctx())
    _propose(store)
    assert json.loads(plan_tools._confirm_pending({}, ctx()))["confirmed"]
    assert store.docs["plan::c-1"]["status"] == "confirmed"
    assert json.loads(plan_tools._confirm_pending({}, ctx()))["confirmed"]  # reintento
    assert plans.get_pending_plan("c-1") is None


def test_confirm_rejects_other_tenant_and_expired(store):
    _propose(store)
    with pytest.raises(ToolError, match="otro negocio"):
        plan_tools._confirm_pending({}, ctx(tenant_id="t-2"))
    store.docs["plan::c-1"]["expiresAt"] = "2000-01-01T00:00:00+00:00"
    with pytest.raises(ToolError, match="venció"):
        plan_tools._confirm_pending({}, ctx())


def test_cancel(store):
    _propose(store)
    plan_tools._cancel_pending({}, ctx())
    assert plans.get_pending_plan("c-1") is None
    with pytest.raises(ToolError):
        plan_tools._cancel_pending({}, ctx())


def test_new_proposal_replaces_old(store):
    _propose(store)
    first = store.docs["plan::c-1"]["planId"]
    _propose(store)
    assert store.docs["plan::c-1"]["planId"] != first


# ------------------------------------------------------------ guardas del loop
class Block:
    def __init__(self, name, input_=None):
        self.name, self.input, self.type, self.id = name, input_ or {}, "tool_use", "id1"


def test_chat_mode_blocks_direct_writes_but_not_reads_or_reports():
    assert "propose_actions" in agent._plan_flow_block(Block("create_expense"), None, [])
    assert agent._plan_flow_block(Block("search_clients"), None, []) is None
    # reportes: lista cerrada propia, no se les aplica el flujo
    assert agent._plan_flow_block(Block("create_expense"), ("search_records",), []) is None


def test_cannot_confirm_in_same_turn_as_proposal():
    log = [{"name": "propose_actions"}]
    assert "siguiente mensaje" in agent._plan_flow_block(Block("confirm_pending"), None, log)
    assert agent._plan_flow_block(Block("confirm_pending"), None, []) is None


def test_tainted_turn_cannot_propose_business_rules():
    log = [{"name": "gmail_read"}]
    bad = Block("propose_actions", {"actions": [{"tool": "add_business_rule"}]})
    assert agent._plan_flow_block(bad, None, log) == agent._TAINTED_TURN_MESSAGE
    fine = Block("propose_actions", {"actions": [{"tool": "create_expense"}]})
    assert agent._plan_flow_block(fine, None, log) is None


# ------------------------------------------------------------ ejecución
def test_run_plan_continues_after_failure_and_reports_real_results(monkeypatch):
    def fake_dispatch(name, tool_input, c, only=None):
        if tool_input.get("boom"):
            return "Monto inválido", True
        return json.dumps({"ok": True, "link": "https://drive/1"}), False

    monkeypatch.setattr(registry, "dispatch", fake_dispatch)
    p = {
        "actions": [
            {"tool": "create_expense", "input": {}, "label": "Uno"},
            {"tool": "create_expense", "input": {"boom": 1}, "label": "Dos"},
            {"tool": "create_expense", "input": {}, "label": "Tres"},
        ]
    }
    results, audit = plan.run_plan(p, ctx())
    assert [r["ok"] for r in results] == [True, False, True]
    assert [a["ok"] for a in audit] == [True, False, True]
    text = plan.format_results(results)
    assert "⚠️ Ejecuté 2 de 3" in text and "❌ Dos: Monto inválido" in text and "https://drive/1" in text


def test_format_results_all_ok_and_all_failed():
    assert plan.format_results([{"label": "A", "ok": True, "text": "{}"}]).startswith("✅ Listo:")
    assert plan.format_results([{"label": "A", "ok": False, "text": "x"}]).startswith("❌ No pude")


# ------------------------------------------------------------ locks
def test_lock_is_exclusive_and_released(store):
    with plans.tenant_lock("t-1"):
        with pytest.raises(plans.LockBusy):
            with plans.tenant_lock("t-1", wait_seconds=0):
                pass
        with plans.tenant_lock("t-2", wait_seconds=0):  # otro tenant no espera
            pass
    with plans.tenant_lock("t-1", wait_seconds=0):  # liberado al salir
        pass
    assert "lock::t-1" not in store.docs


def test_expired_lease_is_taken_over(store):
    store.docs["lock::t-1"] = store._stamp(
        {"id": "lock::t-1", "owner": "dead", "expiresAtEpoch": time.time() - 1}
    )
    with plans.tenant_lock("t-1", wait_seconds=0):
        assert store.docs["lock::t-1"]["owner"] != "dead"


def test_lock_released_when_body_raises(store):
    with pytest.raises(RuntimeError):
        with plans.tenant_lock("t-1"):
            raise RuntimeError("x")
    assert "lock::t-1" not in store.docs


# ------------------------------------------------------------ pre-validación al proponer
_TYPE = {
    "typeKey": "tareas", "name": "Tareas", "status": "active", "mode": "event", "measureField": "cantidad",
    "groupField": None, "clientLink": "none",
    "fields": [
        {"key": "estado", "label": "Estado", "type": "choice", "choices": ["pendiente", "hecho"], "required": True},
        {"key": "cantidad", "label": "Cantidad", "type": "number", "required": True},
    ],
}


def _log(values):
    return [{"tool": "log_record", "input": {"type_key": "tareas", "values": values}, "label": "Tarea"}]


def test_log_record_validated_when_proposed():
    c = ToolContext(chat_id="c", role="owner", user_name="A", correlation_id="x", tenant_id="t-1", tenant={}, record_types=(_TYPE,))
    assert plan.validate_actions(_log({"estado": "hecho", "cantidad": 1}), c)
    with pytest.raises(ValueError, match="estado"):
        plan.validate_actions(_log({"estado": "terminado", "cantidad": 1}), c)
    # el mismo plan define el tipo: no se pre-valida contra uno que aún no existe
    both = [{"tool": "define_record_type", "input": {"name": "Tareas", "fields": [], "mode": "event", "measure_field": "cantidad"}, "label": "Tipo"}] + _log({"estado": "x"})
    both[0]["input"].update({"fields": []})
    assert plan.validate_actions(both, c)


def test_log_record_unknown_type_rejected(monkeypatch):
    monkeypatch.setattr(plan.records_repo, "get_record_type", lambda t, k: None)
    with pytest.raises(ValueError, match="no existe"):
        plan.validate_actions(_log({"estado": "hecho"}), ctx())


# ------------------------------------------------------------ respuesta cuando se corta
def test_max_tokens_reply_is_honest(monkeypatch):
    from types import SimpleNamespace

    resp = SimpleNamespace(
        stop_reason="max_tokens", content=[], usage=SimpleNamespace(input_tokens=1, output_tokens=1)
    )
    monkeypatch.setattr(agent, "_get_client", lambda: SimpleNamespace(messages=SimpleNamespace(create=lambda **k: resp)))
    monkeypatch.setattr(agent, "_chat_system_prompt", lambda c: "p")
    out = agent.run_agent(ctx(), [], "hola")
    assert "cortó" in out.reply_text and "hice los cambios" not in out.reply_text
