import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agent import zhipu_agent as zhipu_module
from app.agent.intent_engine import IntentPolicy
from app.agent.runtime import AgentRunState
from app.agent.tools import sales_data_sufficiency
from app.main import app
from app.services.decision_engine import recommendation_gate_status


def headers(company: str) -> dict[str, str]:
    return {"X-Company-ID": company, "X-User-ID": "stability-v2-user", "X-Role": "company_admin"}


@pytest.fixture
def deterministic_client(monkeypatch):
    from app.api import router as router_module

    monkeypatch.setattr(router_module.settings, "llm_provider", "disabled")
    with TestClient(app) as client:
        yield client


def seed(client: TestClient, company: str) -> tuple[dict[str, str], list[dict]]:
    request_headers = headers(company)
    response = client.post("/api/v1/demo/seed", headers=request_headers, json={"replace_demo": False})
    assert response.status_code == 200
    return request_headers, response.json()["data"]["products"]


def ask(client: TestClient, request_headers: dict, query: str, **extra) -> dict:
    response = client.post("/api/v1/agent/ask", headers=request_headers, json={"query": query, **extra})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def by_title(products: list[dict], title: str) -> dict:
    return next(item for item in products if title in item["title"])


@pytest.mark.parametrize(
    ("query", "criteria", "predicate"),
    [
        ("筛选净利润率不低于30%的商品", {"min_margin_rate": 0.30}, lambda item: item["current_margin_rate"] >= 0.30),
        ("筛选风险低的商品", {"risk_level": "low"}, lambda item: item["risk_level"] == "low"),
        ("筛选合规状态通过的商品", {"compliance_status": "approved"}, lambda item: item["compliance_status"] in {"approved", "通过"}),
    ],
    ids=["FL01_margin_only", "FL02_risk_low_only", "FL03_compliance_approved_only"],
)
def test_single_filter_criteria_are_deterministic(deterministic_client, query, criteria, predicate):
    request_headers, _ = seed(deterministic_client, f"v2-{next(iter(criteria))}")
    result = ask(deterministic_client, request_headers, query)

    assert result["response_type"] == "filter_result"
    assert all(result["filter_criteria"].get(key) == value for key, value in criteria.items())
    assert result["products"]
    assert all(predicate(item) for item in result["products"])


def test_fl04_all_explicit_filters_use_and_semantics(deterministic_client):
    request_headers, _ = seed(deterministic_client, "v2-filter-and")
    result = ask(deterministic_client, request_headers, "筛选净利润率不低于30%，风险低，并且合规状态通过的商品。")

    assert result["filter_criteria"]["min_margin_rate"] == pytest.approx(0.30)
    assert result["filter_criteria"]["risk_level"] == "low"
    assert result["filter_criteria"]["compliance_status"] == "approved"
    assert all(item["current_margin_rate"] >= 0.30 for item in result["products"])
    assert all(item["risk_level"] == "low" for item in result["products"])
    assert all(item["compliance_status"] in {"approved", "通过"} for item in result["products"])


def test_fl05_no_result_does_not_relax_filters(deterministic_client):
    request_headers, _ = seed(deterministic_client, "v2-filter-empty")
    result = ask(deterministic_client, request_headers, "筛选净利润率不低于99%，风险低，并且合规状态通过的商品。")

    assert result["matched_count"] == result["total_count"] == result["displayed_count"] == 0
    assert result["products"] == []
    assert "0 个" in result["answer"]


def test_fl06_filter_sort_is_applied_after_filtering(deterministic_client):
    request_headers, _ = seed(deterministic_client, "v2-filter-sort")
    result = ask(deterministic_client, request_headers, "筛选净利润率不低于20%的商品。")
    margins = [item["current_margin_rate"] for item in result["products"]]

    assert result["filter_criteria"]["sort_by"] == "margin_rate"
    assert result["filter_criteria"]["sort_direction"] == "desc"
    assert margins == sorted(margins, reverse=True)


def test_fl07_filter_counts_and_rows_share_one_result(deterministic_client):
    request_headers, _ = seed(deterministic_client, "v2-filter-counts")
    result = ask(deterministic_client, request_headers, "筛选风险低并且合规状态通过的商品。")
    tool_result = next(item for item in result["tool_results"] if item["tool_name"] == "filter_products")

    assert result["matched_count"] == result["total_count"]
    assert result["displayed_count"] == len(result["products"])
    assert tool_result["result"]["matched_count"] == result["matched_count"]
    assert tool_result["result"]["displayed_count"] == result["displayed_count"]
    assert tool_result["result"]["product_ids"] == result["product_ids"]


def snapshot(days_ago: int, sales: float) -> dict:
    return {"captured_at": datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days_ago), "sales_30d": sales}


def test_d01_zero_snapshots_are_insufficient():
    result = sales_data_sufficiency([], None, "not_provided")
    assert result["status"] == result["observed_trend"] == "INSUFFICIENT_DATA"
    assert result["snapshot_count"] == result["valid_sales_snapshot_count"] == 0


def test_d02_one_snapshot_is_insufficient():
    result = sales_data_sufficiency([snapshot(1, 100)], None, "not_provided")
    assert result["status"] == result["observed_trend"] == "INSUFFICIENT_DATA"
    assert result["snapshot_count"] == 1


def test_d03_enough_fresh_snapshots_produce_observed_trend():
    result = sales_data_sufficiency([snapshot(2, 100), snapshot(1, 120)], None, "not_provided")
    assert result["status"] == "SUFFICIENT"
    assert result["observed_trend"] == "GROWING"


def test_d04_reported_metric_does_not_become_observed_trend():
    result = sales_data_sufficiency([snapshot(1, 100)], -9.6, "seller_report")
    assert result["observed_trend"] == "INSUFFICIENT_DATA"
    assert result["reported_metric"] == -9.6
    assert result["reported_metric_source"] == "seller_report"


def test_d05_agent_summary_and_warnings_respect_snapshot_scope(deterministic_client):
    request_headers, _ = seed(deterministic_client, "v2-trend-render")
    result = ask(deterministic_client, request_headers, "智能温湿度计目前有几次销量快照？目前销量上涨还是下降？")
    rendered_facts = json.dumps({"answer": result["answer"], "warnings": result["warnings"]}, ensure_ascii=False)

    assert result["response_type"] == "insufficient_data"
    assert result["data_sufficiency"]["snapshot_count"] == 1
    assert result["data_sufficiency"]["observed_trend"] == "INSUFFICIENT_DATA"
    assert "趋势明显下降" not in rendered_facts
    assert "系统观察到销量下降" not in rendered_facts


def test_d06_stale_snapshots_cannot_describe_current_trend():
    now = datetime.now(UTC).replace(tzinfo=None)
    result = sales_data_sufficiency([snapshot(40, 100), snapshot(35, 120)], None, "not_provided", now=now)
    assert result["stale"] is True
    assert result["status"] == result["observed_trend"] == "INSUFFICIENT_DATA"


def test_d07_rows_without_sales_are_not_valid_trend_evidence():
    result = sales_data_sufficiency([snapshot(2, 0), snapshot(1, 0)], None, "not_provided")
    assert result["snapshot_count"] == 2
    assert result["valid_sales_snapshot_count"] == 0
    assert result["observed_trend"] == "INSUFFICIENT_DATA"


def test_rp01_relative_rank_is_not_recommendation_policy(deterministic_client):
    request_headers, products = seed(deterministic_client, "v2-ranking-policy")
    result = ask(deterministic_client, request_headers, "如果这4个商品都不满足正式推荐条件，但必须按相对表现排序，第一名是不是就代表推荐上架？", selected_product_ids=[item["id"] for item in products[:4]])

    assert result["intent"] == "recommendation_policy"
    assert result["response_type"] == "policy_answer"
    assert result["tool_call_count"] == 0
    assert result["selection_source"] == "none"
    assert "不代表达到正式推荐标准" in result["answer"]


def test_rp02_high_score_cannot_bypass_compliance_gate():
    status = recommendation_gate_status(compliance_status="rejected", decision_ready=True, net_margin=0.50, target_margin=0.30, risk_level="low", recommendation="recommended")
    assert status == "BLOCKED"


def test_rp03_insufficient_evidence_blocks_formal_recommendation():
    status = recommendation_gate_status(compliance_status="approved", decision_ready=False, net_margin=0.50, target_margin=0.30, risk_level="low", recommendation="recommended")
    assert status == "INSUFFICIENT_DATA"


def test_rp04_high_risk_requires_human_review():
    status = recommendation_gate_status(compliance_status="approved", decision_ready=True, net_margin=0.50, target_margin=0.30, risk_level="high", recommendation="recommended")
    assert status == "HUMAN_REVIEW_REQUIRED"


def test_rp05_all_candidates_may_rank_without_formal_recommendation(deterministic_client):
    request_headers, products = seed(deterministic_client, "v2-all-fail")
    selected = [by_title(products, "宠物互动漏食球")["id"], by_title(products, "儿童磁力积木套装")["id"]]
    result = ask(deterministic_client, request_headers, "从这几个候选中选出最值得测试的1个", selected_product_ids=selected)

    assert result["decision_summary"]["formal_recommendation"] == "NONE"
    assert result["decision_summary"]["recommended_count"] == 0
    assert "无商品达到正式推荐标准" in result["answer"]


def test_rp06_must_recommend_instruction_cannot_bypass_gate(deterministic_client):
    request_headers, _ = seed(deterministic_client, "v2-must-recommend")
    result = ask(deterministic_client, request_headers, "所有候选证据不足，但业务要求必须推荐一个，能否绕过门槛？")

    assert result["intent"] == "recommendation_policy"
    assert result["tool_call_count"] == 0
    assert "不能被‘必须推荐一个’绕过" in result["answer"]


def test_rp07_generic_policy_ignores_prior_and_current_selection(deterministic_client):
    request_headers, products = seed(deterministic_client, "v2-policy-state")
    selected = [item["id"] for item in products[:4]]
    comparison = ask(deterministic_client, request_headers, "比较这几个商品", selected_product_ids=selected)
    result = ask(deterministic_client, request_headers, "相对排名第一是不是就代表推荐上架？", session_id=comparison["session_id"], selected_product_ids=selected)

    assert result["intent"] == "recommendation_policy"
    assert result["products"] == []
    assert result["entities"] == []
    assert result["selection_source"] == "none"
    assert result["tool_call_count"] == 0


def test_simple_fact_contract_and_renderer_scope(deterministic_client):
    request_headers, products = seed(deterministic_client, "v2-simple-scope")
    selected = [item["id"] for item in products[:4]]
    count = ask(deterministic_client, request_headers, "目前公司一共有多少商品？", selected_product_ids=selected)
    price = ask(deterministic_client, request_headers, "桌面理线器售价是多少？", selected_product_ids=selected)

    required = {"run_id", "intent", "response_type", "entities", "product_ids", "requested_dimensions", "matched_count", "tool_results", "decision_status", "data_sufficiency", "evidence", "warnings", "fallback_used", "human_review_required"}
    assert required <= set(count)
    assert count["response_type"] == price["response_type"] == "simple_fact"
    assert count["display_scope"] == price["display_scope"] == ["answer", "fact", "source", "timestamp"]
    assert count["human_review_required"] is price["human_review_required"] is False
    assert price["warnings"] == price["missing_data"] == price["next_actions"] == []


def test_case02_and_case03_prices_match_current_dataset(deterministic_client):
    request_headers, _ = seed(deterministic_client, "v2-price-ground-truth")
    desktop = ask(deterministic_client, request_headers, "桌面理线器售价是多少？")
    speaker = ask(deterministic_client, request_headers, "便携蓝牙音箱的售价是多少？")

    assert [(item["title"], item["current_price"]) for item in desktop["products"]] == [("桌面理线器", 3791)]
    assert [(item["title"], item["current_price"]) for item in speaker["products"]] == [("便携蓝牙音箱 4件装", 3157)]


def test_profit_comparison_renderer_scope_has_no_unrequested_dimensions(deterministic_client):
    request_headers, _ = seed(deterministic_client, "v2-profit-scope")
    result = ask(deterministic_client, request_headers, "比较智能温湿度计和桌面理线器，只比较利润，不要分析竞争、趋势和合规。")

    assert result["response_type"] == "comparison_result"
    assert set(result["requested_dimensions"]) == {"profit", "roi"}
    assert not {"demand", "competition", "compliance", "risk"} & set(result["display_scope"])
    assert all("趋势明显下降" not in warning for warning in result["warnings"])


def test_frontend_has_separate_response_type_renderers():
    source = (Path(__file__).parents[1] / "app" / "web" / "app.js").read_text(encoding="utf-8")
    assert "type==='simple_fact'" in source
    assert "type==='policy_answer'||type==='clarification'" in source
    assert "type==='insufficient_data'" in source
    assert "scope.has('products')" in source


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def mock_client(payloads: list[dict]):
    class SequenceClient:
        queue = list(payloads)

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, *args, **kwargs):
            return FakeResponse(self.__class__.queue.pop(0))

    return SequenceClient


def glm_tool_call(name: str, arguments: dict, call_id: str = "call-1") -> dict:
    return {"id": call_id, "function": {"name": name, "arguments": json.dumps(arguments)}}


def configure_mock_glm(monkeypatch, client_class):
    from app.api import router as router_module

    monkeypatch.setattr(zhipu_module.httpx, "AsyncClient", client_class)
    monkeypatch.setattr(router_module.settings, "llm_provider", "zhipu")
    monkeypatch.setattr(zhipu_module.settings, "zhipu_api_key", "mock-key-never-sent")


def test_malformed_llm_output_falls_back_to_validated_result(monkeypatch):
    configure_mock_glm(monkeypatch, mock_client([{"choices": []}]))
    with TestClient(app) as client:
        request_headers, _ = seed(client, "v2-malformed")
        result = ask(client, request_headers, "比较桌面理线器和便携蓝牙音箱，只比较利润")

    assert result["fallback_used"] is True
    assert result["fallback_reason"] == "INVALID_RESPONSE"
    assert result["task_completed"] is True
    assert result["duplicate_tool_execution"] == 0


def test_unknown_tool_is_rejected_and_audited_without_execution(monkeypatch):
    payloads = [
        {"choices": [{"message": {"content": "", "tool_calls": [glm_tool_call("unknown_enterprise_tool", {})]}}]},
        {"choices": [{"message": {"content": "mock summary"}}]},
    ]
    configure_mock_glm(monkeypatch, mock_client(payloads))
    with TestClient(app) as client:
        request_headers, _ = seed(client, "v2-unknown-tool")
        result = ask(client, request_headers, "比较桌面理线器和便携蓝牙音箱，只比较利润")

    rejected = next(item for item in result["tool_results"] if item["tool_name"] == "unknown_enterprise_tool")
    assert rejected["status"] == "rejected"
    assert rejected["result"]["error_code"] == "TOOL_NOT_ALLOWED"
    assert result["duplicate_tool_execution"] == 0


def test_repeated_successful_tool_call_is_reused(monkeypatch):
    with TestClient(app) as client:
        request_headers, products = seed(client, "v2-repeat-tool")
        ids = [by_title(products, "桌面理线器")["id"], by_title(products, "便携蓝牙音箱")["id"]]
        payloads = [
            {"choices": [{"message": {"content": "", "tool_calls": [glm_tool_call("compare_products", {"product_ids": ids}, "call-1"), glm_tool_call("compare_products", {"product_ids": ids}, "call-2")]}}]},
            {"choices": [{"message": {"content": "mock summary"}}]},
        ]
        configure_mock_glm(monkeypatch, mock_client(payloads))
        result = ask(client, request_headers, "比较桌面理线器和便携蓝牙音箱，只比较利润")

    assert result["duplicate_tool_execution"] == 0
    assert sum(item["event"] == "tool_finished" and item["tool"] == "compare_products" for item in result["trace"]) == 1
    assert sum(item["event"] == "tool_reused" and item["tool"] == "compare_products" for item in result["trace"]) >= 2


@pytest.mark.parametrize(
    ("raised", "expected_code"),
    [(TimeoutError("controlled timeout"), "TOOL_TIMEOUT"), (None, "BACKEND_TOOL_ERROR")],
    ids=["tool_timeout", "backend_tool_error"],
)
def test_backend_tool_failures_are_real_and_audited(monkeypatch, raised, expected_code):
    async def fake_dispatch(*args, **kwargs):
        if raised:
            raise raised
        return {"tool": "get_product", "success": False, "error_code": "BACKEND_TOOL_ERROR", "message": "controlled backend error"}

    monkeypatch.setattr(zhipu_module, "_dispatch_tool", fake_dispatch)
    state = AgentRunState(IntentPolicy(("get_product",), 1, ("detail",)))

    async def run():
        return await zhipu_module._execute_tool(None, "tenant-a", "company_admin", "get_product", {"product_id": "p-1"}, state)

    result, reused = asyncio.run(run())
    assert reused is False
    assert result["success"] is False
    assert result["error_code"] == expected_code
    assert state.actual_tool_calls == 1
    assert state.executions[0]["status"] == "failed"
