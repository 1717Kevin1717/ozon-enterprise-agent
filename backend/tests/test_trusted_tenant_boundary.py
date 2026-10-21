"""API and repository boundary tests without real credentials or external services."""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.tenant_context import VerifiedPrincipal, get_verified_principal, require_local_demo_database
from app.db.models import Base, Company, ConversationSession, User
from app.db.session import SessionLocal
from app.main import app
from app.repositories.products import ProductRepository
from app.schemas.products import ProductIn, ProductPatch


@pytest.fixture
def production(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "llm_provider", "disabled")
    # Even a misconfigured demo flag cannot activate header identity in production.
    monkeypatch.setattr(settings, "demo_identity_enabled", True)
    current = {"principal": None}
    app.dependency_overrides[get_verified_principal] = lambda: current["principal"]
    try:
        yield current
    finally:
        app.dependency_overrides.pop(get_verified_principal, None)


def _principal(company: str, *, user: str):
    return VerifiedPrincipal(user_id=user, active_company_id=company)


async def _provision(company: str, user: str, role: str = "company_admin"):
    async with SessionLocal() as db:
        db.add(Company(id=company, name=company, status="active"))
        db.add(User(id=user, company_id=company, username=user, role=role, status="active"))
        await db.commit()


def test_production_fails_closed_even_with_forged_headers_and_demo_flag(production):
    forged = {"X-Company-ID": "target-company", "X-Tenant-ID": "target-company",
              "X-User-ID": "target-admin", "X-Role": "super_admin"}
    with TestClient(app) as client:
        for method, path, payload in (
            ("get", "/api/v1/products", None),
            ("post", "/api/v1/agent/sessions", {}),
            ("post", "/api/v1/agent/ask", {"query": "公司有多少商品"}),
            ("post", "/api/v1/demo/seed", {"replace_demo": False}),
        ):
            response = getattr(client, method)(path, headers=forged, json=payload) if payload is not None else client.get(path, headers=forged)
            assert response.status_code == 401
            assert response.json()["detail"]["code"] == "AUTHENTICATION_REQUIRED"
        health = client.get("/api/v1/health")
        assert health.status_code == 200
        assert health.json()["data"] == {"status": "ok", "auth_mode": "verified_required"}
    assert production["principal"] is None


def test_verified_subject_authorization_cannot_be_changed_by_headers(production):
    forged = {"X-Company-ID": "different-company", "X-Tenant-ID": "different-company",
              "X-User-ID": "forged-user", "X-Role": "super_admin"}
    with TestClient(app) as client:
        asyncio.run(_provision("tenant-a", "tenant-a-viewer", "viewer"))
        production["principal"] = _principal("tenant-a", user="tenant-a-viewer")
        me = client.get("/api/v1/companies/me", headers=forged)
        assert me.status_code == 200
        assert me.json()["data"]["id"] == "tenant-a"
        assert me.json()["data"]["role"] == "viewer"
        create = client.post("/api/v1/products", headers=forged,
                             json={"external_product_id": "tenant-bound-item", "title": "tenant-bound", "current_price": 100})
        assert create.status_code == 403
        production["principal"] = _principal("tenant-b", user="tenant-a-viewer")
        assert client.get("/api/v1/products", headers=forged).status_code == 403
        production["principal"] = _principal("tenant-a", user="")
        assert client.get("/api/v1/products", headers=forged).status_code == 403
        production["principal"] = _principal("tenant-a", user="unknown-user")
        assert client.get("/api/v1/products", headers=forged).status_code == 403


def test_cross_tenant_product_snapshot_analysis_and_decision_are_inaccessible(production):
    with TestClient(app) as client:
        asyncio.run(_provision("tenant-products-a", "tenant-products-user-a"))
        asyncio.run(_provision("tenant-products-b", "tenant-products-user-b"))
        production["principal"] = _principal("tenant-products-a", user="tenant-products-user-a")
        created = client.post("/api/v1/products", json={"external_product_id": "shared-offer",
            "title": "A-private-product", "current_price": 200, "procurement_cost": 50})
        assert created.status_code == 201
        product_id = created.json()["data"]["id"]
        assert client.post(f"/api/v1/products/{product_id}/analyze").status_code == 200
        assert client.post(f"/api/v1/products/{product_id}/decisions",
                           json={"action": "needs_more_data", "reason": "Review privately"}).status_code == 200
        production["principal"] = _principal("tenant-products-b", user="tenant-products-user-b")
        for path in (f"/api/v1/products/{product_id}", f"/api/v1/products/{product_id}/history",
                     f"/api/v1/products/{product_id}/analysis"):
            assert client.get(path).status_code == 404
        assert client.patch(f"/api/v1/products/{product_id}", json={"procurement_cost": 1}).status_code == 404
        assert client.post(f"/api/v1/products/{product_id}/analyze").status_code == 404
        assert client.post(f"/api/v1/products/{product_id}/decisions",
                           json={"action": "reject", "reason": "foreign write"}).status_code == 404
        assert client.get("/api/v1/products").json()["data"] == []
        assert client.get("/api/v1/decisions").json()["data"] == []
        own = client.post("/api/v1/products", json={"external_product_id": "shared-offer",
            "title": "B-own-product", "current_price": 80})
        assert own.status_code == 201
        assert own.json()["data"]["id"] != product_id
        assert "A-private-product" not in str(client.post("/api/v1/products/compare",
            json={"product_ids": [product_id, own.json()["data"]["id"]]}).json())
        via_agent = client.post("/api/v1/agent/ask", json={"query": "这两个商品售价是多少？",
            "selected_product_ids": [product_id, own.json()["data"]["id"]]})
        assert via_agent.status_code == 200
        assert product_id not in via_agent.json()["data"].get("product_ids", [])
        assert "A-private-product" not in str(via_agent.json())


def test_foreign_session_id_cannot_read_or_mutate_collection_or_scenario_state(production):
    with TestClient(app) as client:
        asyncio.run(_provision("tenant-session-a", "user-a"))
        asyncio.run(_provision("tenant-session-b", "user-b"))
        production["principal"] = _principal("tenant-session-a", user="user-a")
        created = client.post("/api/v1/agent/sessions")
        assert created.status_code == 200
        session_id = created.json()["data"]["id"]

        async def install_state():
            async with SessionLocal() as db:
                row = await db.scalar(select(ConversationSession).where(ConversationSession.id == session_id))
                row.state_json = {"task_state": {"active_collection": {"product_ids": ["private-product"]},
                                                  "active_result_set": {"product_ids": ["private-product"]}},
                                  "scenario_state": {"scenario_id": "private-scenario"}}
                await db.commit()
        asyncio.run(install_state())

        production["principal"] = _principal("tenant-session-b", user="user-b")
        assert client.get(f"/api/v1/agent/sessions/{session_id}/messages").status_code == 404
        assert client.get("/api/v1/agent/sessions").json()["data"] == []
        blocked = client.post("/api/v1/agent/ask", json={"session_id": session_id,
            "query": "现在第一名是谁", "selected_product_ids": ["private-product"]})
        assert blocked.status_code == 404
        assert blocked.json()["detail"]["code"] == "SESSION_NOT_FOUND"
        assert client.get("/api/v1/agent/sessions").json()["data"] == []

        async def read_state():
            async with SessionLocal() as db:
                row = await db.scalar(select(ConversationSession).where(ConversationSession.id == session_id))
                return row.state_json
        preserved = asyncio.run(read_state())
        assert preserved["scenario_state"]["scenario_id"] == "private-scenario"
        assert preserved["task_state"]["active_result_set"]["product_ids"] == ["private-product"]


def test_demo_requires_server_flag_and_local_database(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "development")
    monkeypatch.setattr(settings, "demo_identity_enabled", False)
    with TestClient(app) as client:
        assert client.get("/api/v1/products", headers={"X-Role": "super_admin"}).status_code == 401
        assert client.post("/api/v1/demo/seed", headers={"X-Role": "super_admin"},
                           json={"replace_demo": False}).status_code == 401
    with pytest.raises(RuntimeError, match="isolated local SQLite"):
        require_local_demo_database("postgresql://unused.invalid/not-called", workspace=Path("E:/OZON AI 选品/backend"))
    with pytest.raises(RuntimeError, match="under this backend"):
        require_local_demo_database("sqlite+aiosqlite:///../outside.db", workspace=Path("E:/OZON AI 选品/backend"))


def test_demo_mode_keeps_existing_mock_api(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "test")
    monkeypatch.setattr(settings, "demo_identity_enabled", True)
    with TestClient(app) as client:
        response = client.post("/api/v1/demo/seed", headers={"X-Company-ID": "tenant-demo-b",
            "X-User-ID": "demo-admin", "X-Role": "company_admin"}, json={"replace_demo": False})
        assert response.status_code == 200
        assert response.json()["data"]["created"] == 60
        assert response.json()["data"]["products"][0]["data_trust"]["is_mock"] is True


def test_repository_rejects_foreign_product_object_for_write_and_analysis():
    async def run():
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            async with sessions() as db:
                owner = ProductRepository(db, "repo-tenant-a")
                foreign = ProductRepository(db, "repo-tenant-b")
                product = await owner.upsert(ProductIn(external_product_id="same-external-id", title="A private item", current_price=100))
                assert await foreign.get(product.id) is None
                assert await foreign.get_by_external_id("same-external-id") is None
                with pytest.raises(ValueError, match="does not belong"):
                    await foreign.patch(product, ProductPatch(current_price=1))
                with pytest.raises(ValueError, match="does not belong"):
                    await foreign.analyze(product)
                assert await foreign.history(product.id) == []
                assert (await owner.get(product.id)).current_price == 100
        finally:
            await engine.dispose()
    asyncio.run(run())
