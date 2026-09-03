"""Single source of truth for Agent tool contracts and permissions.

The registry stores metadata only. Business logic remains in the existing tool
functions, so adopting the registry does not change profit or scoring rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


ToolRiskLevel = Literal["low", "medium", "high"]


class ToolInput(BaseModel):
    """Base class for strict, model-callable tool arguments."""

    model_config = ConfigDict(extra="forbid")


class EmptyInput(ToolInput):
    """Input for tools that do not require arguments."""


class SearchProductsInput(ToolInput):
    keyword: str = Field(default="", max_length=200)


class FilterProductsInput(ToolInput):
    keyword: str = Field(default="", max_length=200)
    min_score: float | None = Field(default=None, ge=0, le=100)
    max_score: float | None = Field(default=None, ge=0, le=100)
    min_margin_rate: float | None = Field(default=None, ge=-1, le=1)
    min_roi: float | None = Field(default=None, ge=-10, le=100)
    max_market_saturation: float | None = Field(default=None, ge=0, le=100)
    brand: str = Field(default="", max_length=255)
    category: str = Field(default="", max_length=500)
    lifecycle_status: str = Field(default="", max_length=32)
    risk_level: str = Field(default="", max_length=32)
    completeness: Literal["all", "complete", "incomplete"] = "all"
    updated_since: datetime | None = None
    sort_by: Literal["recommendation_score", "completeness", "updated_at", "margin_rate"] = "recommendation_score"
    sort_direction: Literal["asc", "desc"] = "desc"
    limit: int = Field(default=20, ge=1, le=100)


class ProductIdInput(ToolInput):
    product_id: str = Field(min_length=1, max_length=64)


class CompareProductsInput(ToolInput):
    product_ids: list[str] = Field(min_length=2, max_length=10)


class SimulatePriceInput(ProductIdInput):
    proposed_price: float = Field(gt=0, le=10_000_000)


class MemorySearchInput(ToolInput):
    query: str = Field(min_length=1, max_length=200)


class ToolOutput(BaseModel):
    """Common result envelope returned by every backend tool."""

    tool: str
    success: bool
    error_code: str | None = None
    message: str = ""


class DictToolOutput(ToolOutput):
    data: dict[str, Any] = Field(default_factory=dict)


class ListToolOutput(ToolOutput):
    data: list[dict[str, Any]] = Field(default_factory=list)


class CompareProductsOutput(ListToolOutput):
    warning: str = ""


class HistoricalFailuresOutput(ListToolOutput):
    notice: str = ""


class MemorySearchOutput(ListToolOutput):
    mode: str = "lexical_memory"


@dataclass(frozen=True)
class RetryPolicy:
    """Declarative retry policy; execution support will be added incrementally."""

    max_attempts: int = 1
    backoff_seconds: float = 0
    retryable_errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolSpec:
    """Stable metadata contract for one backend-controlled Agent tool."""

    tool_name: str
    description: str
    input_schema: type[ToolInput]
    output_schema: type[ToolOutput]
    required_permission: str
    allowed_roles: tuple[str, ...]
    timeout_seconds: float
    retry_policy: RetryPolicy
    idempotent: bool
    risk_level: ToolRiskLevel
    version: str = "1.0.0"

    def allows(self, role: str) -> bool:
        """Return whether a backend-authenticated role may call this tool."""

        return role in self.allowed_roles

    def to_llm_function(self) -> dict[str, Any]:
        """Build an OpenAI-compatible function definition from the same schema."""

        return {
            "type": "function",
            "function": {
                "name": self.tool_name,
                "description": self.description,
                "parameters": self.input_schema.model_json_schema(),
            },
        }


class ToolRegistry:
    """Read-only registry used by permission checks and LLM tool exposure."""

    def __init__(self, specs: tuple[ToolSpec, ...]):
        mapping = {spec.tool_name: spec for spec in specs}
        if len(mapping) != len(specs):
            raise ValueError("Tool names must be unique.")
        self._specs = mapping

    def get(self, tool_name: str) -> ToolSpec | None:
        return self._specs.get(tool_name)

    def require(self, tool_name: str) -> ToolSpec:
        spec = self.get(tool_name)
        if spec is None:
            raise KeyError(f"Unknown tool: {tool_name}")
        return spec

    def allows(self, tool_name: str, role: str) -> bool:
        spec = self.get(tool_name)
        return bool(spec and spec.allows(role))

    def all(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs.values())

    def llm_tools(self, tool_names: tuple[str, ...]) -> list[dict[str, Any]]:
        return [self.require(name).to_llm_function() for name in tool_names]


READ_ROLES = (
    "super_admin",
    "company_admin",
    "analyst",
    "operator",
    "reviewer",
    "viewer",
)
ANALYSIS_ROLES = (
    "super_admin",
    "company_admin",
    "analyst",
    "operator",
    "reviewer",
)
KNOWLEDGE_ROLES = (
    "super_admin",
    "company_admin",
    "analyst",
    "reviewer",
)

NO_RETRY = RetryPolicy()


def _spec(
    tool_name: str,
    description: str,
    input_schema: type[ToolInput],
    output_schema: type[ToolOutput],
    required_permission: str,
    allowed_roles: tuple[str, ...],
    *,
    risk_level: ToolRiskLevel = "low",
) -> ToolSpec:
    return ToolSpec(
        tool_name=tool_name,
        description=description,
        input_schema=input_schema,
        output_schema=output_schema,
        required_permission=required_permission,
        allowed_roles=allowed_roles,
        timeout_seconds=5,
        retry_policy=NO_RETRY,
        idempotent=True,
        risk_level=risk_level,
    )


TOOL_REGISTRY = ToolRegistry(
    (
        _spec(
            "search_products",
            "在当前企业已保存的商品主档中按标题、品牌或类目检索，不能搜索 Ozon 外网。",
            SearchProductsInput,
            ListToolOutput,
            "product.read",
            READ_ROLES,
        ),
        _spec(
            "filter_products",
            "在当前企业商品库中按分数阈值、品牌、类目、生命周期、风险、完整度和更新时间确定性筛选排序。",
            FilterProductsInput,
            ListToolOutput,
            "product.analyze",
            ANALYSIS_ROLES,
        ),
        _spec(
            "get_product",
            "读取当前企业单个商品的主档、字段来源和最近分析。",
            ProductIdInput,
            DictToolOutput,
            "product.read",
            READ_ROLES,
        ),
        _spec(
            "compare_products",
            "比较二至十个当前企业已保存的商品，不补造缺失数据。",
            CompareProductsInput,
            CompareProductsOutput,
            "product.read",
            READ_ROLES,
        ),
        _spec(
            "get_product_history",
            "读取指定商品已保存的历史快照。",
            ProductIdInput,
            ListToolOutput,
            "product.read",
            READ_ROLES,
        ),
        _spec(
            "get_sales_trend",
            "读取指定商品的销量快照趋势；快照不足时不得推测。",
            ProductIdInput,
            DictToolOutput,
            "product.analyze",
            ANALYSIS_ROLES,
        ),
        _spec(
            "get_price_trend",
            "读取指定商品的价格快照趋势；快照不足时不得推测。",
            ProductIdInput,
            DictToolOutput,
            "product.analyze",
            ANALYSIS_ROLES,
        ),
        _spec(
            "analyze_competition",
            "读取竞品数量、当前可见竞品证据和竞争数据完整度。",
            ProductIdInput,
            DictToolOutput,
            "product.analyze",
            ANALYSIS_ROLES,
        ),
        _spec(
            "calculate_profit",
            "执行确定性利润、保本价和目标价计算。",
            ProductIdInput,
            DictToolOutput,
            "product.analyze",
            ANALYSIS_ROLES,
        ),
        _spec(
            "simulate_price_change",
            "仅改变 RUB 售价并重算利润和风险，不预测销量变化。",
            SimulatePriceInput,
            DictToolOutput,
            "product.analyze",
            ANALYSIS_ROLES,
        ),
        _spec(
            "find_historical_failures",
            "按品牌和类目确定性匹配当前企业的历史放弃商品。",
            ProductIdInput,
            HistoricalFailuresOutput,
            "knowledge.read",
            KNOWLEDGE_ROLES,
        ),
        _spec(
            "search_company_memory",
            "在当前企业的文字记忆中按关键词检索。",
            MemorySearchInput,
            MemorySearchOutput,
            "knowledge.read",
            KNOWLEDGE_ROLES,
        ),
        _spec(
            "search_company_knowledge",
            "在当前企业已上传的 CSV、Excel、PDF、TXT 或 Markdown 知识文档中检索可引用片段。",
            MemorySearchInput,
            MemorySearchOutput,
            "knowledge.read",
            KNOWLEDGE_ROLES,
        ),
        _spec(
            "rank_products",
            "按当前确定性动态推荐度排序企业商品。",
            SearchProductsInput,
            ListToolOutput,
            "product.analyze",
            KNOWLEDGE_ROLES,
        ),
    )
)
