from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from app.schemas.provenance import FieldEvidence, TrustNotice
from app.schemas.semantic_vocabulary import (
    CANONICAL_DIMENSIONS,
    CANONICAL_INTENTS,
    CANONICAL_METRICS,
)


ResponseMode = Literal[
    "glm_success",
    "model_success",
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

ContextSource = Literal[
    "explicit_query", "ui_selection", "last_explicit_entity", "last_comparison",
    "last_resolved_entity", "ordinal_reference", "session_state", "none", "conflict", "invalid",
]

TaskOperation = Literal[
    "CREATE", "CONTINUE", "SWITCH", "REFINE", "ADD_CONSTRAINT",
    "UPDATE_CONSTRAINT", "REMOVE_CONSTRAINT", "REORDER", "RERUN",
    "INSPECT", "COMPARE", "RECOMMEND", "EXPLAIN", "RECOVER",
    "SIMULATE", "CANCEL",
    "ARGMAX", "ARGMIN", "EXPLAIN_RANKING",
]

EntityRole = Literal[
    "EXPLICIT_PRODUCT", "PRODUCT_ALIAS", "PRONOUN_REFERENCE",
    "ORDINAL_REFERENCE", "RESULT_SET_REFERENCE", "COMPARISON_REFERENCE",
    "TASK_REFERENCE", "SOURCE_SCOPE", "METRIC_REFERENCE", "NON_ENTITY_TEXT",
]


class ResultSetState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_ids: list[str] = Field(default_factory=list, max_length=100)
    source_task_type: str
    filter_spec: dict[str, Any] = Field(default_factory=dict)
    sort_spec: list[str] = Field(default_factory=list, max_length=10)
    ranking_dimensions: list[str] = Field(default_factory=list, max_length=10)
    source_task_revision: int = Field(default=0, ge=0)
    created_turn: int = Field(ge=1)


class TaskEntitySlot(BaseModel):
    """Session-local entity resolution outcome retained for safe recovery."""

    model_config = ConfigDict(extra="forbid")
    mention: str = Field(min_length=1, max_length=300)
    status: Literal[
        "EXACT_MATCH", "NORMALIZED_MATCH", "UNIQUE_ALIAS_MATCH",
        "FUZZY_UNIQUE_MATCH", "AMBIGUOUS", "NOT_FOUND", "LOW_CONFIDENCE",
    ]
    product_id: str | None = None
    display_name: str | None = None


class ComparisonState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product_ids: list[str] = Field(default_factory=list, max_length=10)
    dimensions: list[str] = Field(default_factory=list, max_length=10)
    revision: int = Field(default=1, ge=1)
    created_turn: int = Field(default=1, ge=1)
    source_task: str = "product_comparison"


class RecommendationState(BaseModel):
    """Backend-owned recommendation result retained for follow-up references."""

    model_config = ConfigDict(extra="forbid")
    selected_product_id: str
    selected_product_display_name: str = ""
    candidate_product_ids: list[str] = Field(default_factory=list, max_length=100)
    ordered_candidates: list[str] = Field(default_factory=list, max_length=100)
    recommendation_score: float | None = None
    decision_status: str = "REVIEW_REQUIRED"
    gate_status: str = "REVIEW_REQUIRED"
    requested_dimensions: list[str] = Field(default_factory=list, max_length=10)
    evidence_fields: list[str] = Field(default_factory=list, max_length=50)
    freshness_summary: dict[str, int] = Field(default_factory=dict)
    readiness_summary: dict[str, Any] = Field(default_factory=dict)
    requires_human_review: bool = True
    source_task_revision: int = Field(default=0, ge=0)


class TaskState(BaseModel):
    """Session-scoped, backend-controlled task continuity; never LLM facts."""

    model_config = ConfigDict(extra="forbid")
    schema_version: str = "task-state-v1"
    task_id: str = ""
    revision: int = Field(default=0, ge=0)
    task_type: str = ""
    operation: TaskOperation = "CREATE"
    status: Literal["EMPTY", "ACTIVE", "COMPLETED", "NEEDS_CLARIFICATION"] = "EMPTY"
    active_entities: list[str] = Field(default_factory=list, max_length=10)
    pending_entities: list[TaskEntitySlot] = Field(default_factory=list, max_length=10)
    active_result_set: ResultSetState | None = None
    active_comparison_set: ComparisonState | None = None
    active_recommendation: RecommendationState | None = None
    filter_spec: dict[str, Any] = Field(default_factory=dict)
    sort_spec: list[str] = Field(default_factory=list, max_length=10)
    ranking_spec: list[str] = Field(default_factory=list, max_length=10)
    requested_dimensions: list[str] = Field(default_factory=list, max_length=10)
    source_scope: str = "enterprise_catalog"
    last_successful_action: str | None = None
    last_execution_product_ids: list[str] = Field(default_factory=list, max_length=100)
    pending_clarification: str | None = None


class SemanticTaskContextPacket(BaseModel):
    """Anonymous task metadata exposed to semantic providers; never business facts."""

    model_config = ConfigDict(extra="forbid")
    active_task_type: str = ""
    active_operation: TaskOperation = "CREATE"
    active_entities_display_names: list[str] = Field(default_factory=list, max_length=10)
    result_set_count: int = Field(default=0, ge=0, le=100)
    comparison_count: int = Field(default=0, ge=0, le=10)
    active_filter_spec: dict[str, Any] = Field(default_factory=dict)
    active_sort_spec: list[str] = Field(default_factory=list, max_length=10)
    active_dimensions: list[str] = Field(default_factory=list, max_length=10)
    ordinal_capacity: int = Field(default=0, ge=0, le=100)
    has_pending_unresolved_entity: bool = False
    pending_entity_statuses: list[str] = Field(default_factory=list, max_length=10)
    has_active_recommendation: bool = False
    recommendation_candidate_count: int = Field(default=0, ge=0, le=100)
    has_active_scenario: bool = False
    last_successful_action: str | None = None


class ContextTarget(BaseModel):
    """A verified product identity that can be referenced by a later turn."""

    model_config = ConfigDict(extra="forbid")

    product_id: str
    source: ContextSource
    turn_index: int = Field(ge=0)


class ContextSnapshot(BaseModel):
    """Typed short-term references; never a copy of enterprise facts."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "context-v1"
    session_id: str
    company_id: str
    user_id: str
    turn_index: int = Field(default=1, ge=1)
    current_query: str
    current_selected_product_ids: list[str] = Field(default_factory=list, max_length=10)
    selection_revision: int = Field(default=0, ge=0)
    selection_bound_session_id: str | None = None
    last_explicit_product_ids: list[str] = Field(default_factory=list, max_length=10)
    last_resolved_product_ids: list[str] = Field(default_factory=list, max_length=10)
    last_intent: str | None = None
    last_response_type: str | None = None
    last_requested_dimensions: list[str] = Field(default_factory=list, max_length=10)
    last_metric: str | None = None
    last_fact_dimension: str | None = None
    last_comparison_product_ids: list[str] = Field(default_factory=list, max_length=10)
    last_comparison_order: list[str] = Field(default_factory=list, max_length=10)
    last_tool_result_refs: list[str] = Field(default_factory=list, max_length=20)
    last_preference_order: list[str] = Field(default_factory=list, max_length=10)
    last_negative_scope: list[str] = Field(default_factory=list, max_length=10)
    context_warnings: list[str] = Field(default_factory=list, max_length=10)
    task_state: TaskState = Field(default_factory=TaskState)


class ReferenceResolutionResult(BaseModel):
    """The product targets selected from explicit entities or short-term context."""

    model_config = ConfigDict(extra="forbid")

    product_ids: list[str] = Field(default_factory=list, max_length=10)
    source: ContextSource = "none"
    reference_expression: str | None = None
    inherited_dimensions: list[str] = Field(default_factory=list, max_length=10)
    confidence: float = Field(default=0, ge=0, le=1)
    requires_clarification: bool = False
    failure_code: str | None = None


IntentName = Literal[*CANONICAL_INTENTS]


class QueryUnderstanding(BaseModel):
    """Understanding is a plan, never a source of business facts."""
    model_config = ConfigDict(extra="forbid")
    intent: IntentName
    task_type: str = ""
    operation: TaskOperation = "CREATE"
    question_type: str = ""
    entity_mentions: list[str] = Field(
        default_factory=list,
        max_length=10,
        description=(
            "Literal product names, product aliases, or product identifiers explicitly present in the current query only; "
            "never pronouns, ordinal references, metrics, dimensions, or references to a previous result."
        ),
    )
    entity_roles: list[EntityRole] = Field(default_factory=list, max_length=10)
    references: list[str] = Field(default_factory=list, max_length=10)
    reference_slots: list[ContextSource] = Field(default_factory=list, max_length=10)
    reference_field: str | None = None
    metrics: list[str] = Field(
        default_factory=list,
        description=f"Specific metric values; canonical values: {', '.join(CANONICAL_METRICS)}",
    )
    metric: str | None = Field(
        default=None,
        description=f"Primary specific metric; canonical values: {', '.join(CANONICAL_METRICS)}",
    )
    metric_values: list[float] = Field(default_factory=list)
    requested_dimensions: list[str] = Field(
        default_factory=list,
        description=f"Business analysis areas; canonical values: {', '.join(CANONICAL_DIMENSIONS)}",
    )
    constraints: dict[str, Any] = Field(default_factory=dict)
    filter_updates: dict[str, Any] = Field(default_factory=dict)
    sort_updates: list[str] = Field(default_factory=list, max_length=10)
    result_set_reference: bool = False
    ordinal_reference: int | None = Field(default=None, ge=1, le=100)
    negative_scope: list[str] = Field(default_factory=list, max_length=20)
    preference_order: list[str] = Field(default_factory=list, max_length=10)
    comparison_requested: bool = False
    comparison_required: bool = False
    explanation_requested: bool = False
    provenance_requested: bool = False
    calculation_requested: bool = False
    policy_requested: bool = False
    requires_context: bool = False
    requires_task_state: bool = False
    requires_tools: bool = False
    requires_reasoning: bool = False
    clarification_required: bool = False
    clarification_reason: str | None = None
    confidence: float = Field(ge=0, le=1)
    route: Literal["DETERMINISTIC_FAST_PATH", "SEMANTIC_PLANNER", "CLARIFICATION"]
    planned_tools: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="Advisory tool suggestions only; the backend derives and authorizes the executable tool plan.",
    )
    advisory_tool_mismatch: list[str] = Field(default_factory=list, max_length=10)
    planner_calls: int = Field(default=0, ge=0, le=1)
    failure_code: str | None = None
    semantic_provider: str | None = None


class ModelInput(BaseModel):
    """Future-safe provider input contract; URLs/opaque references only, no media bytes."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=20000)
    images: list[str] = Field(default_factory=list, max_length=5)
    videos: list[str] = Field(default_factory=list, max_length=2)

    @property
    def has_multimodal_input(self) -> bool:
        return bool(self.images or self.videos)


class ProviderCallAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requested_provider: str
    actual_provider: str
    model_name: str
    route: str
    status: Literal["success", "failed", "skipped"]
    failure_code: str | None = None
    exception_class: str | None = None
    failure_stage: str | None = None
    upstream_status: int | None = Field(default=None, ge=100, le=599)
    validation_rule_id: str | None = None
    validation_reason_code: str | None = None
    validation_field: str | None = None
    latency_ms: int | None = Field(default=None, ge=0)
    token_usage: dict[str, Any] = Field(default_factory=dict)


class SemanticContextPacket(BaseModel):
    """Minimum context exposed to an LLM planner; contains no business metrics."""

    model_config = ConfigDict(extra="forbid")

    query: str
    session_id: str
    turn_index: int = Field(ge=1)
    current_selected_product_ids: list[str] = Field(default_factory=list, max_length=10)
    last_explicit_product_ids: list[str] = Field(default_factory=list, max_length=10)
    last_resolved_product_ids: list[str] = Field(default_factory=list, max_length=10)
    last_comparison_order: list[str] = Field(default_factory=list, max_length=10)
    last_intent: str | None = None
    last_response_type: str | None = None
    last_requested_dimensions: list[str] = Field(default_factory=list, max_length=10)
    last_metric: str | None = None
    last_preference_order: list[str] = Field(default_factory=list, max_length=10)
    last_negative_scope: list[str] = Field(default_factory=list, max_length=10)
    reference_candidates: list[str] = Field(default_factory=list, max_length=10)
    verified_product_names: dict[str, str] = Field(default_factory=dict)
    allowed_tools: list[str] = Field(default_factory=list, max_length=20)
    max_tool_calls: int = Field(default=0, ge=0, le=20)
    task_context: SemanticTaskContextPacket = Field(default_factory=SemanticTaskContextPacket)
    rule_understanding: QueryUnderstanding


class GroundedReasoningOutput(BaseModel):
    """Advisory explanation over validated facts; never a business truth source."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=1200)
    priority_labels: list[str] = Field(default_factory=list, max_length=10)
    dimensions_used: list[str] = Field(default_factory=list, max_length=10)
    formal_recommendation_label: str | None = None
    human_review_required: bool = False


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
    resolution_method: Literal[
        "query_entity", "explicit_selection", "session_reference", "explicit_query", "ui_selection",
        "last_explicit_entity", "last_comparison", "last_resolved_entity", "ordinal_reference", "session_state",
    ]


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
    requested_provider: str = "deterministic_planner"
    fallback_provider: str | None = None
    model_route: str = "DETERMINISTIC_FAST_PATH"
    provider_calls: list[ProviderCallAudit] = Field(default_factory=list)
    fallback_reason: str | None = None
    clarification_code: str | None = None
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
    context_source: ContextSource = "none"
    decision_status: str = "NOT_APPLICABLE"
    decision_summary: dict[str, Any] = Field(default_factory=dict)
    data_sufficiency: DataSufficiencyResult | None = None
    task_state: TaskState = Field(default_factory=TaskState)

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
