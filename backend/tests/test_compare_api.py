from fastapi.testclient import TestClient

from app.main import app


ADMIN_HEADERS = {
    "X-Company-ID": "compare-company",
    "X-User-ID": "compare-admin",
    "X-Role": "company_admin",
}
VIEWER_HEADERS = {
    "X-Company-ID": "compare-company",
    "X-User-ID": "compare-viewer",
    "X-Role": "viewer",
}


def candidate(external_id: str, title: str) -> dict:
    return {
        "external_product_id": external_id,
        "title": title,
        "current_price": 100,
    }


def test_compare_api_returns_two_company_products():
    with TestClient(app) as client:
        company_response = client.get("/api/v1/companies/me", headers=ADMIN_HEADERS)
        assert company_response.status_code == 200
        first = client.post(
            "/api/v1/products",
            headers=ADMIN_HEADERS,
            json=candidate("compare-1", "比较候选 A"),
        ).json()["data"]
        second = client.post(
            "/api/v1/products",
            headers=ADMIN_HEADERS,
            json=candidate("compare-2", "比较候选 B"),
        ).json()["data"]

        response = client.post(
            "/api/v1/products/compare",
            headers=VIEWER_HEADERS,
            json={"product_ids": [first["id"], second["id"]]},
        )

        assert response.status_code == 200
        payload = response.json()["data"]
        assert payload["success"] is True
        assert {item["id"] for item in payload["data"]} == {first["id"], second["id"]}


def test_compare_api_rejects_unknown_role():
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/products/compare",
            headers={**VIEWER_HEADERS, "X-Role": "unknown-role"},
            json={"product_ids": ["p1", "p2"]},
        )

        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "FORBIDDEN"

