from george import prompts, telegram
from tests.test_tools import make_ctx
import function_app


def test_is_group_chat_by_id_sign():
    assert telegram.is_group_chat("-1001234567890")
    assert telegram.is_group_chat("-4567")
    assert not telegram.is_group_chat("222222222")


def _docs():
    return [
        {"userName": "Ana", "input": {"text": "anota 3 empanadas"}, "output": {"text": "listo"}},
        {"userName": "Luis", "input": {"transcript": "y 2 más"}, "output": {"text": "ok"}},
    ]


def test_history_tags_speaker_only_in_groups():
    private = function_app._history_to_messages(_docs())
    assert private[0]["content"] == "anota 3 empanadas"

    group = function_app._history_to_messages(_docs(), group=True)
    assert group[0]["content"] == "[Ana]: anota 3 empanadas"
    assert group[2]["content"] == "[Luis]: y 2 más"
    # George's own replies are never tagged
    assert group[1]["content"] == "listo"


def test_group_prompt_mentions_speaker_prefix():
    tenant = {"name": "Empanadas", "businessType": "comida", "currency": "COP", "timezone": "America/Bogota"}
    group_ctx = make_ctx("owner", chat_id="-100123")
    private_ctx = make_ctx("owner", chat_id="123")
    from dataclasses import replace
    group_prompt = prompts.build_system_prompt(replace(group_ctx, tenant=tenant))
    private_prompt = prompts.build_system_prompt(replace(private_ctx, tenant=tenant))
    assert "GRUPAL" in group_prompt
    assert "GRUPAL" not in private_prompt


def test_estimate_cost_uses_per_model_prices():
    from george.config import estimate_cost_usd

    # 1M in + 1M out at each model's list price
    assert estimate_cost_usd(1_000_000, 1_000_000, model="claude-haiku-4-5") == 6.0
    assert estimate_cost_usd(1_000_000, 1_000_000, model="claude-haiku-5-5") == 0.6
    # unknown model falls back instead of raising
    assert estimate_cost_usd(1_000_000, 0, model="claude-future-x") == 0.1
