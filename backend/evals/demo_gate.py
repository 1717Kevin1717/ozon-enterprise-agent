"""Deterministic release gate for the separately versioned demo contract."""

from __future__ import annotations

from pathlib import Path

from app.agent.capabilities import CAPABILITIES
from evals.runner import run_case, summarize
from evals.schemas import load_cases


BACKEND = Path(__file__).resolve().parents[1]
DEMO_CASES = BACKEND / "evals" / "cases" / "demo_real_user_v1.jsonl"


def validate_capability_contract(cases) -> tuple[int, int, list[str]]:
    by_id = {case.case_id: case for case in cases}
    visible = verified = 0
    failures: list[str] = []
    for capability in CAPABILITIES:
        suggestion = capability.get("suggestion")
        if not suggestion:
            continue
        visible += 1
        case = by_id.get(str(suggestion.get("golden_case_id") or ""))
        valid = bool(
            capability.get("demo_verified")
            and case
            and suggestion.get("prompt") == case.query
            and capability.get("backend_handlers")
            and capability.get("renderer")
        )
        if valid:
            verified += 1
        else:
            failures.append(str(capability.get("capability_id") or "unknown"))
    return visible, verified, failures


def main() -> int:
    cases = load_cases(DEMO_CASES)
    visible, verified, contract_failures = validate_capability_contract(cases)
    rows = []
    for case in cases:
        row = run_case(case)
        rows.append(row)
        print(f"{'PASS' if row['passed'] else 'FAIL'} {case.case_id}")
    summary = summarize(rows)
    passed = summary["overall_case_pass_rate"]["passed"]
    total = summary["overall_case_pass_rate"]["total"]
    failed_ids = [row["case_id"] for row in rows if not row["passed"]]
    cases_by_id = {case.case_id: case for case in cases}
    entity_false_positives = 0
    generic_clarifications = 0
    renderer_trace_ok = True
    for row in rows:
        result = row.get("execution", {}).get("result", {})
        expected = cases_by_id[row["case_id"]].expected
        if expected.intent == "collection_analysis" and result.get("intent") not in {
            "collection_analysis", "scenario_analysis",
        }:
            entity_false_positives += 1
        if expected.response_type != "clarification" and result.get("response_type") == "clarification":
            generic_clarifications += 1
        if not isinstance(result.get("trace"), list) or not result.get("trace"):
            renderer_trace_ok = False
    provider_recovery_ok = next(
        (row["passed"] for row in rows if row["case_id"] == "D19"), False,
    )
    p0 = len(failed_ids) + len(contract_failures)
    print(f"Visible Suggestions: {visible}")
    print(f"Verified Suggestions: {verified}/{visible}")
    print(f"Demo Golden: {passed}/{total}")
    print(f"P0: {p0}")
    print(f"Entity False Positive: {entity_false_positives}")
    print(f"Generic Clarification: {generic_clarifications}")
    print(f"Provider Failure Recovery: {'PASS' if provider_recovery_ok else 'FAIL'}")
    print(f"Renderer Trace: {'PASS' if renderer_trace_ok else 'FAIL'}")
    print(f"Failed Cases: {', '.join(failed_ids) or 'NONE'}")
    print(f"Capability Contract Failures: {', '.join(contract_failures) or 'NONE'}")
    ready = bool(
        p0 == 0 and verified == visible and entity_false_positives == 0
        and generic_clarifications == 0 and provider_recovery_ok and renderer_trace_ok
    )
    print("DEMO READY" if ready else "DEMO NOT READY")
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
