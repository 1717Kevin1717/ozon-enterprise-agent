"""Provider-independent vocabulary for semantic planning.

The model may interpret language, but every value entering backend planning is
normalized to this finite contract.  No catalogue facts or query phrases live
here.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Iterable


CANONICAL_INTENTS = (
    "company_product_count", "product_price", "product_filter", "product_detail",
    "profit_comparison", "product_comparison", "compliance_policy", "recommendation_policy",
    "selection_recommendation", "provenance_fact", "calculation_explanation",
    "decision_explanation", "data_quality_answer", "data_quality_policy", "unknown",
)

SEMANTIC_PLANNER_INTENTS = frozenset({
    "product_price", "product_filter", "product_detail", "product_comparison", "profit_comparison",
    "selection_recommendation", "provenance_fact", "calculation_explanation",
    "decision_explanation", "data_quality_answer", "data_quality_policy", "unknown",
})

CANONICAL_DIMENSIONS = (
    "price", "profit", "roi", "risk", "detail", "identity", "filters", "demand",
    "competition", "compliance", "recommendation", "decision", "provenance",
    "sales_snapshot", "data_sufficiency", "calculation", "evidence",
    "decision_reason", "freshness", "data_quality", "policy",
)

# Metrics are specific measurements or analysis modes inside a dimension.  They
# are not accepted as top-level business dimensions.
CANONICAL_METRICS = (
    "current_price", "net_profit", "net_margin", "roi", "recommendation_score",
    "risk_level", "sales_growth_rate", "cost_breakdown", "freshness",
)

INTENT_ALLOWED_DIMENSIONS = {
    "product_filter": frozenset({
        "filters", "profit", "roi", "risk", "competition", "compliance",
        "recommendation", "data_quality",
    }),
    "provenance_fact": frozenset({
        "price", "profit", "provenance", "sales_snapshot", "data_sufficiency", "freshness",
    }),
    "calculation_explanation": frozenset({"profit", "roi", "calculation", "evidence"}),
    "decision_explanation": frozenset({
        "decision_reason", "evidence", "freshness", "recommendation",
        "profit", "risk", "compliance", "decision",
    }),
    "data_quality_answer": frozenset({"price", "freshness"}),
    "data_quality_policy": frozenset({"data_quality", "freshness", "policy"}),
    "product_price": frozenset({"price"}),
    "product_detail": frozenset({"price", "profit", "roi", "risk", "detail"}),
    "product_comparison": frozenset({
        "identity", "price", "profit", "roi", "demand", "competition", "compliance", "risk",
        "recommendation", "decision",
    }),
    "profit_comparison": frozenset({"profit", "roi"}),
    "selection_recommendation": frozenset({
        "recommendation", "decision", "profit", "risk", "compliance",
    }),
}

# These are aliases for provider-emitted enum values, not natural-language query
# patterns.  Keep the table deliberately finite.
DIMENSION_ALIASES = {
    "analysis": ("detail",),
    "product_analysis": ("detail",),
    "profitability": ("profit",),
    "profit_analysis": ("profit",),
    "net_profit_margin": ("profit",),
    "profit_margin": ("profit",),
    "margin": ("profit",),
    "cost_breakdown": ("profit",),
    "cost_structure": ("profit",),
    "cost": ("profit",),
    "costs": ("profit",),
    "cost_analysis": ("profit",),
    "cost_components": ("profit",),
    "profit_calculation": ("profit", "calculation"),
    "calculation_explanation": ("calculation",),
    "calculation_method": ("calculation",),
    "calculation_formula": ("calculation",),
    "data_freshness": ("freshness",),
    "recency": ("freshness",),
    "price_freshness": ("price", "freshness"),
    "price_recency": ("price", "freshness"),
    "source": ("provenance",),
    "source_traceability": ("provenance",),
}

METRIC_ALIASES = {
    "price": "current_price",
    "sale_price": "current_price",
    "profit": "net_profit",
    "profit_amount": "net_profit",
    "net_profit_margin": "net_margin",
    "profit_margin": "net_margin",
    "margin": "net_margin",
    "cost_structure": "cost_breakdown",
    "costs": "cost_breakdown",
    "data_freshness": "freshness",
    "recency": "freshness",
    "price_freshness": "freshness",
    "price_recency": "freshness",
}

METRIC_TO_DIMENSIONS = {
    "current_price": ("price",),
    "net_profit": ("profit",),
    "net_margin": ("profit",),
    "roi": ("roi",),
    "recommendation_score": ("recommendation",),
    "risk_level": ("risk",),
    "sales_growth_rate": ("sales_snapshot", "data_sufficiency"),
    "cost_breakdown": ("profit",),
    "freshness": ("freshness",),
}

DIMENSION_TO_METRIC = {
    "net_profit_margin": "net_margin",
    "profit_margin": "net_margin",
    "margin": "net_margin",
    "cost_breakdown": "cost_breakdown",
    "cost_structure": "cost_breakdown",
    "cost": "cost_breakdown",
    "costs": "cost_breakdown",
    "cost_analysis": "cost_breakdown",
    "cost_components": "cost_breakdown",
    "data_freshness": "freshness",
    "recency": "freshness",
    "price_freshness": "freshness",
    "price_recency": "freshness",
}


@dataclass(frozen=True)
class SemanticValueNormalization:
    dimensions: tuple[str, ...]
    metrics: tuple[str, ...]
    metric: str | None
    unknown_dimensions: tuple[str, ...]
    unknown_metrics: tuple[str, ...]


def _enum_value(value: str) -> str:
    """Normalize enum spelling only; never inspect user query text."""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", value.strip().casefold())).strip("_")


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def normalize_semantic_values(
    dimensions: Iterable[str],
    metrics: Iterable[str] = (),
    metric: str | None = None,
) -> SemanticValueNormalization:
    """Canonicalize finite provider values before SEM004 validates intent scope."""
    normalized_dimensions: list[str] = []
    normalized_metrics: list[str] = []
    unknown_dimensions: list[str] = []
    unknown_metrics: list[str] = []

    for raw in dimensions:
        value = _enum_value(raw)
        mapped = (value,) if value in CANONICAL_DIMENSIONS else DIMENSION_ALIASES.get(value)
        if not mapped:
            unknown_dimensions.append(value or raw)
            continue
        normalized_dimensions.extend(mapped)
        implied_metric = DIMENSION_TO_METRIC.get(value)
        if implied_metric:
            normalized_metrics.append(implied_metric)

    raw_metrics = [*metrics, *([metric] if metric else [])]
    for raw in raw_metrics:
        value = _enum_value(raw)
        canonical = value if value in CANONICAL_METRICS else METRIC_ALIASES.get(value)
        if not canonical:
            unknown_metrics.append(value or raw)
            continue
        normalized_metrics.append(canonical)
        normalized_dimensions.extend(METRIC_TO_DIMENSIONS[canonical])

    canonical_metrics = _unique(normalized_metrics)
    return SemanticValueNormalization(
        dimensions=_unique(normalized_dimensions),
        metrics=canonical_metrics,
        metric=canonical_metrics[0] if canonical_metrics else None,
        unknown_dimensions=_unique(unknown_dimensions),
        unknown_metrics=_unique(unknown_metrics),
    )


def semantic_output_contract() -> str:
    """Compact contract shared by provider prompts and backend validation."""
    return json.dumps({
        "semantic_value_contract": {
            "intents": list(SEMANTIC_PLANNER_INTENTS),
            "requested_dimensions": list(CANONICAL_DIMENSIONS),
            "metrics": list(CANONICAL_METRICS),
            "intent_allowed_dimensions": {
                key: sorted(values) for key, values in INTENT_ALLOWED_DIMENSIONS.items()
            },
            "layering": {
                "intent": "task type",
                "requested_dimensions": "business analysis areas",
                "metrics": "specific measurements or analysis modes inside a dimension",
            },
        }
    }, ensure_ascii=False, sort_keys=True)
