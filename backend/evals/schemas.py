from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator, field_validator
from app.schemas.agent import EntityResolutionResult, ResponseType, IntentName

EntityStatus = EntityResolutionResult.model_fields["status"].annotation
Category = Literal["simple_fact", "entity", "filtering", "comparison", "policy", "data_sufficiency", "state", "fallback", "provenance", "semantic_understanding", "conversation_context", "semantic_orchestration", "natural_language_generalization", "contextual_followup", "model_routing"]
Intent = IntentName
Dimension = Literal["count", "price", "risk", "profit", "roi", "identity", "demand", "competition", "compliance", "decision", "recommendation", "recommendation_policy", "sales_snapshot", "data_sufficiency", "filters", "data_completeness", "recommendation_score", "detail", "history", "calculation", "evidence", "decision_reason", "provenance", "freshness", "policy", "data_quality"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AddedProduct(StrictModel):
    external_product_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    current_price: float = Field(gt=0)


class ProvenanceOverride(StrictModel):
    title: str
    field: Literal["current_price", "sales_growth_rate"] = "current_price"
    age_days: int | None = Field(default=None, ge=0)
    source_type: str | None = None
    provider: str | None = None
    clear_provenance: bool = False


class Setup(StrictModel):
    dataset: Literal["mock_enterprise_v1", "empty"] = "mock_enterprise_v1"
    selected_titles: list[str] = Field(default_factory=list)
    additions: list[AddedProduct] = Field(default_factory=list)
    previous_query: str | None = None
    semantic_plan: dict[str, JsonValue] | None = None
    reasoning_plan: dict[str, JsonValue] | None = None
    mock_provider_route: Literal["disabled", "qwen_semantic", "qwen_reasoning", "qwen_timeout_deepseek", "provider_unconfigured"] = "disabled"
    mock_llm: Literal["disabled", "timeout_after_tool", "repeated_tool"] = "disabled"
    mock_product_titles: list[str] = Field(default_factory=list)
    provenance_overrides: list[ProvenanceOverride] = Field(default_factory=list)
    foreign_additions: list[AddedProduct] = Field(default_factory=list)

    @model_validator(mode="after")
    def mock_targets_required(self):
        if self.mock_llm != "disabled" and len(self.mock_product_titles) != 2:
            raise ValueError("Mock tool scenario requires two fixture product titles")
        if self.mock_provider_route in {"qwen_semantic", "qwen_reasoning", "qwen_timeout_deepseek"} and self.semantic_plan is None:
            raise ValueError("Mock provider route requires a SemanticFrame fixture")
        if self.mock_provider_route == "qwen_reasoning" and self.reasoning_plan is None:
            raise ValueError("Mock reasoning route requires a reasoning frame fixture")
        return self


class FilterTruth(StrictModel):
    min_margin_rate: float | None = Field(default=None, ge=0, le=1)
    risk_level: Literal["low", "medium", "high"] | None = None
    compliance_status: Literal["approved", "pending", "rejected"] | None = None
    min_matches: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def nonempty(self):
        if all(value is None for value in (self.min_margin_rate, self.risk_level, self.compliance_status)):
            raise ValueError("Filter ground truth requires at least one explicit predicate")
        return self


class SufficiencyTruth(StrictModel):
    status: Literal["SUFFICIENT", "INSUFFICIENT_DATA"]
    observed_trend: Literal["GROWING", "DECLINING", "FLAT", "INSUFFICIENT_DATA"]
    snapshot_count: int = Field(ge=0)
    reported_metric: float | None = None
    reported_metric_source: str | None = None


class ProvenanceTruth(StrictModel):
    field: str | None = None
    source_type: str | None = None
    provider: str | None = None
    is_mock: bool | None = None
    freshness: Literal["FRESH", "STALE", "UNKNOWN"] | None = None
    evidence_status: Literal["TRACEABLE", "SOURCE_MISSING"] | None = None
    derived_inputs: list[str] = Field(default_factory=list)
    notice_codes: list[str] = Field(default_factory=list)
    forbidden_notice_codes: list[str] = Field(default_factory=list)
    require_collected_at: bool = False
    product_binding: bool = True
    tenant_isolation: bool = False


class Expected(StrictModel):
    intent: Intent | None = None
    entity_mentions: list[str] | None = None
    semantic_route: Literal["DETERMINISTIC_FAST_PATH", "SEMANTIC_PLANNER", "CLARIFICATION"] | None = None
    planner_calls: int | None = Field(default=None, ge=0, le=1)
    reference_field: str | None = None
    response_type: ResponseType | None = None
    entity_statuses: list[EntityStatus] | None = None
    resolved_titles: list[str] | None = None
    product_titles: list[str] | None = None
    candidate_titles: list[str] | None = None
    requested_dimensions: list[Dimension] | None = None
    tool_names: list[str] | None = None
    tool_call_count_max: int | None = Field(default=None, ge=0)
    decision_status: str | None = None
    human_review_required: bool | None = None
    required_facts: dict[str, JsonValue] = Field(default_factory=dict)
    forbidden_content: list[str] = Field(default_factory=list)
    required_content: list[str] = Field(default_factory=list)
    forbidden_display_scope: list[str] = Field(default_factory=list)
    empty_fields: list[Literal["warnings", "risks", "missing_data", "next_actions", "products", "entities"]] = Field(default_factory=list)
    filter_truth: FilterTruth | None = None
    data_sufficiency: SufficiencyTruth | None = None
    selection_source: Literal["none", "query_entity", "explicit_selection", "session_reference"] | None = None
    context_source: Literal["none", "explicit_query", "ui_selection", "last_explicit_entity", "last_comparison", "last_resolved_entity", "ordinal_reference", "session_state"] | None = None
    fallback_used: bool | None = None
    task_completed: bool | None = None
    reuse_tool: str | None = None
    duplicate_tool_execution: int | None = Field(default=None, ge=0)
    provenance: ProvenanceTruth | None = None
    active_provider: str | None = None
    requested_provider: str | None = None
    fallback_provider: str | None = None
    model_route: str | None = None
    provider_call_count_max: int | None = Field(default=None, ge=0)
    requires_reasoning: bool | None = None

    @field_validator("required_facts")
    @classmethod
    def safe_fact_paths(cls, value):
        roots = {"fact", "matched_count", "total_count", "displayed_count", "products", "decision_summary", "fallback_reason", "response_mode"}
        for path in value:
            if not re.fullmatch(r"[a-z_]+(?:\.[a-z_0-9]+)*", path) or path.split(".")[0] not in roots:
                raise ValueError("Unsupported fact assertion path")
        return value

    @model_validator(mode="after")
    def has_assertions(self):
        if not any(value is not None and value != [] and value != {} for value in self.model_dump().values()):
            raise ValueError("A case must contain required assertions")
        return self


class GoldenCase(StrictModel):
    case_id: str = Field(pattern=r"^[A-Z][A-Z0-9_-]+$")
    category: Category
    query: str = Field(min_length=1)
    provenance: str = Field(min_length=10)
    setup: Setup = Field(default_factory=Setup)
    expected: Expected


class AssertionResult(StrictModel):
    evaluator: str
    metric: str
    assertion: str
    passed: bool
    expected: JsonValue
    actual: JsonValue
    reason: str


def load_cases(path: Path) -> list[GoldenCase]:
    cases, seen = [], set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            case = GoldenCase.model_validate(json.loads(line))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Invalid Golden JSONL at line {number}: {type(exc).__name__}") from exc
        if case.case_id in seen:
            raise ValueError(f"Duplicate case_id: {case.case_id}")
        seen.add(case.case_id)
        cases.append(case)
    if not cases:
        raise ValueError("Golden dataset is empty")
    return cases
