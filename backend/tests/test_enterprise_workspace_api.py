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
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, *args, **kwargs):
            raise RuntimeError("401 Unauthorized")

    headers = {**ADMIN_HEADERS, "X-Company-ID": "provider-audit-company"}
    monkeypatch.setattr(zhipu_module.httpx, "AsyncClient", FailingAsyncClient)
    monkeypatch.setattr(zhipu_module.settings, "llm_provider", "zhipu")
    monkeypatch.setattr(zhipu_module.settings, "zhipu_api_key", "test-invalid-key")

    with TestClient(app) as client:
        answer = client.post(
            "/api/v1/agent/ask",
            headers=headers,
            json={"query": "连接验证"},
        )
        overview = client.get("/api/v1/dashboard/overview", headers=headers)

    assert answer.status_code == 200
    assert answer.json()["data"]["mode"] == "agent_v1_deterministic_fallback"
    agent = overview.json()["data"]["agent"]
    assert agent["status"] == "error"
    assert agent["status_code"] == "AUTHENTICATION_FAILED"
    assert "安全回退" in agent["notice"]
