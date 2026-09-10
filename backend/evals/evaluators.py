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
            # Required facts may live in the user-facing answer or in validated
            # structured evidence. This keeps presentation wording independent
            # from the deterministic calculation/provenance contract.
            rows.append(self.check(f"required_content:{text}", True, text in business_text(run["result"])))
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
        if expected.provenance:
            rows += self.provenance_assertions(expected.provenance, run)
        return rows

    def provenance_assertions(self, truth, run):
        result = run["result"]
        fields = [field for item in result.get("evidence", []) for field in item.get("fields", [])]
        matches = [item for item in fields if truth.field is None or item["field"] == truth.field]
        rows = []
        if truth.field:
            rows.append(self.check("provenance_field_present", True, bool(matches)))
        for item in matches:
            for name in ("source_type", "provider", "is_mock", "evidence_status"):
                expected = getattr(truth, name)
                if expected is not None:
                    rows.append(self.check(f"provenance.{item['field']}.{name}", expected, item.get(name)))
            if truth.freshness:
                rows.append(self.check("freshness", truth.freshness, item["freshness"]["status"]))
            if truth.require_collected_at:
                rows.append(self.check("collected_at_exists", True, bool(item.get("collected_at"))))
            if truth.derived_inputs:
                rows.append(self.check("derived_input_fields", True, set(truth.derived_inputs).issubset(item.get("derived_from", []))))
                rows.append(self.check("derived_input_values", True, all(value.get("value") is not None for value in item.get("inputs", []))))
            if item.get("is_mock"):
                rows.append(self.check("mock_not_marketplace_real", True, item.get("source_type") in {"mock", "derived"} and not item.get("source_url")))
        def bound(item, product_id):
            return item.get("product_id") == product_id and item.get("company_id") == run["company_id"] and all(bound(child, product_id) for child in item.get("inputs", []))
        if truth.product_binding:
            rows.append(self.check("evidence_product_tenant_binding", True, all(bound(field, item["product_id"]) for item in result.get("evidence", []) for field in item.get("fields", []))))
        notices = {item["code"] for item in result.get("trust_notices", []) if truth.field is None or item["field"] == truth.field}
        for code in truth.notice_codes:
            rows.append(self.check("required_trust_notice:" + code, True, code in notices))
        for code in truth.forbidden_notice_codes:
            rows.append(self.check("forbidden_trust_notice:" + code, False, code in notices))
        if truth.tenant_isolation:
            denials = run.get("foreign_denials", [])
            rows.append(self.check("foreign_tool_denied", True, len(denials) == 3 and all(not item.get("success") and item.get("error_code") == "NOT_FOUND" and "data" not in item for item in denials)))
            rows.append(self.check("foreign_evidence_hidden", [], result.get("evidence", [])))
        for row in rows:
            row.metric = "provenance_accuracy"
        return rows


class StateLeakageEvaluator(Evaluator):
    metric = "state_accuracy"
    def evaluate(self, case, run):
        return self.fields(case.expected, run["result"], ["selection_source"])


class ConversationContextEvaluator(Evaluator):
    """Deterministic context/source/scope assertions for conversational cases."""

    def evaluate(self, case, run):
        if case.category not in {"conversation_context", "semantic_orchestration"}:
            return []
        result, expected = run["result"], case.expected
        rows = []
        if expected.context_source is not None:
            source = result.get("context_source", MISSING)
            metric = "selection_binding_accuracy" if expected.context_source == "ui_selection" else "session_isolation_accuracy" if expected.context_source in {"none", "session_state"} and expected.response_type == "clarification" else "reference_resolution_accuracy"
            row = self.check("context_source", expected.context_source, source)
            row.metric = metric
            rows.append(row)
        if expected.product_titles is not None:
            actual = sorted(item.get("title") for item in result.get("products", []))
            row = self.check("context_products", sorted(expected.product_titles), actual)
            row.metric = "context_resolution_accuracy"
            rows.append(row)
        if expected.semantic_route is not None:
            row = self.check("semantic_route", expected.semantic_route, (result.get("understanding") or {}).get("route"))
            row.metric = "semantic_route_accuracy"
            rows.append(row)
        if expected.requested_dimensions is not None:
            row = self.check("contextual_dimensions", sorted(expected.requested_dimensions), sorted(result.get("requested_dimensions", [])))
            row.metric = "contextual_scope_accuracy"
            rows.append(row)
        return rows


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


class SemanticUnderstandingEvaluator(Evaluator):
    def evaluate(self, case, run):
        if case.category != "semantic_understanding":
            return []
        result, expected = run["result"], case.expected
        understanding = result.get("understanding") or {}
        checks = [
            ("intent_understanding_accuracy", "intent", expected.intent, understanding.get("intent")),
            ("entity_span_accuracy", "entity_mentions", expected.entity_mentions, understanding.get("entity_mentions")),
            ("semantic_route_accuracy", "route", expected.semantic_route, understanding.get("route")),
            ("semantic_route_accuracy", "planner_calls", expected.planner_calls, understanding.get("planner_calls")),
            ("reference_resolution_accuracy", "selection_source", expected.selection_source, result.get("selection_source")),
            ("reference_resolution_accuracy", "reference_field", expected.reference_field, understanding.get("reference_field")),
            ("response_scope_accuracy", "response_type", expected.response_type, result.get("response_type")),
        ]
        rows = []
        for metric, name, truth, actual in checks:
            if truth is not None:
                row = self.check(name, truth, actual)
                row.metric = metric
                rows.append(row)
        for scope in expected.forbidden_display_scope:
            row = self.check("forbidden_scope:"+scope, False, scope in result.get("display_scope", []))
            row.metric = "response_scope_accuracy"
            rows.append(row)
        return rows


class ModelRouterEvaluator(Evaluator):
    metric = "model_routing_accuracy"

    def evaluate(self, case, run):
        if case.category not in {"natural_language_generalization", "contextual_followup", "model_routing"}:
            return []
        result, expected = run["result"], case.expected
        rows = self.fields(expected, result, ["active_provider", "requested_provider", "fallback_provider", "model_route"])
        if expected.requires_reasoning is not None:
            rows.append(self.check(
                "requires_reasoning", expected.requires_reasoning,
                bool((result.get("understanding") or {}).get("requires_reasoning")),
            ))
        if expected.provider_call_count_max is not None:
            actual = len(result.get("provider_calls") or [])
            rows.append(self.check("provider_call_count_max", expected.provider_call_count_max, actual, passed=actual <= expected.provider_call_count_max))
        for row in rows:
            row.metric = self.metric
        return rows


EVALUATORS = (IntentEvaluator(), ResponseTypeEvaluator(), EntityResolutionEvaluator(), ToolScopeEvaluator(), RequestedDimensionEvaluator(), DecisionPolicyEvaluator(), DataSufficiencyEvaluator(), FactGroundingEvaluator(), StateLeakageEvaluator(), IdempotencyEvaluator(), SemanticUnderstandingEvaluator(), ConversationContextEvaluator(), ModelRouterEvaluator())


def evaluate(case: GoldenCase, run: dict) -> list[AssertionResult]:
    return [row for evaluator in EVALUATORS for row in evaluator.evaluate(case, run)]
