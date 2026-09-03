from fastapi.testclient import TestClient

from app.agent.intent_engine import parse_intent
from app.agent.tools import plan_query
from app.main import app


HEADERS = {"X-Company-ID": "agent-v2-company", "X-User-ID": "agent-analyst", "X-Role": "company_admin"}


def test_generic_product_question_has_no_single_category_hardcode():
    intent, plan = plan_query("分析这个厨房收纳商品是否值得测试", 1)
    assert intent == "single_product_decision"
    assert [step["tool"] for step in plan] == ["search_products", "get_product", "calculate_profit", "get_sales_trend", "analyze_competition", "find_historical_failures"]


def test_price_question_creates_minimal_simulation_plan():
    parsed = parse_intent("如果售价调整到 139 RUB，净利润和风险如何变化？", ["product-1"])
    assert parsed.name == "price_simulation"
    assert parsed.proposed_price == 139
    assert [step["tool"] for step in parsed.plan] == ["get_product", "simulate_price_change"]


def test_explicit_selected_products_drive_comparison_plan():
    parsed = parse_intent("比较这两个商品", ["product-1", "product-2"])
    assert parsed.name == "comparison"
    assert parsed.selected_product_ids == ("product-1", "product-2")
    assert [step["tool"] for step in parsed.plan] == ["compare_products", "calculate_profit", "analyze_competition"]


def test_business_constraints_are_parsed_as_backend_filters():
    parsed = parse_intent("找利润率30%以上，竞争低于40的商品")
    assert parsed.name == "constraint_filter"
    assert parsed.filters["min_margin_rate"] == 0.30
    assert parsed.filters["max_market_saturation"] == 40


def test_score_threshold_answer_returns_exact_count_and_sorted_products(monkeypatch):
    from app.api import router as router_module

    monkeypatch.setattr(router_module.settings, "llm_provider", "disabled")
    with TestClient(app) as client:
        client.post("/api/v1/demo/seed", headers=HEADERS, json={"replace_demo": False})
        catalog = client.get("/api/v1/products", headers=HEADERS).json()["data"]
        expected = sorted(
            [item for item in catalog if float((item.get("analysis") or {}).get("recommendation_score") or 0) >= 60],
            key=lambda item: float(item["analysis"]["recommendation_score"]),
            reverse=True,
        )
        response = client.post("/api/v1/agent/ask", headers=HEADERS, json={"query": "企业有哪些商品分数达到60以上？"})

    assert response.status_code == 200
    result = response.json()["data"]
    assert result["matched_count"] == len(expected)
    assert [item["id"] for item in result["products"]] == [item["id"] for item in expected]
    assert [item["score"] for item in result["products"]] == sorted([item["score"] for item in result["products"]], reverse=True)
    assert result["source_badge"] == "规则引擎结果"
    assert result["response_mode"] == "rule_engine"
    assert result["tool_trace_summary"] == ["1. 筛选企业商品库"]


def test_session_memory_resolves_follow_up_reference_without_guessing(monkeypatch):
    from app.api import router as router_module

    monkeypatch.setattr(router_module.settings, "llm_provider", "disabled")
    headers = {**HEADERS, "X-Company-ID": "agent-memory-company"}
    with TestClient(app) as client:
        products = client.post("/api/v1/demo/seed", headers=headers, json={"replace_demo": False}).json()["data"]["products"]
        selected = [products[0]["id"], products[3]["id"]]
        first = client.post("/api/v1/agent/ask", headers=headers, json={"query": "比较这两个商品", "selected_product_ids": selected}).json()["data"]
        second = client.post("/api/v1/agent/ask", headers=headers, json={"query": "和刚才那两个相比，哪个更值得测试？", "session_id": first["session_id"]}).json()["data"]
        sessions = client.get("/api/v1/agent/sessions", headers=headers).json()["data"]

    assert first["matched_count"] == 2
    assert second["matched_count"] == 2
    assert {item["id"] for item in second["products"]} == set(selected)
    saved = next(item for item in sessions if item["id"] == first["session_id"])
    assert set(saved["last_product_ids"]) == set(selected)
    assert saved["state"]["last_intent"] == "comparison"
