"""Immutable, session-scoped what-if state over canonical product snapshots."""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from hashlib import sha256
from math import isfinite
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field

from app.agent.collection_ranking import normalize_ranking_dimensions, rank_collection
from app.db.models import Product
from app.schemas.agent import CollectionState
from app.services.decision_engine import (
    ScenarioCalculationInput,
    analyze_snapshot,
    calculation_input_from_product,
    recommendation_gate_status,
)


ScenarioOperation = Literal[
    "CREATE_SCENARIO", "UPDATE_SCENARIO", "REMOVE_OVERRIDE",
    "RESET_SCENARIO", "COMPARE_SCENARIO", "EXPLAIN_SCENARIO",
]
OverrideOperation = Literal[
    "SET", "INCREASE_BY", "DECREASE_BY",
    "INCREASE_PERCENT", "DECREASE_PERCENT", "MULTIPLY",
]
OverrideUnit = Literal["RUB", "PERCENT", "RATIO", "MULTIPLIER"]
OverrideField = Literal[
    "current_price", "procurement_cost", "advertising_cost", "shipping_cost",
    "fulfillment_cost", "platform_fee", "warehousing_cost", "tax_cost",
    "return_loss_reserve", "other_cost", "platform_commission_rate",
]
ScenarioCollectionScope = Literal["ALL_MEMBERS", "TOP_K", "SELECTED_MEMBERS"]

MONEY_FIELDS = frozenset({
    "current_price", "procurement_cost", "advertising_cost", "shipping_cost",
    "fulfillment_cost", "platform_fee", "warehousing_cost", "tax_cost",
    "return_loss_reserve", "other_cost",
})
RATE_FIELDS = frozenset({"platform_commission_rate"})
OVERRIDE_FIELDS = MONEY_FIELDS | RATE_FIELDS
DIRECT_OPERATIONS = frozenset({"SET", "INCREASE_BY", "DECREASE_BY"})
PERCENT_OPERATIONS = frozenset({"INCREASE_PERCENT", "DECREASE_PERCENT"})


class ScenarioStateError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ScenarioCommand(BaseModel):
    """Finite, fact-free command extracted from hypothetical language."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: ScenarioOperation
    field: OverrideField | None = None
    mutation_type: OverrideOperation | None = None
    value: float | None = None
    unit: OverrideUnit | None = None
    unsupported_field: bool = False
    collection_scope: ScenarioCollectionScope | None = None
    top_k: int | None = Field(default=None, ge=1, le=20)
    ranking_dimensions: tuple[str, ...] = ()


_FIELD_PATTERNS: tuple[tuple[str, str], ...] = (
    ("platform_commission_rate", r"平台佣金率|佣金率"),
    ("return_loss_reserve", r"退货损失准备|退货准备|退货损失"),
    ("procurement_cost", r"采购成本|采购价"),
    ("advertising_cost", r"广告成本|广告费"),
    ("shipping_cost", r"物流成本|运输成本|运费"),
    ("fulfillment_cost", r"履约成本|履约费"),
    ("platform_fee", r"平台费用|平台费"),
    ("warehousing_cost", r"仓储成本|仓储费"),
    ("tax_cost", r"税费|税收成本"),
    ("other_cost", r"其他成本|其它成本"),
    ("current_price", r"当前售价|销售价格|售价|定价|价格"),
)


def _scenario_field(query: str) -> str | None:
    return next((field for field, pattern in _FIELD_PATTERNS if re.search(pattern, query, re.I)), None)


def _ranking_dimensions(query: str) -> tuple[str, ...]:
    return tuple(
        dimension for dimension, pattern in (
            ("profit", r"利润|净利率|赚钱"), ("roi", r"\broi\b"),
            ("risk", r"风险"), ("recommendation", r"推荐(?:度|分)?"),
            ("demand", r"需求|销量|趋势"), ("competition", r"竞争"),
            ("compliance", r"合规"),
        ) if re.search(pattern, query, re.I)
    )


def _collection_scope(query: str) -> tuple[ScenarioCollectionScope | None, int | None]:
    top_match = re.search(r"前\s*(\d+)\s*(?:名|个|件|款)?", query, re.I)
    chinese_top = next((value for text, value in (
        ("前三", 3), ("前两", 2), ("前二", 2), ("前五", 5), ("前四", 4), ("第一", 1),
    ) if text in query), None)
    if top_match or chinese_top:
        return "TOP_K", max(1, min(20, int(top_match.group(1)))) if top_match else chinese_top
    if re.search(r"已选|选中的|选择的|这几个", query):
        return "SELECTED_MEMBERS", None
    if re.search(r"候选池|候选集合|这批(?:商品|候选)|当前(?:集合|候选)|全部(?:商品|候选|成员)", query):
        return "ALL_MEMBERS", None
    return None, None


def scenario_entity_text(query: str) -> str:
    """Return only the possible explicit-product prefix before a scenario mutation."""

    normalized = " ".join(str(query).strip().split())
    scope, _ = _collection_scope(normalized)
    if scope:
        return ""
    field_match = next((re.search(pattern, normalized, re.I) for _, pattern in _FIELD_PATTERNS if re.search(pattern, normalized, re.I)), None)
    if field_match:
        prefix = normalized[:field_match.start()]
    else:
        prefix = re.split(r"评分|销量|评论|搜索量|趋势|竞争|合规", normalized, maxsplit=1)[0]
    return re.sub(r"^(?:如果|假设|假如)\s*", "", prefix).strip(" ，,：:")


def parse_scenario_command(query: str, *, has_active_scenario: bool) -> ScenarioCommand | None:
    """Recognize general scenario operations without binding catalogue identity or facts."""

    normalized = " ".join(str(query).strip().split())
    field = _scenario_field(normalized)
    collection_scope, collection_top_k = _collection_scope(normalized)
    ranking_dimensions = _ranking_dimensions(normalized)
    if has_active_scenario and re.search(r"全部.*(?:恢复|还原).*原值|恢复全部.*原值|清空.*(?:假设|调整|情景)|重置.*(?:假设|情景)", normalized):
        return ScenarioCommand(operation="RESET_SCENARIO")
    if has_active_scenario and re.search(r"恢复|还原", normalized) and re.search(r"原来.*(?:排序|排名)|(?:排序|排名).*原来", normalized):
        return ScenarioCommand(operation="RESET_SCENARIO")
    if has_active_scenario and field and re.search(r"恢复|还原|取消.*(?:调整|假设)", normalized):
        return ScenarioCommand(operation="REMOVE_OVERRIDE", field=field)
    if has_active_scenario and re.search(r"(?:和|与).*(?:原来|原值|基准).*(?:比|比较)|(?:原来|原值|基准).*(?:比|比较|变化|超过)", normalized):
        return ScenarioCommand(operation="COMPARE_SCENARIO")
    if has_active_scenario and re.search(r"(?:当前|这个|刚才).*(?:假设|情景)", normalized) and re.search(r"重排|重新?排|排序", normalized):
        return ScenarioCommand(operation="UPDATE_SCENARIO", ranking_dimensions=ranking_dimensions)
    if has_active_scenario and re.search(r"第[一二两三四五六七八九十\d]+名|第一名|排名", normalized) and re.search(r"谁|哪个|解释|为什么|为何", normalized):
        return ScenarioCommand(operation="EXPLAIN_SCENARIO", ranking_dimensions=ranking_dimensions)
    if has_active_scenario and re.search(r"(?:现在|当前|这个假设|刚才那个假设).*(?:利润|净利率|roi|风险|推荐|结果|怎么算)|(?:解释|说明).*(?:假设|情景|结果)", normalized, re.I):
        return ScenarioCommand(operation="EXPLAIN_SCENARIO")

    # Ranking/task mutations are not hypothetical calculations. Unanchored
    # text without an allowed override field stays available to TaskState.
    scenario_anchor = bool(re.search(r"如果|假设|假如|情景|模拟|会怎样|会怎么样|如何变化|有什么影响", normalized))
    if not field and not collection_scope and not scenario_anchor:
        return None

    mutation_signal = bool(re.search(
        r"(?:如果|假设|假如|再|继续|另外|同时|并且|然后).*(?:降低|下降|减少|降|提高|上升|增加|涨|改成|改为|设为|调整为|减半|翻倍)|"
        r"(?:降低|下降|减少|降|提高|上升|增加|涨|改成|改为|设为|调整为|减半|翻倍).*(?:会怎样|会怎么样|利润|净利率|roi|风险|推荐)",
        normalized,
        re.I,
    ))
    if not mutation_signal:
        return None
    follows_existing = bool(re.search(r"^(?:再|继续|另外|同时|并且|然后)", normalized))
    operation: ScenarioOperation = "UPDATE_SCENARIO" if has_active_scenario or follows_existing else "CREATE_SCENARIO"
    if not field:
        return ScenarioCommand(
            operation=operation, unsupported_field=True,
            collection_scope=collection_scope, top_k=collection_top_k,
            ranking_dimensions=ranking_dimensions,
        )

    percent = re.search(r"([+-]?\d+(?:\.\d+)?)\s*[%％]", normalized)
    rub = re.search(r"([+-]?\d+(?:\.\d+)?)\s*(?:rub|卢布|元)?", normalized, re.I)
    if re.search(r"减半|一半", normalized):
        return ScenarioCommand(operation=operation, field=field, mutation_type="MULTIPLY", value=0.5, unit="MULTIPLIER", collection_scope=collection_scope, top_k=collection_top_k, ranking_dimensions=ranking_dimensions)
    if re.search(r"翻倍|两倍", normalized):
        return ScenarioCommand(operation=operation, field=field, mutation_type="MULTIPLY", value=2, unit="MULTIPLIER", collection_scope=collection_scope, top_k=collection_top_k, ranking_dimensions=ranking_dimensions)
    if percent and re.search(r"降低|下降|减少|降", normalized):
        return ScenarioCommand(operation=operation, field=field, mutation_type="DECREASE_PERCENT", value=float(percent.group(1)), unit="PERCENT", collection_scope=collection_scope, top_k=collection_top_k, ranking_dimensions=ranking_dimensions)
    if percent and re.search(r"提高|上升|增加|涨", normalized):
        return ScenarioCommand(operation=operation, field=field, mutation_type="INCREASE_PERCENT", value=float(percent.group(1)), unit="PERCENT", collection_scope=collection_scope, top_k=collection_top_k, ranking_dimensions=ranking_dimensions)
    if percent and field == "platform_commission_rate" and re.search(r"改成|改为|设为|调整为", normalized):
        return ScenarioCommand(operation=operation, field=field, mutation_type="SET", value=float(percent.group(1)), unit="PERCENT", collection_scope=collection_scope, top_k=collection_top_k, ranking_dimensions=ranking_dimensions)
    if rub and re.search(r"改成|改为|设为|调整为", normalized):
        return ScenarioCommand(operation=operation, field=field, mutation_type="SET", value=float(rub.group(1)), unit="RUB", collection_scope=collection_scope, top_k=collection_top_k, ranking_dimensions=ranking_dimensions)
    if rub and re.search(r"降低|下降|减少|降", normalized):
        return ScenarioCommand(operation=operation, field=field, mutation_type="DECREASE_BY", value=float(rub.group(1)), unit="RUB", collection_scope=collection_scope, top_k=collection_top_k, ranking_dimensions=ranking_dimensions)
    if rub and re.search(r"提高|上升|增加|涨", normalized):
        return ScenarioCommand(operation=operation, field=field, mutation_type="INCREASE_BY", value=float(rub.group(1)), unit="RUB", collection_scope=collection_scope, top_k=collection_top_k, ranking_dimensions=ranking_dimensions)
    return ScenarioCommand(operation=operation, field=field, unsupported_field=True, collection_scope=collection_scope, top_k=collection_top_k, ranking_dimensions=ranking_dimensions)


class ScenarioOverride(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    field: OverrideField
    operation: OverrideOperation
    value: float
    unit: OverrideUnit
    normalized_value: float
    target_product_ids: tuple[str, ...] = ()
    created_revision: int = Field(ge=1)


class ScenarioProductSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    product_id: str
    source_revision: str
    captured_at: datetime
    input_values: ScenarioCalculationInput
    snapshot_hash: str


class CanonicalSnapshotRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    product_id: str
    source_revision: str
    captured_at: datetime
    snapshot_hash: str


class ScenarioBaseline(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    product_id: str
    net_profit: float
    net_margin: float
    roi: float
    recommendation_score: float
    risk_level: str
    decision_status: str
    demand_score: float = 0
    competition_score: float = 0
    compliance_score: float = 0


class ScenarioCollectionSnapshot(BaseModel):
    """Immutable membership and ordering captured from the canonical collection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    collection_type: Literal["candidate_pool", "active_result_set", "company_catalog"]
    source_collection_revision: int = Field(ge=0)
    member_ids: tuple[str, ...]
    canonical_ordered_ids: tuple[str, ...]
    canonical_ranking_spec: tuple[str, ...]
    canonical_top_k: int = Field(ge=1, le=20)
    require_gate_pass: bool = True
    exclude_insufficient_data: bool = False
    captured_at: datetime
    snapshot_hash: str


class ScenarioCollectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    product_id: str
    canonical_rank: int | None = Field(default=None, ge=1)
    scenario_rank: int | None = Field(default=None, ge=1)
    rank_delta: int | None = None
    eligible: bool
    decision_status: str
    ranking_revision: int = Field(ge=1)


class ScenarioMetricDelta(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    net_profit: float
    net_margin: float
    roi: float
    recommendation_score: float


class ScenarioEffectiveValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    field: OverrideField
    value: float


class ScenarioDerivedResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    product_id: str
    net_profit: float
    net_margin: float
    roi: float
    recommendation_score: float
    recommendation_grade: str
    risk_level: str
    decision_status: str
    demand_score: float = 0
    competition_score: float = 0
    compliance_score: float = 0
    recommendation_label: Literal["HYPOTHETICAL"] = "HYPOTHETICAL"
    scenario_revision: int = Field(ge=1)
    effective_values: tuple[ScenarioEffectiveValue, ...] = ()
    delta: ScenarioMetricDelta


class ScenarioState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "scenario-state-v1"
    scenario_id: str
    session_id: str
    company_id: str
    base_product_ids: tuple[str, ...]
    base_collection_revision: int | None = Field(default=None, ge=0)
    source_task_revision: int = Field(default=0, ge=0)
    canonical_snapshot_refs: tuple[CanonicalSnapshotRef, ...]
    canonical_snapshots: tuple[ScenarioProductSnapshot, ...]
    overrides: tuple[ScenarioOverride, ...] = ()
    derived_results: tuple[ScenarioDerivedResult, ...] = ()
    comparison_baseline: tuple[ScenarioBaseline, ...] = ()
    scenario_ordered_ids: tuple[str, ...] = ()
    collection_snapshot: ScenarioCollectionSnapshot | None = None
    scenario_collection_scope: ScenarioCollectionScope | None = None
    scenario_scope_target_ids: tuple[str, ...] = ()
    scenario_ranking_spec: tuple[str, ...] = ()
    scenario_top_k_ids: tuple[str, ...] = ()
    scenario_collection_results: tuple[ScenarioCollectionResult, ...] = ()
    scenario_revision: int = Field(default=1, ge=1)
    status: Literal["HYPOTHETICAL", "SIMULATION"] = "HYPOTHETICAL"
    baseline_status: Literal["CURRENT", "STALE_BASELINE"] = "CURRENT"
    collection_baseline_status: Literal["CURRENT", "STALE_COLLECTION_BASELINE"] = "CURRENT"
    created_at: datetime
    updated_at: datetime


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _snapshot_hash(product_id: str, values: ScenarioCalculationInput) -> str:
    payload = {"product_id": product_id, "input_values": values.model_dump(mode="json")}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _collection_snapshot_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def snapshot_product(product: Product) -> ScenarioProductSnapshot:
    """Detach the canonical calculation input from the tracked ORM instance."""

    values = calculation_input_from_product(product)
    changed_at = getattr(product, "source_updated_at", None) or getattr(product, "updated_at", None)
    captured_at = changed_at or _now()
    source_revision = changed_at.isoformat() if changed_at else "unversioned"
    return ScenarioProductSnapshot(
        product_id=str(product.id),
        source_revision=source_revision,
        captured_at=captured_at,
        input_values=values,
        snapshot_hash=_snapshot_hash(str(product.id), values),
    )


def normalize_override(
    *,
    field: str,
    operation: str,
    value: float,
    unit: str,
    created_revision: int,
    target_product_ids: Sequence[str] = (),
) -> ScenarioOverride:
    """Validate and normalize the finite override vocabulary in one place."""

    field = str(field).strip().casefold()
    operation = str(operation).strip().upper()
    unit = str(unit).strip().upper()
    if field not in OVERRIDE_FIELDS:
        raise ScenarioStateError("UNSUPPORTED_OVERRIDE_FIELD", f"Unsupported scenario field: {field}")
    if operation not in {"SET", "INCREASE_BY", "DECREASE_BY", "INCREASE_PERCENT", "DECREASE_PERCENT", "MULTIPLY"}:
        raise ScenarioStateError("UNSUPPORTED_OVERRIDE_OPERATION", f"Unsupported scenario operation: {operation}")
    if unit not in {"RUB", "PERCENT", "RATIO", "MULTIPLIER"}:
        raise ScenarioStateError("INVALID_OVERRIDE_UNIT", f"Unsupported scenario unit: {unit}")
    if isinstance(value, bool):
        raise ScenarioStateError("INVALID_OVERRIDE_VALUE", "Boolean is not a scenario number.")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ScenarioStateError("INVALID_OVERRIDE_VALUE", "Scenario value must be numeric.") from exc
    if not isfinite(numeric):
        raise ScenarioStateError("INVALID_OVERRIDE_VALUE", "Scenario value must be finite.")

    if field in RATE_FIELDS:
        if operation != "SET" or unit not in {"PERCENT", "RATIO"}:
            raise ScenarioStateError("INVALID_OVERRIDE_CONTRACT", "Commission rate only supports SET with PERCENT or RATIO.")
        normalized = numeric / 100 if unit == "PERCENT" else numeric
        if not 0 <= normalized <= 1:
            raise ScenarioStateError("INVALID_OVERRIDE_RANGE", "Commission rate must be between 0 and 1.")
    elif operation in DIRECT_OPERATIONS:
        if unit != "RUB":
            raise ScenarioStateError("INVALID_OVERRIDE_CONTRACT", "Money SET/INCREASE_BY/DECREASE_BY requires RUB.")
        if numeric < 0:
            raise ScenarioStateError("INVALID_OVERRIDE_RANGE", "Money override cannot be negative.")
        normalized = numeric
    elif operation in PERCENT_OPERATIONS:
        if unit not in {"PERCENT", "RATIO"}:
            raise ScenarioStateError("INVALID_OVERRIDE_CONTRACT", "Percentage operation requires PERCENT or normalized RATIO.")
        normalized = numeric / 100 if unit == "PERCENT" else numeric
        if not 0 <= normalized <= 1:
            raise ScenarioStateError("INVALID_OVERRIDE_RANGE", "Percentage change must be between 0% and 100%.")
    else:
        if unit != "MULTIPLIER":
            raise ScenarioStateError("INVALID_OVERRIDE_CONTRACT", "MULTIPLY requires MULTIPLIER.")
        if not 0 < numeric <= 10:
            raise ScenarioStateError("INVALID_OVERRIDE_RANGE", "Multiplier must be greater than 0 and no more than 10.")
        normalized = numeric

    return ScenarioOverride(
        field=field,
        operation=operation,
        value=numeric,
        unit=unit,
        normalized_value=normalized,
        target_product_ids=tuple(dict.fromkeys(str(item) for item in target_product_ids if item)),
        created_revision=created_revision,
    )


def apply_overrides(
    snapshot: ScenarioProductSnapshot,
    overrides: Sequence[ScenarioOverride],
) -> ScenarioCalculationInput:
    """Always replay ordered overrides from the immutable canonical snapshot."""

    values = snapshot.input_values
    ordered = sorted(enumerate(overrides), key=lambda item: (item[1].created_revision, item[0]))
    for _, override in ordered:
        if override.target_product_ids and snapshot.product_id not in override.target_product_ids:
            continue
        current = float(getattr(values, override.field))
        amount = override.normalized_value
        if override.operation == "SET":
            updated = amount
        elif override.operation == "INCREASE_BY":
            updated = current + amount
        elif override.operation == "DECREASE_BY":
            updated = current - amount
        elif override.operation == "INCREASE_PERCENT":
            updated = current * (1 + amount)
        elif override.operation == "DECREASE_PERCENT":
            updated = current * (1 - amount)
        else:
            updated = current * amount
        if not isfinite(updated) or updated < 0:
            raise ScenarioStateError("INVALID_DERIVED_VALUE", f"Override makes {override.field} invalid.")
        if override.field in RATE_FIELDS and updated > 1:
            raise ScenarioStateError("INVALID_DERIVED_VALUE", "Commission rate cannot exceed 1.")
        values = values.model_copy(update={override.field: updated})
    return values


def _decision_status(values: ScenarioCalculationInput, analysis: Any) -> str:
    return recommendation_gate_status(
        compliance_status=values.compliance_status,
        decision_ready=bool(analysis.evidence_completeness.get("decision_ready", False)),
        net_margin=analysis.net_margin,
        target_margin=values.target_margin_rate,
        risk_level=analysis.risk_level,
        recommendation=analysis.recommendation,
    )


def _baseline(snapshot: ScenarioProductSnapshot) -> ScenarioBaseline:
    result = analyze_snapshot(snapshot.input_values)
    return ScenarioBaseline(
        product_id=snapshot.product_id,
        net_profit=result.net_profit,
        net_margin=result.net_margin,
        roi=result.roi,
        recommendation_score=result.recommendation_score,
        risk_level=result.risk_level,
        decision_status=_decision_status(snapshot.input_values, result),
        demand_score=result.demand_score,
        competition_score=result.competition_score,
        compliance_score=result.compliance_score,
    )


def recalculate_scenario(state: ScenarioState) -> ScenarioState:
    """Rebuild every derived result from its canonical snapshot and ordered overrides."""

    baseline_by_id = {item.product_id: item for item in state.comparison_baseline}
    results: list[ScenarioDerivedResult] = []
    for snapshot in state.canonical_snapshots:
        values = apply_overrides(snapshot, state.overrides)
        analysis = analyze_snapshot(values)
        baseline = baseline_by_id[snapshot.product_id]
        results.append(ScenarioDerivedResult(
            product_id=snapshot.product_id,
            net_profit=analysis.net_profit,
            net_margin=analysis.net_margin,
            roi=analysis.roi,
            recommendation_score=analysis.recommendation_score,
            recommendation_grade=analysis.recommendation_grade,
            risk_level=analysis.risk_level,
            decision_status=_decision_status(values, analysis),
            demand_score=analysis.demand_score,
            competition_score=analysis.competition_score,
            compliance_score=analysis.compliance_score,
            scenario_revision=state.scenario_revision,
            effective_values=tuple(
                ScenarioEffectiveValue(field=field, value=float(getattr(values, field)))
                for field in sorted(OVERRIDE_FIELDS)
            ),
            delta=ScenarioMetricDelta(
                net_profit=round(analysis.net_profit - baseline.net_profit, 4),
                net_margin=round(analysis.net_margin - baseline.net_margin, 6),
                roi=round(analysis.roi - baseline.roi, 6),
                recommendation_score=round(analysis.recommendation_score - baseline.recommendation_score, 4),
            ),
        ))
    ordered_ids = tuple(item.product_id for item in results)
    collection_results: tuple[ScenarioCollectionResult, ...] = ()
    top_ids: tuple[str, ...] = ()
    if state.collection_snapshot:
        dimensions = normalize_ranking_dimensions(
            state.scenario_ranking_spec or state.collection_snapshot.canonical_ranking_spec
        )
        ranked = rank_collection(results, dimensions, identity=lambda item: item.product_id)
        eligible = [item for item in ranked if (
            item.decision_status != "BLOCKED"
            and (not state.collection_snapshot.require_gate_pass or item.decision_status == "RECOMMENDED")
            and (not state.collection_snapshot.exclude_insufficient_data or item.decision_status != "INSUFFICIENT_DATA")
        )]
        ordered_ids = tuple(item.product_id for item in eligible)
        top_ids = ordered_ids[:state.collection_snapshot.canonical_top_k]
        canonical_rank = {
            product_id: index for index, product_id in enumerate(state.collection_snapshot.canonical_ordered_ids, 1)
        }
        scenario_rank = {product_id: index for index, product_id in enumerate(ordered_ids, 1)}
        by_id = {item.product_id: item for item in results}
        collection_results = tuple(ScenarioCollectionResult(
            product_id=product_id,
            canonical_rank=canonical_rank.get(product_id),
            scenario_rank=scenario_rank.get(product_id),
            rank_delta=(canonical_rank[product_id] - scenario_rank[product_id])
            if product_id in canonical_rank and product_id in scenario_rank else None,
            eligible=product_id in scenario_rank,
            decision_status=by_id[product_id].decision_status,
            ranking_revision=state.scenario_revision,
        ) for product_id in state.collection_snapshot.member_ids if product_id in by_id)
    return state.model_copy(update={
        "derived_results": tuple(results),
        "scenario_ordered_ids": ordered_ids,
        "scenario_ranking_spec": normalize_ranking_dimensions(
            state.scenario_ranking_spec or (
                state.collection_snapshot.canonical_ranking_spec if state.collection_snapshot else ()
            )
        ) if state.collection_snapshot else (),
        "scenario_top_k_ids": top_ids,
        "scenario_collection_results": collection_results,
        "status": "SIMULATION",
        "updated_at": _now(),
    })


def create_scenario(
    *,
    scenario_id: str,
    session_id: str,
    company_id: str,
    products: Sequence[Product],
    override_specs: Sequence[dict[str, Any]] = (),
    source_task_revision: int = 0,
    base_collection_revision: int | None = None,
    collection_snapshot: ScenarioCollectionSnapshot | None = None,
    scenario_collection_scope: ScenarioCollectionScope | None = None,
    scenario_scope_target_ids: Sequence[str] = (),
    scenario_ranking_spec: Sequence[str] = (),
) -> ScenarioState:
    if not scenario_id or not session_id or not company_id:
        raise ScenarioStateError("INVALID_SCENARIO_OWNER", "Scenario, session and company identities are required.")
    snapshots = tuple(snapshot_product(product) for product in products)
    if not snapshots or any(product.company_id != company_id for product in products):
        raise ScenarioStateError("SCENARIO_SCOPE_VIOLATION", "Scenario products must belong to the current company.")
    ids = tuple(dict.fromkeys(item.product_id for item in snapshots))
    if len(ids) != len(snapshots):
        raise ScenarioStateError("DUPLICATE_SCENARIO_PRODUCT", "Scenario products must be unique.")
    overrides = tuple(
        normalize_override(created_revision=1, **spec)
        for spec in override_specs
    )
    if any(set(item.target_product_ids) - set(ids) for item in overrides):
        raise ScenarioStateError("SCENARIO_SCOPE_VIOLATION", "Override target is outside the scenario.")
    now = _now()
    baselines = tuple(_baseline(snapshot) for snapshot in snapshots)
    state = ScenarioState(
        scenario_id=scenario_id,
        session_id=session_id,
        company_id=company_id,
        base_product_ids=ids,
        base_collection_revision=base_collection_revision,
        source_task_revision=source_task_revision,
        canonical_snapshot_refs=tuple(CanonicalSnapshotRef(
            product_id=item.product_id,
            source_revision=item.source_revision,
            captured_at=item.captured_at,
            snapshot_hash=item.snapshot_hash,
        ) for item in snapshots),
        canonical_snapshots=snapshots,
        overrides=overrides,
        comparison_baseline=baselines,
        collection_snapshot=collection_snapshot,
        scenario_collection_scope=scenario_collection_scope,
        scenario_scope_target_ids=tuple(scenario_scope_target_ids),
        scenario_ranking_spec=tuple(scenario_ranking_spec),
        created_at=now,
        updated_at=now,
    )
    return recalculate_scenario(state)


def create_collection_scenario(
    *,
    scenario_id: str,
    session_id: str,
    company_id: str,
    products: Sequence[Product],
    collection: CollectionState,
    override_specs: Sequence[dict[str, Any]],
    scope: ScenarioCollectionScope,
    top_k: int | None = None,
    selected_product_ids: Sequence[str] = (),
    ranking_dimensions: Sequence[str] = (),
) -> ScenarioState:
    """Freeze a canonical collection, then create an isolated hypothetical overlay."""

    member_ids = tuple(dict.fromkeys(str(item) for item in collection.product_ids))
    product_by_id = {str(item.id): item for item in products}
    if not member_ids or set(member_ids) - set(product_by_id):
        raise ScenarioStateError("SCENARIO_SCOPE_VIOLATION", "Collection members are unavailable in the current tenant.")
    canonical_order = tuple(
        item for item in (collection.ranked_product_ids or collection.product_ids) if item in member_ids
    )
    canonical_dimensions = normalize_ranking_dimensions(collection.ranking_dimensions)
    frozen_top_k = max(1, min(20, int(top_k or collection.top_k)))
    if scope == "TOP_K":
        target_ids = canonical_order[:frozen_top_k]
    elif scope == "SELECTED_MEMBERS":
        target_ids = tuple(item for item in dict.fromkeys(selected_product_ids) if item in member_ids)
    else:
        target_ids = member_ids
    if not target_ids:
        raise ScenarioStateError("MISSING_CONTEXT", "The selected scenario collection scope is empty.")
    captured_at = _now()
    snapshot_payload = {
        "collection_type": collection.collection_type,
        "source_collection_revision": collection.source_task_revision,
        "member_ids": member_ids,
        "canonical_ordered_ids": canonical_order,
        "canonical_ranking_spec": canonical_dimensions,
        "canonical_top_k": collection.top_k,
        "require_gate_pass": collection.require_gate_pass,
        "exclude_insufficient_data": collection.exclude_insufficient_data,
    }
    frozen_collection = ScenarioCollectionSnapshot(
        **snapshot_payload,
        captured_at=captured_at,
        snapshot_hash=_collection_snapshot_hash(snapshot_payload),
    )
    targeted_specs = [{**spec, "target_product_ids": target_ids} for spec in override_specs]
    return create_scenario(
        scenario_id=scenario_id, session_id=session_id, company_id=company_id,
        products=[product_by_id[item] for item in member_ids],
        override_specs=targeted_specs,
        source_task_revision=collection.source_task_revision,
        base_collection_revision=collection.source_task_revision,
        collection_snapshot=frozen_collection,
        scenario_collection_scope=scope,
        scenario_scope_target_ids=target_ids,
        scenario_ranking_spec=ranking_dimensions or canonical_dimensions,
    )


def _require_revision(state: ScenarioState, expected_revision: int) -> None:
    if state.scenario_revision != expected_revision:
        raise ScenarioStateError("STALE_SCENARIO_REVISION", "Scenario revision is stale; state was not changed.")


def update_scenario(
    state: ScenarioState,
    *,
    expected_revision: int,
    field: str,
    operation: str,
    value: float,
    unit: str,
    target_product_ids: Sequence[str] = (),
) -> ScenarioState:
    _require_revision(state, expected_revision)
    revision = state.scenario_revision + 1
    override = normalize_override(
        field=field, operation=operation, value=value, unit=unit,
        target_product_ids=target_product_ids, created_revision=revision,
    )
    if set(override.target_product_ids) - set(state.base_product_ids):
        raise ScenarioStateError("SCENARIO_SCOPE_VIOLATION", "Override target is outside the scenario.")
    candidate = state.model_copy(update={
        "scenario_revision": revision,
        "overrides": (*state.overrides, override),
        "status": "HYPOTHETICAL",
    })
    return recalculate_scenario(candidate)


def update_scenario_ranking(
    state: ScenarioState,
    *,
    expected_revision: int,
    ranking_dimensions: Sequence[str],
) -> ScenarioState:
    _require_revision(state, expected_revision)
    if not state.collection_snapshot:
        raise ScenarioStateError("INVALID_SCENARIO_OPERATION", "Ranking changes require a collection scenario.")
    candidate = state.model_copy(update={
        "scenario_revision": state.scenario_revision + 1,
        "scenario_ranking_spec": normalize_ranking_dimensions(ranking_dimensions),
        "status": "HYPOTHETICAL",
    })
    return recalculate_scenario(candidate)


def remove_override(
    state: ScenarioState,
    *,
    field: str,
    expected_revision: int,
) -> ScenarioState:
    _require_revision(state, expected_revision)
    normalized_field = str(field).strip().casefold()
    if normalized_field not in OVERRIDE_FIELDS:
        raise ScenarioStateError("UNSUPPORTED_OVERRIDE_FIELD", f"Unsupported scenario field: {normalized_field}")
    candidate = state.model_copy(update={
        "scenario_revision": state.scenario_revision + 1,
        "overrides": tuple(item for item in state.overrides if item.field != normalized_field),
        "status": "HYPOTHETICAL",
    })
    return recalculate_scenario(candidate)


def reset_scenario(state: ScenarioState, *, expected_revision: int) -> ScenarioState:
    _require_revision(state, expected_revision)
    candidate = state.model_copy(update={
        "scenario_revision": state.scenario_revision + 1,
        "overrides": (),
        "scenario_ranking_spec": state.collection_snapshot.canonical_ranking_spec if state.collection_snapshot else (),
        "status": "HYPOTHETICAL",
    })
    reset = recalculate_scenario(candidate)
    if not reset.collection_snapshot:
        return reset
    eligible = {item.product_id for item in reset.scenario_collection_results if item.eligible}
    restored_order = tuple(
        product_id for product_id in reset.collection_snapshot.canonical_ordered_ids
        if product_id in eligible
    )
    canonical_rank = {product_id: index for index, product_id in enumerate(restored_order, 1)}
    restored_results = tuple(item.model_copy(update={
        "canonical_rank": canonical_rank.get(item.product_id),
        "scenario_rank": canonical_rank.get(item.product_id),
        "rank_delta": 0 if item.product_id in canonical_rank else None,
        "ranking_revision": reset.scenario_revision,
    }) for item in reset.scenario_collection_results)
    return reset.model_copy(update={
        "scenario_ordered_ids": restored_order,
        "scenario_top_k_ids": restored_order[:reset.collection_snapshot.canonical_top_k],
        "scenario_collection_results": restored_results,
    })


def compare_to_baseline(state: ScenarioState) -> dict[str, dict[str, float]]:
    return {item.product_id: item.delta.model_dump() for item in state.derived_results}


def scenario_comparison_rows(state: ScenarioState) -> list[dict[str, Any]]:
    """Join frozen canonical metrics, hypothetical metrics, and ranks by product id."""

    baseline_by_id = {item.product_id: item for item in state.comparison_baseline}
    derived_by_id = {item.product_id: item for item in state.derived_results}
    ranking_by_id = {item.product_id: item for item in state.scenario_collection_results}
    rows: list[dict[str, Any]] = []
    for product_id in state.base_product_ids:
        canonical = baseline_by_id.get(product_id)
        hypothetical = derived_by_id.get(product_id)
        if canonical is None or hypothetical is None:
            continue
        ranking = ranking_by_id.get(product_id)
        rows.append({
            "product_id": product_id,
            "canonical": {
                "net_profit": canonical.net_profit,
                "net_margin": canonical.net_margin,
                "roi": canonical.roi,
                "recommendation_score": canonical.recommendation_score,
            },
            "hypothetical": {
                "net_profit": hypothetical.net_profit,
                "net_margin": hypothetical.net_margin,
                "roi": hypothetical.roi,
                "recommendation_score": hypothetical.recommendation_score,
            },
            "delta": hypothetical.delta.model_dump(mode="json"),
            "canonical_rank": ranking.canonical_rank if ranking else None,
            "scenario_rank": ranking.scenario_rank if ranking else None,
            "rank_delta": ranking.rank_delta if ranking else None,
        })
    return rows


def scenario_response_view(state: ScenarioState, operation: ScenarioOperation) -> dict[str, Any]:
    """Expose a validated hypothetical result without leaking raw canonical inputs."""

    return {
        "status": "HYPOTHETICAL",
        "baseline_status": state.baseline_status,
        "scenario_id": state.scenario_id,
        "scenario_revision": state.scenario_revision,
        "operation": operation,
        "overrides": [item.model_dump(mode="json") for item in state.overrides],
        "canonical": [item.model_dump(mode="json") for item in state.comparison_baseline],
        "hypothetical": [item.model_dump(mode="json") for item in state.derived_results],
        "delta": compare_to_baseline(state),
        "comparison": scenario_comparison_rows(state) if operation == "COMPARE_SCENARIO" else [],
        "collection_baseline_status": state.collection_baseline_status,
        "collection_scope": state.scenario_collection_scope,
        "scope_target_count": len(state.scenario_scope_target_ids),
        "scenario_ranking_spec": list(state.scenario_ranking_spec),
        "scenario_ordered_ids": list(state.scenario_ordered_ids),
        "scenario_top_k_ids": list(state.scenario_top_k_ids),
        "collection_ranking": [item.model_dump(mode="json") for item in state.scenario_collection_results],
        "canonical_collection_unchanged": bool(state.collection_snapshot),
    }


def detect_stale_baseline(state: ScenarioState, products: Sequence[Product]) -> ScenarioState:
    """Compare current canonical calculation inputs with the immutable scenario snapshot."""

    current = {str(product.id): snapshot_product(product) for product in products}
    stale = any(
        reference.product_id not in current
        or current[reference.product_id].snapshot_hash != reference.snapshot_hash
        or current[reference.product_id].source_revision != reference.source_revision
        for reference in state.canonical_snapshot_refs
    )
    status = "STALE_BASELINE" if stale else "CURRENT"
    return state if state.baseline_status == status else state.model_copy(update={"baseline_status": status})


def detect_stale_collection_baseline(
    state: ScenarioState,
    collection: CollectionState | None,
) -> ScenarioState:
    snapshot = state.collection_snapshot
    if not snapshot:
        return state
    stale = bool(
        collection is None
        or collection.source_task_revision != snapshot.source_collection_revision
        or tuple(collection.product_ids) != snapshot.member_ids
        or tuple(collection.ranked_product_ids or collection.product_ids) != snapshot.canonical_ordered_ids
        or normalize_ranking_dimensions(collection.ranking_dimensions) != snapshot.canonical_ranking_spec
    )
    status = "STALE_COLLECTION_BASELINE" if stale else "CURRENT"
    return state if state.collection_baseline_status == status else state.model_copy(update={"collection_baseline_status": status})


def load_scenario_state(
    raw_state: dict[str, Any] | None,
    *,
    session_id: str,
    company_id: str,
    valid_product_ids: set[str],
) -> ScenarioState | None:
    raw = (raw_state or {}).get("scenario_state")
    if not raw:
        return None
    try:
        state = ScenarioState.model_validate(raw)
    except Exception as exc:
        raise ScenarioStateError("INVALID_SCENARIO_STATE", "Stored scenario state is invalid.") from exc
    if state.session_id != session_id or state.company_id != company_id:
        raise ScenarioStateError("SCENARIO_SCOPE_VIOLATION", "Scenario does not belong to this session and company.")
    if set(state.base_product_ids) - valid_product_ids:
        raise ScenarioStateError("SCENARIO_SCOPE_VIOLATION", "Scenario references an unavailable product.")
    return state


def evolve_scenario_state(
    state: ScenarioState,
    *,
    operation: ScenarioOperation,
    expected_revision: int,
    **arguments: Any,
) -> ScenarioState:
    if operation == "UPDATE_SCENARIO":
        return update_scenario(state, expected_revision=expected_revision, **arguments)
    if operation == "REMOVE_OVERRIDE":
        return remove_override(state, expected_revision=expected_revision, **arguments)
    if operation == "RESET_SCENARIO":
        return reset_scenario(state, expected_revision=expected_revision)
    if operation in {"COMPARE_SCENARIO", "EXPLAIN_SCENARIO"}:
        _require_revision(state, expected_revision)
        return state
    raise ScenarioStateError("INVALID_SCENARIO_OPERATION", f"Unsupported evolution operation: {operation}")
