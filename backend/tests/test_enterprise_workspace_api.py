from fastapi.testclient import TestClient

from app.agent import zhipu_agent as zhipu_module
from app.main import app


ADMIN_HEADERS = {
    "X-Company-ID": "workspace-company",
    "X-User-ID": "workspace-admin",
    "X-Role": "company_admin",
}
VIEWER_HEADERS = {**ADMIN_HEADERS, "X-User-ID": "workspace-viewer", "X-Role": "viewer"}


def test_demo_seed_populates_enterprise_dashboard_idempotently():
    with TestClient(app) as client:
        first = client.post("/api/v1/demo/seed", headers=ADMIN_HEADERS, json={"replace_demo": False})
        second = client.post("/api/v1/demo/seed", headers=ADMIN_HEADERS, json={"replace_demo": False})
        overview = client.get("/api/v1/dashboard/overview", headers=ADMIN_HEADERS)

    assert first.status_code == 200
    assert first.json()["data"]["created"] == 60
    assert second.status_code == 200
    assert second.json()["data"]["created"] == 0
    payload = overview.json()["data"]
    assert payload["metrics"]["total_products"] == 60
    assert payload["metrics"]["analyzed_products"] == 60
    assert payload["categories"]
    assert payload["agent"]["status"] in {"unconfigured", "configured", "ready", "error"}
    assert "notice" in payload["agent"]
    assert {item["key"] for item in payload["data_coverage"]} == {
        "identity",
        "demand",
        "profit",
        "competition",
        "compliance",
    }


def test_review_gate_blocks_incomplete_product_and_approves_complete_product():
    headers = {**ADMIN_HEADERS, "X-Company-ID": "review-workspace-company"}
    with TestClient(app) as client:
        seed = client.post("/api/v1/demo/seed", headers=headers, json={"replace_demo": False}).json()["data"]
        products = {item["external_product_id"]: item for item in seed["products"]}
        blocked = client.post(
            f"/api/v1/products/{products['demo-ozon-1002']['id']}/decisions",
            headers=headers,
            json={"action": "approve", "reason": "尝试批准证据不完整商品"},
        )
        approved = client.post(
            f"/api/v1/products/{products['demo-ozon-1001']['id']}/decisions",
            headers=headers,
            json={"action": "approve", "reason": "证据和合规闸门均已通过"},
        )
        decisions = client.get("/api/v1/decisions", headers=headers)

    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "DECISION_GATED"
    assert approved.status_code == 200
    assert approved.json()["data"]["status"] == "approved"
    assert approved.json()["data"]["lifecycle_status"] == "ready_to_list"
    approved_row = next(
        item for item in decisions.json()["data"] if item["product"]["id"] == products["demo-ozon-1001"]["id"]
    )
    assert approved_row["decision"]["status"] == "approved"


def test_workspace_exposes_history_and_role_filtered_tool_registry():
    headers = {**ADMIN_HEADERS, "X-Company-ID": "governance-workspace-company"}
    with TestClient(app) as client:
        products = client.post("/api/v1/demo/seed", headers=headers, json={"replace_demo": False}).json()["data"]["products"]
        history = client.get(f"/api/v1/products/{products[0]['id']}/history", headers=headers)
        admin_tools = client.get("/api/v1/agent/tools", headers=headers)
        viewer_tools = client.get("/api/v1/agent/tools", headers={**VIEWER_HEADERS, "X-Company-ID": headers["X-Company-ID"]})

    assert history.status_code == 200
    assert history.json()["data"]
    admin_compare = next(item for item in admin_tools.json()["data"] if item["name"] == "compare_products")
    viewer_memory = next(item for item in viewer_tools.json()["data"] if item["name"] == "search_company_memory")
    assert admin_compare["allowed"] is True
    assert viewer_memory["allowed"] is False


def test_viewer_cannot_submit_human_decision():
    headers = {**ADMIN_HEADERS, "X-Company-ID": "viewer-review-company"}
    with TestClient(app) as client:
        product = client.post("/api/v1/demo/seed", headers=headers, json={"replace_demo": False}).json()["data"]["products"][0]
        response = client.post(
            f"/api/v1/products/{product['id']}/decisions",
            headers={**VIEWER_HEADERS, "X-Company-ID": headers["X-Company-ID"]},
            json={"action": "approve", "reason": "只读角色不应成功"},
        )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "FORBIDDEN"


def test_zhipu_failure_is_audited_and_dashboard_reports_safe_fallback(monkeypatch):
    class FailingAsyncClient:
        calls = 0

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, *args, **kwargs):
            self.__class__.calls += 1
            raise RuntimeError("401 Unauthorized")

    headers = {**ADMIN_HEADERS, "X-Company-ID": "provider-audit-company"}
    monkeypatch.setattr(zhipu_module.httpx, "AsyncClient", FailingAsyncClient)
    monkeypatch.setattr(zhipu_module.settings, "llm_provider", "zhipu")
    monkeypatch.setattr(zhipu_module.settings, "zhipu_api_key", "test-invalid-key")

    with TestClient(app) as client:
        # Provider failure is reachable only after both identities pass the guard.
        names = ["审计候选甲", "审计候选乙"]
        for index, name in enumerate(names):
            created = client.post("/api/v1/products", headers=headers, json={"external_product_id": f"provider-audit-{index}", "title": name, "current_price": 173})
            assert created.status_code == 201
        answer = client.post(
            "/api/v1/agent/ask",
            headers=headers,
            json={"query": f"比较{names[0]}和{names[1]}"},
        )
        overview = client.get("/api/v1/dashboard/overview", headers=headers)

    assert answer.status_code == 200
    assert FailingAsyncClient.calls == 1
    assert answer.json()["data"]["mode"] == "agent_v3_deterministic_fallback"
    assert answer.json()["data"]["response_mode"] == "deterministic_fallback"
    assert answer.json()["data"]["source_badge"] == "确定性 Planner 回退"
    assert answer.json()["data"]["fallback_reason"] == "AUTHENTICATION_FAILED"
    agent = overview.json()["data"]["agent"]
    assert agent["status"] == "error"
    assert agent["status_code"] == "AUTHENTICATION_FAILED"
    assert "安全回退" in agent["notice"]


def test_deterministic_fast_path_skips_external_model(monkeypatch):
    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class MockAsyncClient:
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
                return FakeResponse({
                    "usage": {"prompt_tokens": 120, "completion_tokens": 20, "total_tokens": 140},
                    "choices": [{"message": {"content": "", "tool_calls": [{"id": "call-1", "function": {"name": "filter_products", "arguments": '{"min_score":60,"sort_by":"recommendation_score","sort_direction":"desc","limit":100}'}}]}}],
                })
            return FakeResponse({
                "usage": {"prompt_tokens": 50, "completion_tokens": 40, "total_tokens": 90},
                "choices": [{"message": {"content": "已完成企业候选筛选，精确数量以后台结构化结果为准。"}}],
            })

    headers = {**ADMIN_HEADERS, "X-Company-ID": "provider-success-company"}
    monkeypatch.setattr(zhipu_module.httpx, "AsyncClient", MockAsyncClient)
    monkeypatch.setattr(zhipu_module.settings, "llm_provider", "zhipu")
    monkeypatch.setattr(zhipu_module.settings, "zhipu_api_key", "mock-key-never-sent")
    with TestClient(app) as client:
        client.post("/api/v1/demo/seed", headers=headers, json={"replace_demo": False})
        answer = client.post("/api/v1/agent/ask", headers=headers, json={"query": "企业有哪些商品分数达到60以上？"})
        status = client.get("/api/v1/agent/status", headers=headers)

    payload = answer.json()["data"]
    assert answer.status_code == 200
    assert payload["response_mode"] == "rule_engine"
    assert payload["source_badge"] == "规则引擎结果"
    assert payload["active_provider"] == "deterministic_planner"
    assert payload["token_usage"] == {}
    assert payload["model_summary"] == ""
    assert MockAsyncClient.calls == 0
    assert payload["matched_count"] == len(payload["products"])
    assert status.json()["data"]["last_call_success"] is None
