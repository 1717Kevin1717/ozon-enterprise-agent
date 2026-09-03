from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


ResponseMode = Literal[
    "glm_success",
    "deterministic_fallback",
    "rule_engine",
]


class AgentProduct(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    external_product_id: str
    title: str
    brand: str = ""
    category_path: str = ""
    score: float
    recommendation_grade: str = "C"
    profit_score: float = 0
    demand_score: float = 0
    competition_score: float = 0
    compliance_score: float = 0
    risk_score: float = 0
    confidence: float = Field(ge=0, le=1)
    completeness: int = Field(ge=0, le=100)
    risk_level: str
    lifecycle_status: str
    current_price: float = 0
    current_margin_rate: float = 0
    net_profit: float = 0
    roi: float = 0
    missing_fields: list[str] = Field(default_factory=list)
    url: str = ""
    main_image_url: str = ""


class AgentEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str
    title: str
    source: str
    summary: str
    url: str = ""


class AgentAnswer(BaseModel):
    """Stable response contract shared by GLM and deterministic fallback."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    answer: str
    matched_count: int = Field(ge=0)
    products: list[AgentProduct] = Field(default_factory=list)
    evidence: list[AgentEvidence] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    missing_data: list[dict[str, Any]] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    requires_human_review: bool = True
    tool_trace_summary: list[str] = Field(default_factory=list)
    response_mode: ResponseMode
    source_badge: str
    active_provider: str
    active_model: str
    fallback_reason: str | None = None
    latency_ms: int = Field(ge=0)
    token_usage: dict[str, Any] = Field(default_factory=dict)
    intent: str

    # Compatibility fields retained for the existing API and UI during V2 migration.
    mode: str
    conclusion: str
    trace: list[dict[str, Any]] = Field(default_factory=list)
    simulation: dict[str, Any] | None = None
    data_completeness: dict[str, Any] | None = None
    provider_notice: str = ""
    model_summary: str = ""


def validate_agent_answer(payload: dict[str, Any]) -> dict[str, Any]:
    return AgentAnswer.model_validate(payload).model_dump(mode="json")
