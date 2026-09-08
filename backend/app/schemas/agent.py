from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.schemas.provenance import FieldEvidence, TrustNotice


ResponseMode = Literal[
    "glm_success",
    "deterministic_fallback",
    "rule_engine",
]

ResponseType = Literal[
    "provenance_fact", "calculation_explanation", "decision_explanation", "data_quality_answer",
    "simple_fact",
    "product_detail",
    "filter_result",
    "comparison_result",
    "decision_report",
    "policy_answer",
    "insufficient_data",
    "clarification",
    "not_found",
    "error",
]


IntentName = Literal[
    "company_product_count", "product_price", "product_filter", "product_detail",
    "profit_comparison", "product_comparison", "compliance_policy", "recommendation_policy",
    "selection_recommendation", "provenance_fact", "calculation_explanation",
    "decision_explanation", "data_quality_answer", "data_quality_policy", "unknown",
]


class QueryUnderstanding(BaseModel):
    """Understanding is a plan, never a source of business facts."""
    model_config = ConfigDict(extra="forbid")
    intent: IntentName
    question_type: str = ""
    entity_mentions: list[str] = Field(default_factory=list, max_length=10)
    references: list[str] = Field(default_factory=list, max_length=10)
    reference_field: str | None = None
    metrics: list[str] = Field(default_factory=list)
    metric_values: list[float] = Field(default_factory=list)
    requested_dimensions: list[str] = Field(default_factory=list)
    comparison_requested: bool = False
    explanation_requested: bool = False
    provenance_requested: bool = False
    calculation_requested: bool = False
    policy_requested: bool = False
    requires_context: bool = False
    confidence: float = Field(ge=0, le=1)
    route: Literal["DETERMINISTIC_FAST_PATH", "SEMANTIC_PLANNER", "CLARIFICATION"]
    planned_tools: list[str] = Field(default_factory=list, max_length=10)
    planner_calls: int = Field(default=0, ge=0, le=1)
    failure_code: str | None = None


class EntityCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str
    name: str
    confidence: float = Field(ge=0, le=1)


class EntityResolutionResult(BaseModel):
    """One requested mention, including unsuccessful resolution, not a search hit."""

    model_config = ConfigDict(extra="forbid")

    mention: str
    status: Literal["EXACT_MATCH", "NORMALIZED_MATCH", "UNIQUE_ALIAS_MATCH", "FUZZY_UNIQUE_MATCH", "AMBIGUOUS", "NOT_FOUND", "LOW_CONFIDENCE"]
    product_id: str | None = None
    name: str = ""
    confidence: float = Field(default=0, ge=0, le=1)
    candidates: list[EntityCandidate] = Field(default_factory=list, max_length=5)

    @property
    def resolved(self) -> bool:
        return self.status in {"EXACT_MATCH", "NORMALIZED_MATCH", "UNIQUE_ALIAS_MATCH", "FUZZY_UNIQUE_MATCH"}

    @model_validator(mode="after")
    def validate_resolution_identity(self):
        if self.resolved != bool(self.product_id):
            raise ValueError("Only a resolved entity may carry a product identity")
        if self.resolved and (not self.name or self.candidates):
            raise ValueError("Resolved entities require a name, not candidate choices")
        minimum = 2 if self.status == "AMBIGUOUS" else 1 if self.status == "LOW_CONFIDENCE" else 0
        if len(self.candidates) < minimum or (self.status == "NOT_FOUND" and self.candidates):
            raise ValueError("Resolution status and candidate count disagree")
        return self


class AgentEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_type: Literal["product"] = "product"
    product_id: str
    name: str
    resolution_method: Literal["query_entity", "explicit_selection", "session_reference"]


class AgentToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str
    normalized_args: dict[str, Any] = Field(default_factory=dict)
    status: str
    result: dict[str, Any] = Field(default_factory=dict)


class AgentFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    value: Any
    unit: str = ""
    product_id: str = ""
    currency: str = ""
    source: str
    timestamp: datetime | None = None
    provenance: FieldEvidence | None = None


class DataSufficiencyResult(BaseModel):
    """Keep reported metrics separate from trends observed from saved snapshots."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["SUFFICIENT", "INSUFFICIENT_DATA"]
    observed_trend: Literal["GROWING", "DECLINING", "FLAT", "INSUFFICIENT_DATA"]
    snapshot_count: int = Field(ge=0)
    valid_sales_snapshot_count: int = Field(ge=0)
    minimum_required: int = Field(ge=2)
    available: int = Field(ge=0)
    latest_snapshot_at: datetime | None = None
    stale: bool = False
    reported_metric: float | None = None
    reported_metric_source: str = "not_provided"
    notice: str = ""


class AgentProduct(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    external_product_id: str
    title: str
    brand: str = ""
    category_path: str = ""
    score: float
    recommendation_grade: str = "C"
    decision_status: str = "REVIEW_REQUIRED"
    profit_score: float = 0
    demand_score: float = 0
    competition_score: float = 0
    compliance_score: float = 0
    risk_score: float = 0
    confidence: float = Field(ge=0, le=1)
    completeness: int = Field(ge=0, le=100)
    risk_level: str
    compliance_status: str = "pending"
    lifecycle_status: str
    current_price: float = 0
    currency: str = "RUB"
    price_source: str = "company_product_database"
    updated_at: datetime | None = None
    current_margin_rate: float = 0
    net_profit: float = 0
    roi: float = 0
    missing_fields: list[str] = Field(default_factory=list)
    url: str = ""
    main_image_url: str = ""
    is_mock: bool = False
    data_disclosure: str = ""


class AgentEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str
    title: str
    source: str
    summary: str
    url: str = ""
    fields: list[FieldEvidence] = Field(default_factory=list)
    disclosure: str = ""

    @model_validator(mode="after")
    def evidence_product_binding(self):
        if any(item.product_id != self.product_id for item in self.fields):
            raise ValueError("Agent evidence product mismatch")
        return self


class AgentRunResult(BaseModel):
    """Stable response contract shared by GLM and deterministic fallback."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    session_id: str
    response_type: ResponseType
    answer: str
    matched_count: int = Field(ge=0)
    total_count: int = Field(ge=0)
    displayed_count: int = Field(ge=0)
    entities: list[AgentEntity] = Field(default_factory=list)
    requested_entities: list[EntityResolutionResult] = Field(default_factory=list)
    product_ids: list[str] = Field(default_factory=list)
    filter_criteria: dict[str, Any] = Field(default_factory=dict)
    fact: AgentFact | None = None
    products: list[AgentProduct] = Field(default_factory=list)
    tool_results: list[AgentToolResult] = Field(default_factory=list)
    evidence: list[AgentEvidence] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    trust_notices: list[TrustNotice] = Field(default_factory=list)
    missing_data: list[dict[str, Any]] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    requires_human_review: bool = False
    human_review_required: bool = False
    tool_trace_summary: list[str] = Field(default_factory=list)
    response_mode: ResponseMode
    source_badge: str
    active_provider: str
    active_model: str
    fallback_reason: str | None = None
    latency_ms: int = Field(ge=0)
    token_usage: dict[str, Any] = Field(default_factory=dict)
    intent: str
    understanding: QueryUnderstanding | None = None
    task_completed: bool = True
    fallback_used: bool = False
    tool_call_count: int = Field(default=0, ge=0)
    duplicate_tool_execution: int = Field(default=0, ge=0)
    requested_dimensions: list[str] = Field(default_factory=list)
    display_scope: list[str] = Field(default_factory=list)
    selection_source: str = "none"
    decision_status: str = "NOT_APPLICABLE"
    decision_summary: dict[str, Any] = Field(default_factory=dict)
    data_sufficiency: DataSufficiencyResult | None = None

    # Compatibility fields retained for the existing API and UI during V2 migration.
    mode: str
    conclusion: str
    trace: list[dict[str, Any]] = Field(default_factory=list)
    simulation: dict[str, Any] | None = None
    data_completeness: dict[str, Any] | None = None
    provider_notice: str = ""
    model_summary: str = ""

    @model_validator(mode="after")
    def validate_evidence_bindings(self):
        identities = set(self.product_ids)
        if any(item.product_id not in identities for item in self.evidence):
            raise ValueError("Evidence must refer to a returned product")
        if self.fact and self.fact.provenance and self.fact.product_id != self.fact.provenance.product_id:
            raise ValueError("Fact and evidence product identity disagree")
        return self


def validate_agent_answer(payload: dict[str, Any]) -> dict[str, Any]:
    return AgentRunResult.model_validate(payload).model_dump(mode="json")


# V2 compatibility alias: existing API imports continue to work while every
# consumer now receives the single AgentRunResult business truth.
AgentAnswer = AgentRunResult
