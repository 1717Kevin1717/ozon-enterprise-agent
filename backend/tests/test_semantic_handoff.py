import json

import pytest

from app.agent import zhipu_agent
from app.agent.query_understanding import SemanticPlannerFailure, semantic_fallback, understand_query
from test_stability_sprint import ask, by_title, deterministic_client, seed


def _frame(*, intent, dimensions, tools, entity_mentions=None, requires_context=False, metric=None):
    value = {
        "intent": intent,
        "question_type": intent,
        "entity_mentions": entity_mentions or [],
        "references": [],
        "requested_dimensions": dimensions,
        "confidence": 0.92,
        "route": "SEMANTIC_PLANNER",
        "planned_tools": tools,
        "requires_context": requires_context,
    }
    if metric:
        value["metric"] = metric
    return value


def _use_mock_semantic(monkeypatch, factory):
    from app.api import router

    calls = []

    async def planner(query, packet):
        calls.append((query, packet))
        return factory(query, packet)

    monkeypatch.setattr(router.settings, "llm_provider", "zhipu")
    monkeypatch.setattr(zhipu_agent.settings, "zhipu_api_key", "mock-not-real")
    monkeypatch.setattr(zhipu_agent, "_semantic_plan", planner)
    return calls


def test_t1_standard_price_remains_deterministic(deterministic_client, monkeypatch):
    headers, items = seed(deterministic_client, "handoff-standard-price")
    target = by_title(items, "桌面理线器")
    calls = _use_mock_semantic(monkeypatch, lambda *_: pytest.fail("fast path must not call semantic planner"))

    result = ask(deterministic_client, headers, target["title"] + "现在卖多少钱")

    assert calls == []
    assert result["intent"] == "product_price"
    assert result["response_type"] == "simple_fact"
    assert result["product_ids"] == [target["id"]]
    assert result["tool_call_count"] == 1


@pytest.mark.parametrize("suffix", ["现在卖多少来着", "目前什么价来着"])
def test_t2_unrecognized_price_paraphrase_uses_semantic_handoff(deterministic_client, monkeypatch, suffix):
    headers, items = seed(deterministic_client, "handoff-price-" + str(abs(hash(suffix))))
    target = by_title(items, "桌面理线器")
    calls = _use_mock_semantic(monkeypatch, lambda query, _: _frame(
        intent="product_price", dimensions=["price"], tools=["get_product"], entity_mentions=[target["title"]],
    ))

    result = ask(deterministic_client, headers, target["title"] + suffix)

    assert len(calls) == 1
    assert calls[0][1]["rule_understanding"]["route"] == "SEMANTIC_PLANNER"
    assert result["intent"] == "product_price"
    assert result["response_type"] == "simple_fact"
    assert result["product_ids"] == [target["id"]]
    assert result["tool_call_count"] == 1


def test_t3_unique_alias_price_remains_deterministic(deterministic_client, monkeypatch):
    headers, items = seed(deterministic_client, "handoff-alias-price")
    target = by_title(items, "桌面理线器")
    calls = _use_mock_semantic(monkeypatch, lambda *_: pytest.fail("alias fast path must not call semantic planner"))

    result = ask(deterministic_client, headers, "理线器多少钱？")

    assert calls == []
    assert result["product_ids"] == [target["id"]]
    assert result["requested_entities"][0]["status"] == "UNIQUE_ALIAS_MATCH"


@pytest.mark.parametrize("follow_up", ["赚得咋样", "盈利能力呢"])
def test_t4_semantic_profit_follow_up_uses_last_entity(deterministic_client, monkeypatch, follow_up):
    headers, items = seed(deterministic_client, "handoff-profit-" + str(abs(hash(follow_up))))
    target = by_title(items, "桌面理线器")
    calls = _use_mock_semantic(monkeypatch, lambda _query, _packet: _frame(
        intent="product_detail", dimensions=["profit", "roi"],
        tools=["get_product", "calculate_profit"], requires_context=True,
    ))
    first = ask(deterministic_client, headers, target["title"] + "多少钱？")

    result = ask(deterministic_client, headers, follow_up, session_id=first["session_id"])

    assert len(calls) == 1
    packet = calls[0][1]
    assert packet["last_explicit_product_ids"] == [target["id"]]
    assert packet["last_intent"] == "product_price"
    assert packet["last_metric"] == "current_price"
    assert result["product_ids"] == [target["id"]]
    assert result["context_source"] == "last_explicit_entity"
    assert result["requested_dimensions"] == ["profit", "roi"]
    assert {row["tool_name"] for row in result["tool_results"]} == {"get_product", "calculate_profit"}
    assert all(label in result["answer"] for label in ("单件净利润", "净利率", "ROI"))
    assert "成本压力主要来自" in result["answer"]
    assert not any(token in result["answer"] for token in ("mock_enterprise_catalog", "deterministic_evaluation_engine", "PRESENT", "net_profit"))


def test_t5_contextual_calculation_explanation(deterministic_client, monkeypatch):
    headers, items = seed(deterministic_client, "handoff-calculation")
    target = by_title(items, "桌面理线器")
    calls = _use_mock_semantic(monkeypatch, lambda _query, _packet: _frame(
        intent="calculation_explanation", dimensions=["profit", "roi", "calculation", "evidence"],
        tools=["get_product", "calculate_profit"], requires_context=True,
    ))
    first = ask(deterministic_client, headers, target["title"] + "利润如何？")

    result = ask(deterministic_client, headers, "这钱怎么来的", session_id=first["session_id"])

    assert len(calls) == 1
    assert result["product_ids"] == [target["id"]]
    assert result["response_type"] == "calculation_explanation"
    assert result["context_source"] in {"last_explicit_entity", "last_resolved_entity"}
    assert "计算逻辑：售价" in result["answer"] and "实际计入的成本" in result["answer"]
    assert not any(token in result["answer"] for token in ("mock_enterprise_catalog", "deterministic_evaluation_engine", "PRESENT", "current_price"))


def test_t5b_cost_breakdown_is_ranked_backend_business_answer(deterministic_client, monkeypatch):
    from app.agent.semantic_response import COST_LABELS

    headers, items = seed(deterministic_client, "handoff-cost-breakdown")
    target = by_title(items, "桌面理线器")
    calls = _use_mock_semantic(monkeypatch, lambda _query, _packet: _frame(
        intent="calculation_explanation", dimensions=["cost_breakdown"],
        tools=["get_product", "calculate_profit"], requires_context=True, metric="cost_breakdown",
    ))
    first = ask(deterministic_client, headers, target["title"] + "多少钱？")

    result = ask(deterministic_client, headers, "成本主要花在哪几块", session_id=first["session_id"])

    assert len(calls) == 1
    assert result["understanding"]["metric"] == "cost_breakdown"
    first_ranked_line = next(line for line in result["answer"].splitlines() if line.startswith("1. "))
    assert any(first_ranked_line.startswith(f"1. {label}") for label in COST_LABELS.values())
    assert "排序完全来自本轮后端成本数据" in result["answer"]
    assert not any(token in result["answer"] for token in ("mock_enterprise_catalog", "deterministic_evaluation_engine", "PRESENT", "procurement_cost"))


@pytest.mark.parametrize(("follow_up", "expected_planner_calls"), [("赚得咋样", 1), ("那净利率呢", 0), ("那ROI呢", None)])
def test_t6_context_free_profit_ellipsis_clarifies(deterministic_client, monkeypatch, follow_up, expected_planner_calls):
    headers, _ = seed(deterministic_client, "handoff-empty-context-" + str(abs(hash(follow_up))))
    calls = _use_mock_semantic(monkeypatch, lambda _query, _packet: _frame(
        intent="product_detail", dimensions=["profit", "roi"],
        tools=["get_product", "calculate_profit"], requires_context=True,
    ))

    result = ask(deterministic_client, headers, follow_up)

    assert len(calls) <= 1 if expected_planner_calls is None else len(calls) == expected_planner_calls
    assert result["response_type"] == "clarification"
    assert result["product_ids"] == []
    assert result["tool_call_count"] == 0
    assert result["fallback_reason"] is None
    assert result["clarification_code"] == "MISSING_CONTEXT"
    if follow_up == "那净利率呢":
        assert "哪个商品的净利率" in result["answer"]
    else:
        assert "哪个商品" in result["answer"]


def test_t7_true_unknown_product_stays_not_found(deterministic_client, monkeypatch):
    headers, _ = seed(deterministic_client, "handoff-not-found")
    calls = _use_mock_semantic(monkeypatch, lambda *_: pytest.fail("high-confidence price intent must remain deterministic"))

    result = ask(deterministic_client, headers, "火星量子烤面包机多少钱？")

    assert calls == []
    assert result["response_type"] == "not_found"
    assert result["requested_entities"][0]["status"] == "NOT_FOUND"


def test_t8_count_fast_path_remains_deterministic(deterministic_client, monkeypatch):
    headers, _ = seed(deterministic_client, "handoff-count")
    calls = _use_mock_semantic(monkeypatch, lambda *_: pytest.fail("count fast path must not call semantic planner"))

    result = ask(deterministic_client, headers, "目前公司一共有多少商品？")

    assert calls == []
    assert result["intent"] == "company_product_count"
    assert result["response_type"] == "simple_fact"
    assert result["tool_call_count"] == 1


def test_real_semantic_provider_receives_only_anonymized_context_slots(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": json.dumps(_frame(
                intent="product_detail", dimensions=["profit", "roi"],
                tools=["get_product", "calculate_profit"], requires_context=True,
            ), ensure_ascii=False)}}]}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def post(self, _url, **kwargs):
            captured.update(kwargs["json"])
            return Response()

    monkeypatch.setattr(zhipu_agent.httpx, "AsyncClient", lambda **_: Client())
    packet = {
        "session_id": "secret-session",
        "last_explicit_product_ids": ["secret-product-id"],
        "last_resolved_product_ids": ["secret-product-id"],
        "verified_product_names": {"secret-product-id": "桌面理线器"},
        "last_intent": "product_price",
        "last_metric": "current_price",
        "last_requested_dimensions": ["price"],
        "last_comparison_order": [],
        "reference_candidates": [],
        "allowed_tools": ["get_product", "calculate_profit"],
    }

    import asyncio
    asyncio.run(zhipu_agent._semantic_plan("赚得怎么样", packet))

    outbound = json.dumps(captured, ensure_ascii=False)
    assert "secret-session" not in outbound
    assert "secret-product-id" not in outbound
    assert "桌面理线器" not in outbound
    assert "last_explicit_entity" in outbound
    assert "current_price" in outbound


def test_semantic_provider_rate_limit_is_preserved_as_safe_fallback_code():
    _, understanding = understand_query("经营表现说得口语一点", [])

    async def rate_limited(*_):
        raise SemanticPlannerFailure("RATE_LIMITED")

    import asyncio
    _, result = asyncio.run(semantic_fallback("经营表现说得口语一点", understanding, rate_limited))

    assert result.route == "CLARIFICATION"
    assert result.failure_code == "RATE_LIMITED"
    assert result.planner_calls == 1
