"""Reproducible, provider-free semantic regression over a persisted corpus.

Run with ``python -m evals.generalization_runner`` from backend/. The corpus is
authored ground truth; this runner never generates cases or calls a model.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.query_understanding import understand_query
from app.schemas.agent import CollectionState, ContextSnapshot, TaskState


CORPUS_VERSION = "generalization_v1"
CORPUS_PATH = Path(__file__).parent / "cases" / "generalization_v1.jsonl"


class CorpusContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active_collection: bool = False
    active_result_set: bool = False
    active_scenario: bool = False
    last_explicit_entity: str | None = None
    thresholds: dict[str, float] = Field(default_factory=dict)


class ExpectedFrame(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: str
    reference_role: str | None = None
    primary_operation: str | None = None
    selectors: dict[str, Any] | None = None
    filters: list[dict[str, Any]] | None = None
    requested_analyses: list[str] | None = None
    requested_dimensions: list[str] | None = None
    product_titles: list[str] | None = None
    expected_clarification: bool
    clarification_reason: str | None = None
    ordinal_references: list[int] | None = None
    ground_truth_basis: str | None = None


class GeneralizationCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    corpus_version: str
    case_id: str = Field(min_length=1)
    semantic_class: str = Field(min_length=1)
    query: str = Field(min_length=1)
    context: CorpusContext
    expected: ExpectedFrame
    metamorphic_group_id: str | None = None
    contrast_group_id: str | None = None
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_version(self):
        if self.corpus_version != CORPUS_VERSION:
            raise ValueError(f"unsupported corpus version: {self.corpus_version}")
        return self


def load_corpus(path: Path = CORPUS_PATH) -> list[GeneralizationCase]:
    cases: list[GeneralizationCase] = []
    ids: set[str] = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        case = GeneralizationCase.model_validate_json(line)
        if case.case_id in ids:
            raise ValueError(f"duplicate case_id on line {number}: {case.case_id}")
        ids.add(case.case_id)
        cases.append(case)
    if not cases:
        raise ValueError("generalization corpus is empty")
    return cases


def _snapshot(case: GeneralizationCase) -> ContextSnapshot:
    collection = None
    if case.context.active_collection:
        collection = CollectionState(
            collection_type="candidate_pool", product_ids=["p1", "p2", "p3", "p4"],
            ranked_product_ids=["p1", "p2", "p3", "p4"], top_product_ids=["p1", "p2", "p3"],
            top_k=3, ranking_dimensions=["recommendation"],
            requested_dimensions=["recommendation"], source_task_revision=1, created_turn=1,
        )
    return ContextSnapshot(
        session_id=f"corpus-{case.case_id}", company_id="fixture-company", user_id="fixture-user",
        current_query=case.query, task_state=TaskState(active_collection=collection),
        last_explicit_product_ids=["p1"] if case.context.last_explicit_entity else [],
    )


def evaluate_case(case: GeneralizationCase) -> dict[str, Any]:
    parsed, frame = understand_query(case.query, [], (), _snapshot(case))
    actual = {
        "intent": parsed.name,
        "reference_role": frame.scenario_reference_role or frame.collection_reference,
        "primary_operation": frame.operation,
        "selectors": frame.collection_selector,
        "filters": frame.collection_filters,
        "requested_analyses": frame.analysis_requests,
        "requested_dimensions": frame.requested_dimensions,
        "product_titles": frame.entity_mentions,
        "expected_clarification": frame.route == "CLARIFICATION" or frame.clarification_required,
        "clarification_reason": frame.clarification_reason,
        "ordinal_references": frame.ordinal_references,
    }
    expected = case.expected.model_dump()
    failures = []
    for key, value in expected.items():
        if value is None or key == "ground_truth_basis":
            continue
        if key == "clarification_reason":
            if value not in (actual[key] or ""):
                failures.append(key)
        elif actual[key] != value:
            failures.append(key)
    return {"case_id": case.case_id, "semantic_class": case.semantic_class,
            "query": case.query, "passed": not failures, "failed_fields": failures,
            "expected": expected, "actual": actual}


def run(cases: list[GeneralizationCase]) -> dict[str, Any]:
    results = [evaluate_case(case) for case in cases]
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case, result in zip(cases, results):
        if case.metamorphic_group_id:
            groups[case.metamorphic_group_id].append(result)
    consistent = 0
    for members in groups.values():
        keys = ("intent", "reference_role", "primary_operation", "selectors", "filters")
        signatures = [{key: result["actual"][key] for key in keys} for result in members]
        if all(signature == signatures[0] for signature in signatures[1:]):
            consistent += 1
    per_class = Counter(result["semantic_class"] for result in results if result["passed"])
    class_total = Counter(case.semantic_class for case in cases)
    negative = [result for case, result in zip(cases, results) if "negative_contrast" in case.tags]
    ambiguity = [result for case, result in zip(cases, results) if "ambiguity" in case.tags]
    return {
        "corpus_version": CORPUS_VERSION, "total": len(results),
        "passed": sum(result["passed"] for result in results),
        "failed": sum(not result["passed"] for result in results),
        "per_semantic_class": {name: {"passed": per_class[name], "total": total}
                               for name, total in sorted(class_total.items())},
        "metamorphic": {"groups": len(groups), "passed": consistent,
                         "failed": len(groups) - consistent,
                         "consistency_rate": round(consistent / len(groups), 4) if groups else None},
        "negative_contrast": {"passed": sum(item["passed"] for item in negative), "total": len(negative)},
        "ambiguity": {"passed": sum(item["passed"] for item in ambiguity), "total": len(ambiguity)},
        "wrong_guess": sum(bool(result["actual"]["product_titles"]) for result in ambiguity
                           if result["expected"]["expected_clarification"]),
        "product_entity_false_positive": sum(
            bool(result["actual"]["product_titles"]) for result in results
            if result["expected"]["product_titles"] == []
        ),
        "failures": [result for result in results if not result["passed"]],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-id")
    parser.add_argument("--semantic-class")
    args = parser.parse_args()
    cases = load_corpus()
    if args.case_id:
        cases = [case for case in cases if case.case_id == args.case_id]
    if args.semantic_class:
        cases = [case for case in cases if case.semantic_class == args.semantic_class]
    if not cases:
        parser.error("no cases match the requested filters")
    report = run(cases)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return int(report["failed"] > 0 or report["metamorphic"]["failed"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
