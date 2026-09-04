import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent import zhipu_agent as zhipu_module
from app.agent.entity_resolution import resolve_query_entities
from app.main import app


def headers(company: str) -> dict[str, str]:
    return {"X-Company-ID": company, "X-User-ID": "stability-user", "X-Role": "company_admin"}


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


def by_title(products: list[dict], title: str) -> dict:
    return next(item for item in products if title in item["title"])


def ask(client: TestClient, request_headers: dict, query: str, **extra) -> dict:
    response = client.post("/api/v1/agent/ask", headers=request_headers, json={"query": query, **extra})
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_company_product_count_uses_one_deterministic_read(deterministic_client):
    request_headers, products = seed(deterministic_client, "stability-count")
    result = ask(deterministic_client, request_headers, "目前公司一共有多少商品？", selected_product_ids=[item["id"] for item in products[:4]])

    assert result["intent"] == "company_product_count"
    assert result["matched_count"] == len(products) == 60
    assert result["products"] == []
    assert result["tool_call_count"] == 1
    assert result["tool_trace_summary"] == ["1. 统计企业商品总数"]


def test_price_lookup_query_entity_overrides_stale_selection(deterministic_client):
    request_headers, products = seed(deterministic_client, "stability-price")
    target = by_title(products, "桌面理线器")
    result = ask(deterministic_client, request_headers, "桌面理线器售价是多少？", selected_product_ids=[item["id"] for item in products[:4]])

    assert result["intent"] == "product_price"
    assert result["matched_count"] == 1
    assert result["products"][0]["id"] == target["id"]
    assert result["products"][0]["current_price"] == target["current_price"]
    assert result["selection_source"] == "query_entity"
    assert result["tool_call_count"] == 1


def test_entity_resolution_returns_unique_named_product(deterministic_client):
    request_headers, products = seed(deterministic_client, "stability-entity")
    desktop = ask(deterministic_client, request_headers, "桌面理线器售价是多少？")
    speaker = ask(deterministic_client, request_headers, "便携蓝牙音箱售价是多少？")

    assert [item["title"] for item in desktop["products"]] == [by_title(products, "桌面理线器")["title"]]
    assert [item["title"] for item in speaker["products"]] == [by_title(products, "便携蓝牙音箱")["title"]]
    assert "桌面文件收纳架" not in desktop["products"][0]["title"]


def test_profit_only_comparison_obeys_tool_policy_and_budget(deterministic_client):
    request_headers, products = seed(deterministic_client, "stability-profit-only")
    result = ask(
        deterministic_client,
        request_headers,
        "比较桌面理线器和便携蓝牙音箱，只看利润",
        selected_product_ids=[item["id"] for item in products[:4]],
    )

    assert result["intent"] == "profit_comparison"
    assert result["matched_count"] == 2
    assert set(result["requested_dimensions"]) == {"profit", "roi"}
    assert result["tool_call_count"] == 3
    assert all(item["tool"] != "analyze_competition" for item in result["trace"])
    assert {item["title"] for item in result["products"]} == {by_title(products, "桌面理线器")["title"], by_title(products, "便携蓝牙音箱")["title"]}


def test_empty_filter_result_has_one_business_truth(deterministic_client):
    request_headers, _ = seed(deterministic_client, "stability-empty-filter")
    result = ask(deterministic_client, request_headers, "找净利润率99%以上、低风险、合规已通过的商品")

    assert result["intent"] == "product_filter"
    assert result["matched_count"] == 0
    assert result["products"] == []
    assert "0 个" in result["answer"]
    assert result["tool_call_count"] == 1


def test_explicit_count_clears_previous_comparison_state(deterministic_client):
    request_headers, products = seed(deterministic_client, "stability-state")
    selected = [item["id"] for item in products[:4]]
    first = ask(deterministic_client, request_headers, "比较这几个商品", selected_product_ids=selected)
    second = ask(deterministic_client, request_headers, "公司有多少商品？", session_id=first["session_id"], selected_product_ids=selected)

    assert first["intent"] == "product_comparison"
    assert second["intent"] == "company_product_count"
    assert second["matched_count"] == 60
    assert second["products"] == []
    assert second["selection_source"] == "none"


def test_compliance_failure_is_a_hard_gate(deterministic_client):
    request_headers, products = seed(deterministic_client, "stability-compliance")
    rejected = by_title(products, "儿童磁力积木套装")
    policy = ask(deterministic_client, request_headers, "合规没通过的商品能上架吗？", selected_product_ids=[item["id"] for item in products[:4]])
    detail = ask(deterministic_client, request_headers, "儿童磁力积木套装是否值得测试？")

    assert policy["intent"] == "compliance_policy"
    assert policy["tool_call_count"] == 0
    assert policy["products"] == []
    assert "BLOCKED" in policy["answer"]
    assert detail["products"][0]["id"] == rejected["id"]
    assert detail["products"][0]["decision_status"] == "BLOCKED"


def test_relative_rank_is_not_formal_recommendation(deterministic_client):
    request_headers, products = seed(deterministic_client, "stability-ranking")
    selected = [by_title(products, "宠物互动漏食球")["id"], by_title(products, "儿童磁力积木套装")["id"]]
    result = ask(deterministic_client, request_headers, "从这几个候选中选出最值得测试的1个", selected_product_ids=selected)

    assert result["intent"] == "selection_recommendation"
    assert result["decision_summary"]["recommended_count"] == 0
    assert "相对排名第一" in result["answer"]
    assert "无商品达到正式推荐标准" in result["answer"]


def test_single_snapshot_is_insufficient_for_trend(deterministic_client):
    request_headers, _ = seed(deterministic_client, "stability-snapshot")
    result = ask(deterministic_client, request_headers, "桌面理线器有几个销量快照，上涨还是下降？")

    assert result["data_sufficiency"]["status"] == "INSUFFICIENT_DATA"
    assert result["data_sufficiency"]["available"] == 1
    assert "无法判断" in result["answer"]
    assert result["tool_call_count"] == 2


def test_timeout_fallback_reuses_completed_tool_results_without_duplicates(monkeypatch):
    from app.api import router as router_module

    company = "stability-fallback"
    request_headers = headers(company)
    with TestClient(app) as client:
        products = client.post("/api/v1/demo/seed", headers=request_headers, json={"replace_demo": False}).json()["data"]["products"]
        ids = [by_title(products, "桌面理线器")["id"], by_title(products, "便携蓝牙音箱")["id"]]

        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": "", "tool_calls": [{"id": "call-1", "function": {"name": "compare_products", "arguments": __import__("json").dumps({"product_ids": ids})}}]}}]}

        class TimeoutAfterToolClient:
            calls = 0

            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return False

            async def post(self, *args, **kwargs):
                self.__class__.calls += 1
                if self.__class__.calls == 1:
                    return FakeResponse()
                raise httpx.ReadTimeout("mock round-2 timeout")

        monkeypatch.setattr(zhipu_module.httpx, "AsyncClient", TimeoutAfterToolClient)
        monkeypatch.setattr(router_module.settings, "llm_provider", "zhipu")
        monkeypatch.setattr(zhipu_module.settings, "zhipu_api_key", "mock-key-never-sent")
        result = ask(client, request_headers, "比较桌面理线器和便携蓝牙音箱，只看利润")

    assert result["fallback_used"] is True
    assert result["task_completed"] is True
    assert result["fallback_reason"] == "NETWORK_TIMEOUT"
    assert result["duplicate_tool_execution"] == 0
    assert result["tool_call_count"] == 3
    assert sum(item["tool"] == "compare_products" and item["event"] == "tool_finished" for item in result["trace"]) == 1
    assert sum(item["tool"] == "compare_products" and item["event"] == "tool_reused" for item in result["trace"]) == 1
