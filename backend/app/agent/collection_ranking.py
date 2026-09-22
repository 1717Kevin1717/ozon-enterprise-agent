"""Shared deterministic ranking semantics for canonical and hypothetical collections."""
from __future__ import annotations

from typing import Any, Callable, Iterable, TypeVar


T = TypeVar("T")
RANKING_DIMENSIONS = frozenset({
    "recommendation", "profit", "roi", "risk", "demand", "competition", "compliance",
})


def normalize_ranking_dimensions(dimensions: Iterable[str] | None) -> tuple[str, ...]:
    normalized = tuple(dict.fromkeys(
        str(item).strip().casefold() for item in (dimensions or ())
        if str(item).strip().casefold() in RANKING_DIMENSIONS
    ))
    return normalized or ("recommendation",)


def ranking_metric(record: Any, dimension: str) -> float:
    """Read one canonical ranking key from a dict or validated result model."""

    def read(name: str, default: Any = 0) -> Any:
        if isinstance(record, dict):
            analysis = record.get("analysis") or {}
            return analysis.get(name, record.get(name, default))
        return getattr(record, name, default)

    return {
        "recommendation": float(read("recommendation_score") or 0),
        "profit": float(read("net_margin", read("current_margin_rate")) or 0),
        "roi": float(read("roi") or 0),
        "risk": {"low": 0.0, "medium": 1.0, "high": 2.0}.get(str(read("risk_level", "")).casefold(), 3.0),
        "demand": float(read("demand_score") or 0),
        "competition": float(read("competition_score") or 0),
        "compliance": float(read("compliance_score") or 0),
    }.get(dimension, float(read("recommendation_score") or 0))


def rank_collection(
    records: Iterable[T],
    dimensions: Iterable[str] | None,
    *,
    identity: Callable[[T], str],
) -> list[T]:
    """Stable multi-key ordering shared by collection tools and ScenarioState."""

    ordered = list(records)
    normalized = normalize_ranking_dimensions(dimensions)
    for dimension in reversed(normalized):
        ordered.sort(
            key=lambda item, dimension=dimension: (ranking_metric(item, dimension), identity(item)),
            reverse=dimension != "risk",
        )
    return ordered
