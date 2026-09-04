"""Deterministic assertions over validated results, never LLM-as-a-Judge."""
from __future__ import annotations

import json
from collections import Counter

from .schemas import AssertionResult, GoldenCase

MISSING = "<missing>"


def lookup(value, path):
    for key in path.split("."):
        if isinstance(value, list) and key.isdigit() and int(key) < len(value):
            value = value[int(key)]
        elif isinstance(value, dict) and key in value:
            value = value[key]
        else:
            return MISSING
    return value


def business_text(result):
    return json.dumps({key: result.get(key) for key in ("answer", "model_summary", "evidence", "warnings", "risks", "next_actions")}, ensure_ascii=False)


class Evaluator:
    metric = ""

    def check(self, name, expected, actual, *, passed=None, reason=""):
        passed = expected == actual if passed is None else passed
        return AssertionResult(evaluator=type(self).__name__, metric=self.metric, assertion=name, passed=bool(passed), expected=expected, actual=actual, reason=reason or ("Matches ground truth" if passed else "Does not match required ground truth"))

    def fields(self, expected, actual, names):
        return [self.check(name, getattr(expected, name), lookup(actual, name)) for name in names if getattr(expected, name) is not None]


class IntentEvaluator(Evaluator):
    metric = "intent_accuracy"
    def evaluate(self, case, run):
        return self.fields(case.expected, run["result"], ["intent"])


class ResponseTypeEvaluator(Evaluator):
    metric = "response_type_accuracy"
    def evaluate(self, case, run):
        return self.fields(case.expected, run["result"], ["response_type"])


class EntityResolutionEvaluator(Evaluator):
    metric = "entity_resolution_accuracy"
    def evaluate(self, case, run):
        result, expected = run["result"], case.expected
        rows = []
        values = {
            "entity_statuses": [item["status"] for item in result.get("requested_entities", [])],
            "resolved_titles": [item["name"] for item in result.get("entities", [])],
            "product_titles": [item["title"] for item in result.get("products", [])],
            "candidate_titles": [candidate["name"] for item in result.get("requested_entities", []) for candidate in item.get("candidates", [])],
        }
        for name, actual in values.items():
            truth = getattr(expected, name)
            if truth is not None:
                rows.append(self.check(name, truth if name == "entity_statuses" else sorted(truth), actual if name == "entity_statuses" else sorted(actual)))
        if rows:
            identities = {item["id"]: item["title"] for item in run["catalog"]}
            entities = result.get("entities", [])
            rows.append(self.check("resolved_id_identity", True, all(identities.get(item["product_id"]) == item["name"] for item in entities)))
        return rows


class ToolScopeEvaluator(Evaluator):
    metric = "tool_scope_accuracy"
    def evaluate(self, case, run):
        expected, result = case.expected, run["result"]
        rows = []
        if expected.tool_names is not None:
            actual = sorted({item["tool_name"] for item in result.get("tool_results", []) if item["status"] in {"success", "failed"}})
            rows.append(self.check("executed_tools", sorted(expected.tool_names), actual))
        if expected.tool_call_count_max is not None:
            actual = result.get("tool_call_count", MISSING)
            rows.append(self.check("tool_call_budget", {"maximum": expected.tool_call_count_max}, actual, passed=isinstance(actual, int) and actual <= expected.tool_call_count_max))
        return rows


class RequestedDimensionEvaluator(Evaluator):
    metric = "response_scope_accuracy"
    def evaluate(self, case, run):
        expected, result = case.expected, run["result"]
        rows = []
        if expected.requested_dimensions is not None:
            rows.append(self.check("requested_dimensions", sorted(expected.requested_dimensions), sorted(result.get("requested_dimensions", []))))
        for key in expected.empty_fields:
            rows.append(self.check(f"empty:{key}", [], result.get(key, MISSING)))
        for text in expected.forbidden_content:
            rows.append(self.check(f"forbidden_content:{text}", False, text in business_text(result)))
        for scope in expected.forbidden_display_scope:
            rows.append(self.check(f"forbidden_display_scope:{scope}", False, scope in result.get("display_scope", [])))
        return rows


class DecisionPolicyEvaluator(Evaluator):
    metric = "decision_policy_accuracy"
    def evaluate(self, case, run):
        rows = self.fields(case.expected, run["result"], ["decision_status", "human_review_required"])
        for text in case.expected.required_content:
            rows.append(self.check(f"required_content:{text}", True, text in run["result"].get("answer", "")))
        return rows


class DataSufficiencyEvaluator(Evaluator):
    metric = "data_sufficiency_accuracy"
    def evaluate(self, case, run):
        if case.expected.data_sufficiency is None:
            return []
        actual = run["result"].get("data_sufficiency") or {}
        return [self.check(name, truth, actual.get(name, MISSING)) for name, truth in case.expected.data_sufficiency.model_dump(exclude_none=True).items()]


class FactGroundingEvaluator(Evaluator):
    metric = "fact_grounding_accuracy"
    def evaluate(self, case, run):
        result, expected = run["result"], case.expected
        rows = [self.check(path, truth, lookup(result, path)) for path, truth in expected.required_facts.items()]
        # Validate rendered product identities/prices against the pre-run fixture,
        # not against another Agent answer or the same search/filter function.
        catalog = {item["id"]: item for item in run["catalog"]}
        products = result.get("products", [])
        if products:
            grounded = all(p["id"] in catalog and p["title"] == catalog[p["id"]]["title"] and p["current_price"] == catalog[p["id"]]["price"] for p in products)
            rows.append(self.check("fixture_product_identity_and_price", True, grounded))
        fact = result.get("fact") or {}
        if fact.get("name") == "product_price":
            source = catalog.get(fact.get("product_id")) or {}
            rows.append(self.check("fact_price_matches_fixture", source.get("price", MISSING), fact.get("value", MISSING)))
        if expected.filter_truth:
            truth = expected.filter_truth
            # Independent predicate over pre-query source fields. No filter_products call.
            matches = [p for p in run["catalog"] if (truth.min_margin_rate is None or p["margin"] >= truth.min_margin_rate) and (truth.risk_level is None or p["risk"] == truth.risk_level) and (truth.compliance_status is None or p["compliance"] == truth.compliance_status)]
            expected_ids = sorted(p["id"] for p in matches)
            actual_ids = sorted(p["id"] for p in products)
            rows += [self.check("filter_exact_ids", expected_ids, actual_ids), self.check("filter_minimum_matches", truth.min_matches, len(matches), passed=len(matches) >= truth.min_matches)]
            for name in ("matched_count", "total_count", "displayed_count"):
                rows.append(self.check(name, len(matches), result.get(name, MISSING)))
            for name, value in truth.model_dump(exclude_none=True).items():
                if name != "min_matches":
                    rows.append(self.check(f"filter_criteria.{name}", value, lookup(result, f"filter_criteria.{name}")))
        return rows


class StateLeakageEvaluator(Evaluator):
    metric = "state_accuracy"
    def evaluate(self, case, run):
        return self.fields(case.expected, run["result"], ["selection_source"])


class IdempotencyEvaluator(Evaluator):
    metric = "fallback_accuracy"
    def evaluate(self, case, run):
        result, expected = run["result"], case.expected
        rows = self.fields(expected, result, ["fallback_used", "task_completed", "duplicate_tool_execution"])
        if expected.fallback_used is None and expected.reuse_tool is None and expected.duplicate_tool_execution is None:
            for row in rows:
                row.metric = "task_completion_accuracy"
        if expected.duplicate_tool_execution is not None:
            calls = [json.dumps([item["tool_name"], item["normalized_args"]], sort_keys=True) for item in result["tool_results"] if item["status"] == "success"]
            rows.append(self.check("independently_counted_duplicates", 0, sum(count - 1 for count in Counter(calls).values())))
        if expected.reuse_tool:
            trace = result.get("trace", [])
            reused = any(item["event"] == "tool_reused" and item["tool"] == expected.reuse_tool for item in trace)
            executed = sum(item["tool_name"] == expected.reuse_tool and item["status"] == "success" for item in result["tool_results"])
            rows += [self.check("successful_result_reused", True, reused), self.check("reused_tool_executed_once", 1, executed), self.check("mock_rounds_exercised", 2, run.get("mock_calls", 0))]
        return rows


EVALUATORS = (IntentEvaluator(), ResponseTypeEvaluator(), EntityResolutionEvaluator(), ToolScopeEvaluator(), RequestedDimensionEvaluator(), DecisionPolicyEvaluator(), DataSufficiencyEvaluator(), FactGroundingEvaluator(), StateLeakageEvaluator(), IdempotencyEvaluator())


def evaluate(case: GoldenCase, run: dict) -> list[AssertionResult]:
    return [row for evaluator in EVALUATORS for row in evaluator.evaluate(case, run)]
