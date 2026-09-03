from fastapi.testclient import TestClient

from app.main import app


ADMIN = {"X-Company-ID": "knowledge-company-a", "X-User-ID": "admin-a", "X-Role": "company_admin"}
OTHER = {"X-Company-ID": "knowledge-company-b", "X-User-ID": "admin-b", "X-Role": "company_admin"}


def test_csv_upload_is_idempotent_tenant_scoped_and_searchable():
    content = "商品,净利率,竞争分,结论\n厨房收纳架,35%,28,进入测试\n宠物饮水机,18%,67,暂缓\n".encode("utf-8")
    files = {"file": ("selection-policy.csv", content, "text/csv")}
    with TestClient(app) as client:
        created = client.post("/api/v1/knowledge/documents", headers=ADMIN, files=files)
        duplicate = client.post("/api/v1/knowledge/documents", headers=ADMIN, files=files)
        search = client.post("/api/v1/knowledge/search", headers=ADMIN, json={"query": "利润率30%以上商品", "limit": 5})
        own_documents = client.get("/api/v1/knowledge/documents", headers=ADMIN)
        other_documents = client.get("/api/v1/knowledge/documents", headers=OTHER)

    assert created.status_code == 201
    assert created.json()["data"]["created"] is True
    assert duplicate.json()["data"]["created"] is False
    assert search.status_code == 200
    assert search.json()["data"]["mode"] == "local_hash_embedding_v1"
    assert search.json()["data"]["matches"]
    assert len(own_documents.json()["data"]) == 1
    assert other_documents.json()["data"] == []


def test_unsupported_knowledge_file_is_rejected_safely():
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/knowledge/documents",
            headers=ADMIN,
            files={"file": ("script.exe", b"not allowed", "application/octet-stream")},
        )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "FILE_TYPE_UNSUPPORTED"
