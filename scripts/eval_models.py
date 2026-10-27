"""Compares models on the failure mode that matters most for George: does a
write-intent message actually end in a tool call, or does the model just SAY
it saved something ("✅ Anotado...") without calling any tool?

It replays messages through the same system prompt and the same tool
definitions the bot sends in production (`prompts.build_system_prompt`,
`registry.anthropic_tool_defs`), with every tool stubbed — nothing touches
Cosmos or Telegram. Each message runs in two scenarios:

  clean          no prior history
  contaminated   two prior turns where George confirmed "✅ Anotado ..." with NO
                 tool call — the self-reinforcing pattern seen in production
                 (after one such turn, the next is one ~67% of the time)

What it does NOT include: the `_VERIFY_NUDGE` backstop in agent.py, so this
measures the raw model, not the model plus its safety net.

Spends real money (Anthropic API). Run with --dry-run first to see the call
count and a cost estimate without any network access.

    set ANTHROPIC_API_KEY=...
    python scripts/eval_models.py --dry-run
    python scripts/eval_models.py --runs 2
    python scripts/eval_models.py --cases path/to/extra_cases.json --out results.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# george.config reads env vars at import time — populate before importing george.*
for _k, _v in {
    "COSMOS_ENDPOINT": "https://localhost:8081",
    "COSMOS_DATABASE": "george-eval",
    "COSMOS_KEY": "eval-key",
    "TELEGRAM_BOT_TOKEN": "eval-token",
    "TELEGRAM_WEBHOOK_SECRET": "eval-secret",
    "TELEGRAM_WEBHOOK_PATH": "eval-path",
    "ANTHROPIC_API_KEY": "unset",
    "GROQ_API_KEY": "eval-groq",
    "DOCUMENT_INTELLIGENCE_ENDPOINT": "https://eval.cognitiveservices.azure.com",
    "PLATFORM_ADMIN_CHAT_ID": "111111111",
    "DRY_RUN": "true",
}.items():
    os.environ.setdefault(_k, _v)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from george.agent import _CONFIRMATION_RE, MAX_TOOL_TURNS  # noqa: E402
from george.prompts import build_system_prompt  # noqa: E402
from george.tools import registry  # noqa: E402
from george.tools.common import ToolContext  # noqa: E402

# $/MTok (input, output) — list prices; Haiku 5.5 is the <=100K-prompt rate card.
MODELS = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
}
_NEW_GEN = {"claude-haiku-5-5", "claude-sonnet-5-5"}  # accept output_config.effort
MAX_TOKENS = 4096  # same as agent._CHAT_MAX_TOKENS

WRITE_PREFIXES = ("create_", "log_", "register_", "upsert_", "define_")

CTX = ToolContext(
    chat_id="-1000000000001",
    role="owner",
    user_name="Ana",
    correlation_id="eval",
    tenant_id="eval-tenant",
    tenant={
        "name": "Compras del hogar",
        "businessType": "control de compras",
        "currency": "COP",
        "timezone": "America/Bogota",
        "itemLabelSingular": "cliente",
        "itemLabelPlural": "clientes",
    },
    record_types=[
        {
            "typeKey": "compras_supermercado",
            "name": "Compras Supermercado",
            "mode": "event",
            "clientLink": "none",
            "fields": [{"key": "articulo"}, {"key": "precio"}, {"key": "cantidad"}, {"key": "supermercado"}],
        }
    ],
)

# Synthetic cases (no production data). expected_items counts how many separate
# records a correct run should write; None = at least one write is enough.
BUILTIN_CASES = [
    {"id": "one_item", "text": "Anota una botella de kéfir de 1000 ml que vale 15.450", "kind": "write", "expected_items": 1},
    {"id": "one_item_b", "text": "Sumale una caja de champiñones de 250 gramos a 7200", "kind": "write", "expected_items": 1},
    {"id": "one_item_c", "text": "Agregale 3800 de espinaca, 200 gramos.", "kind": "write", "expected_items": 1},
    {"id": "three_items", "text": "Llevo 3 cogollos a 6890, una bolsa de manzanas a 4990 y una de zanahoria a 2490", "kind": "write", "expected_items": 3},
    {"id": "five_items", "text": "Compré leche a 4200, pan a 3500, huevos a 12000, arroz a 5800 y aceite a 14900", "kind": "write", "expected_items": 5},
    {"id": "eight_items", "text": "Lista del mercado: tomate 3200, cebolla 2800, papa 4100, plátano 3000, limón 2500, aguacate 6900, cilantro 1200, ajo 1800", "kind": "write", "expected_items": 8},
    {"id": "expense", "text": "Pagué 85 mil de arriendo del local", "kind": "write", "expected_items": None},
    {"id": "voice_style", "text": "eh listo anota ahí tres libras de tomate que valieron nueve mil y dos de cebolla a cuatro mil", "kind": "write", "expected_items": 2},
    {"id": "correction", "text": "Corrige lo de la espinaca, fueron 4200 y no 3800", "kind": "write", "expected_items": None},
    {"id": "query_total", "text": "¿Cuánto llevo gastado hoy?", "kind": "read", "expected_items": None},
    {"id": "query_list", "text": "¿Qué compré en el supermercado esta semana?", "kind": "read", "expected_items": None},
    {"id": "smalltalk", "text": "Gracias George, quedó muy bien", "kind": "chat", "expected_items": None},
]

CONTAMINATED_HISTORY = [
    {"role": "user", "content": "[Ana]: Agrega 2 paquetes de pasta a 3600 y un frasco de salsa a 5200"},
    {"role": "assistant", "content": "✅ Anotado:\n- 2 paquetes de pasta — $3.600\n- 1 frasco de salsa — $5.200\n\nVas por $8.800 en total. ¿Qué más?"},
    {"role": "user", "content": "[Ana]: Sumale una bolsa de arroz de 5800"},
    {"role": "assistant", "content": "✅ Anotado: 1 bolsa de arroz — $5.800.\n\nVas por $14.600 en total. ¿Qué más agregás?"},
]


def stub_result(name: str, tool_input: dict) -> str:
    if name.startswith(("search_", "list_")) or name in ("summarize_records", "recall_chat_history", "get_client_balance"):
        return json.dumps({"results": [], "records": [], "clients": [], "note": "sin resultados"})
    if name == "get_current_datetime":
        return json.dumps({"now": "2026-10-26T10:30:00-05:00"})
    return json.dumps({"ok": True, "id": "stub-id", "saved": tool_input})


def is_write(name: str) -> bool:
    return name.startswith(WRITE_PREFIXES)


def run_one(client, model: str, system: str, tools: list, history: list, text: str, effort: str | None) -> dict:
    messages = [*history, {"role": "user", "content": f"[{CTX.user_name}]: {text}"}]
    tool_calls: list[str] = []
    tin = tout = 0
    stop = None
    final_text = ""
    for _turn in range(MAX_TOOL_TURNS):
        kwargs = {"model": model, "max_tokens": MAX_TOKENS, "system": system, "tools": tools, "messages": messages}
        if effort and model in _NEW_GEN:
            kwargs["output_config"] = {"effort": effort}
        response = client.messages.create(**kwargs)
        tin += response.usage.input_tokens
        tout += response.usage.output_tokens
        stop = response.stop_reason
        final_text = "".join(b.text for b in response.content if b.type == "text")
        if stop != "tool_use":
            break
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type == "tool_use":
                tool_calls.append(block.name)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": stub_result(block.name, block.input)})
        messages.append({"role": "user", "content": results})
    return {"tool_calls": tool_calls, "text": final_text, "stop": stop, "in": tin, "out": tout}


def classify(case: dict, run: dict) -> dict:
    calls = run["tool_calls"]
    writes = [c for c in calls if is_write(c)]
    claims = bool(_CONFIRMATION_RE.search(run["text"]))
    out = {"wrote": bool(writes), "n_writes": len(writes), "claim_no_tool": (not calls) and claims}
    if case["kind"] == "write":
        want = case["expected_items"]
        out["pass"] = bool(writes) and (want is None or len(writes) >= want)
        out["partial"] = bool(writes) and want is not None and len(writes) < want
        # Same definition as production's backstop (agent._CONFIRMATION_RE): ZERO tool
        # calls and a confirmation-sounding reply. A run that searched and then said
        # "no encontré nada guardado" is not a fabrication.
        out["fabricated"] = (not calls) and claims
        out["asked_or_checked"] = (not writes) and not out["fabricated"]
    elif case["kind"] == "read":
        out["pass"] = bool(calls) and not writes
        out["partial"] = False
        out["fabricated"] = (not calls) and claims
    else:  # chat: must not write
        out["pass"] = not writes
        out["partial"] = False
        out["fabricated"] = False
    out["max_tokens_stop"] = run["stop"] == "max_tokens"
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", default=",".join(MODELS), help="comma-separated model ids")
    ap.add_argument("--runs", type=int, default=2, help="repetitions per (case, scenario, model)")
    ap.add_argument("--effort", default="medium", help="effort for the 5.5 models (low|medium|high); '' = API default")
    ap.add_argument("--cases", help="JSON file with extra cases: [{id,text,kind,expected_items}]")
    ap.add_argument("--no-builtin", action="store_true", help="run only the cases from --cases")
    ap.add_argument("--out", default="eval_results.jsonl")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true", help="no API calls: print the plan and a cost estimate")
    args = ap.parse_args()

    cases = [] if args.no_builtin else list(BUILTIN_CASES)
    if args.cases:
        cases += json.loads(Path(args.cases).read_text(encoding="utf-8"))
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    for m in models:
        if m not in MODELS:
            sys.exit(f"unknown model {m!r}; known: {', '.join(MODELS)}")

    system = build_system_prompt(CTX)
    tools = registry.anthropic_tool_defs()
    scenarios = {"clean": [], "contaminated": CONTAMINATED_HISTORY}
    jobs = [(m, sc, c, r) for m in models for sc in scenarios for c in cases for r in range(args.runs)]

    est_in = (len(system) + len(json.dumps(tools))) / 3.2 + 150  # rough chars->tokens, per call
    print(f"cases={len(cases)} scenarios={len(scenarios)} models={len(models)} runs={args.runs} -> {len(jobs)} runs")
    print(f"prompt+tools ~{est_in:,.0f} input tokens per call (rough; Haiku 5.5's tokenizer counts ~30% more)")
    est_total = 0.0
    for m in models:
        n = sum(1 for j in jobs if j[0] == m)
        calls = n * 2.2  # ~1-3 model calls per run (tool loop)
        cost = calls * (est_in * MODELS[m][0] + 350 * MODELS[m][1]) / 1e6
        est_total += cost
        print(f"  {m:20} ~{calls:,.0f} API calls, est. ${cost:.2f}")
    print(f"  estimated total: ~${est_total:.2f} (thinking models may add output tokens)")
    if args.dry_run:
        return

    if os.environ.get("ANTHROPIC_API_KEY", "unset") == "unset":
        sys.exit("ANTHROPIC_API_KEY is not set")
    import anthropic

    client = anthropic.Anthropic()

    def work(job):
        m, sc, case, rep = job
        try:
            run = run_one(client, m, system, tools, scenarios[sc], case["text"], args.effort or None)
            return {"model": m, "scenario": sc, "case": case["id"], "kind": case["kind"], "rep": rep, **run, **classify(case, run)}
        except Exception as exc:  # noqa: BLE001 — keep going, report at the end
            return {"model": m, "scenario": sc, "case": case["id"], "kind": case["kind"], "rep": rep, "error": repr(exc)}

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(work, jobs))

    with open(args.out, "w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    errors = [r for r in results if "error" in r]
    ok = [r for r in results if "error" not in r]
    print(f"\n{len(ok)} runs ok, {len(errors)} errors (raw results: {args.out})")
    for e in errors[:3]:
        print("  error:", e["error"][:200])

    for scenario in scenarios:
        print(f"\n=== scenario: {scenario} (write cases only: pass = wrote enough records) ===")
        print(f"{'model':20} {'runs':>5} {'pass%':>6} {'partial%':>9} {'fabricated%':>12} {'maxtok':>7} {'cost$':>7}")
        for m in models:
            rows = [r for r in ok if r["model"] == m and r["scenario"] == scenario]
            w = [r for r in rows if r["kind"] == "write"]
            if not w:
                continue
            cost = sum((r["in"] * MODELS[m][0] + r["out"] * MODELS[m][1]) / 1e6 for r in rows)
            pct = lambda key: 100 * sum(1 for r in w if r[key]) / len(w)  # noqa: E731
            print(f"{m:20} {len(w):5d} {pct('pass'):6.0f} {pct('partial'):9.0f} {pct('fabricated'):12.0f} "
                  f"{sum(1 for r in rows if r['max_tokens_stop']):7d} {cost:7.3f}")

    print("\n=== controls (read = must call a tool without writing; chat = must not write) ===")
    for m in models:
        rows = [r for r in ok if r["model"] == m and r["kind"] in ("read", "chat")]
        if rows:
            print(f"{m:20} pass {100 * sum(1 for r in rows if r['pass']) / len(rows):.0f}% of {len(rows)}")

    print("\n=== write cases by size (pass%, both scenarios) ===")
    by_size = defaultdict(lambda: defaultdict(list))
    for r in ok:
        if r["kind"] == "write":
            want = next(c["expected_items"] for c in cases if c["id"] == r["case"])
            by_size["1" if want in (None, 1) else ("2-3" if want <= 3 else "5+")][r["model"]].append(r["pass"])
    for size in ("1", "2-3", "5+"):
        cells = "  ".join(f"{m.replace('claude-', '')}={100 * sum(v) / len(v):.0f}%" for m, v in by_size[size].items())
        print(f"  items {size:4}: {cells}")


if __name__ == "__main__":
    main()
