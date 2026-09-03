from fastapi.testclient import TestClient

from app.main import app


HEADERS = {
    "X-Company-ID": "evaluation-company",
    "X-User-ID": "evaluation-admin",
    "X-Role": "company_admin",
}


def test_deterministic_agent_evaluation_suite_uses_no_llm_and_is_tenant_scoped():
    with TestClient(app) as client:
        seed = client.post("/api/v1/demo/seed", headers=HEADERS, json={"replace_demo": False})
        run = client.post("/api/v1/evaluations/run", headers=HEADERS)
        summary = client.get("/api/v1/evaluations/summary", headers=HEADERS)
        other_tenant = client.get("/api/v1/evaluations", headers={**HEADERS, "X-Company-ID": "other-evaluation-company"})

    assert seed.status_code == 200
    assert run.status_code == 200
    payload = run.json()["data"]
    assert len(payload["evaluations"]) == 4
    assert all(item["judge_mode"] == "deterministic_no_llm" for item in payload["evaluations"])
    assert all(item["task_success_rate"] == 1 for item in payload["evaluations"])
    assert all(item["answer_accuracy"] == 1 for item in payload["evaluations"])
    assert all(item["tool_calling_accuracy"] == 1 for item in payload["evaluations"])
    assert summary.json()["data"]["evaluation_count"] == 4
    assert summary.json()["data"]["task_success_rate"] == 1
    assert other_tenant.json()["data"] == []
