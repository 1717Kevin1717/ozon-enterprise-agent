import asyncio
from types import SimpleNamespace

from pydantic import ValidationError

from app.agent.tool_registry import TOOL_REGISTRY
from app.agent.tools import compare_products, get_price_trend
from app.agent.zhipu_agent import ZHIPU_TOOL_NAMES, ZHIPU_TOOLS, _execute_tool
from app.db.models import Product


class FakeProductRepository:
    def __init__(self, products: list[Product], snapshots: list | None = None):
        self.products = {product.id: product for product in products}
        self.snapshots = snapshots or []

    async def get(self, product_id: str) -> Product | None:
        return self.products.get(product_id)

    async def latest_analysis(self, _product_id: str):
        return None

    async def history(self, _product_id: str):
        return self.snapshots


def product(product_id: str, title: str) -> Product:
    return Product(
        id=product_id,
        company_id="company-1",
        external_product_id=f"external-{product_id}",
        title=title,
    )


def test_every_tool_has_an_explainable_contract():
    specs = TOOL_REGISTRY.all()

    assert specs
    assert len({spec.tool_name for spec in specs}) == len(specs)
    for spec in specs:
        assert spec.description
        assert spec.required_permission
        assert spec.timeout_seconds > 0
        assert spec.version
        assert spec.risk_level in {"low", "medium", "high"}
        assert spec.input_schema.model_json_schema()["type"] == "object"
        assert spec.output_schema.model_json_schema()["type"] == "object"


def test_compare_products_is_registered_for_read_roles():
    spec = TOOL_REGISTRY.require("compare_products")

    assert spec.required_permission == "product.read"
    assert spec.idempotent is True
    assert spec.allows("viewer") is True
    assert spec.allows("unknown-role") is False


def test_compare_products_input_rejects_too_few_products():
    spec = TOOL_REGISTRY.require("compare_products")

    try:
        spec.input_schema.model_validate({"product_ids": ["only-one"]})
    except ValidationError:
        pass
    else:
        raise AssertionError("compare_products must require at least two product IDs")


def test_compare_products_executes_for_viewer():
    repo = FakeProductRepository([product("p1", "候选 A"), product("p2", "候选 B")])

    result = asyncio.run(compare_products(repo, ["p1", "p2"], "viewer"))

    assert result["success"] is True
    assert [item["id"] for item in result["data"]] == ["p1", "p2"]


def test_compare_products_rejects_unknown_role():
    repo = FakeProductRepository([product("p1", "候选 A"), product("p2", "候选 B")])

    result = asyncio.run(compare_products(repo, ["p1", "p2"], "unknown-role"))

    assert result["success"] is False
    assert result["error_code"] == "FORBIDDEN"


def test_registered_price_trend_has_a_real_implementation():
    snapshots = [
        SimpleNamespace(captured_at="2026-08-01", price=100, sales_30d=0, rating=0, review_count=0, competitor_count=0),
        SimpleNamespace(captured_at="2026-09-01", price=120, sales_30d=0, rating=0, review_count=0, competitor_count=0),
    ]
    repo = FakeProductRepository([product("p1", "候选 A")], snapshots)

    result = asyncio.run(get_price_trend(repo, "p1", "analyst"))

    assert result["success"] is True
    assert result["data"]["trend"] == "rising"


def test_llm_tool_schemas_come_from_registry():
    exposed_names = tuple(item["function"]["name"] for item in ZHIPU_TOOLS)

    assert exposed_names == ZHIPU_TOOL_NAMES
    for name in exposed_names:
        assert TOOL_REGISTRY.require(name).to_llm_function() in ZHIPU_TOOLS


def test_llm_invalid_arguments_are_rejected_before_execution():
    result = asyncio.run(
        _execute_tool(
            session=None,
            company_id="company-1",
            role="viewer",
            name="compare_products",
            arguments={"product_ids": ["only-one"]},
        )
    )

    assert result["success"] is False
    assert result["error_code"] == "INVALID_ARGUMENTS"
