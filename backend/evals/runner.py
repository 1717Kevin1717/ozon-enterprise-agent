from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

from .evaluators import evaluate
from .fixtures import safe_environment
from .schemas import AssertionResult, GoldenCase, load_cases

BACKEND = Path(__file__).resolve().parents[1]
DEFAULT_CASES = BACKEND / "evals" / "cases" / "golden_cases_v1.jsonl"


def select_cases(cases, case_id=None, category=None):
    selected = [case for case in cases if (case_id is None or case.case_id == case_id) and (category is None or case.category == category)]
    if not selected:
        raise ValueError("No cases match the requested case_id/category")
    return selected


def execute_case(case: GoldenCase) -> dict:
    try:
        process = subprocess.run([sys.executable, "-m", "evals.runner", "--worker"], input=case.model_dump_json(), text=True, encoding="utf-8", capture_output=True, cwd=BACKEND, env=safe_environment(), timeout=60)
        payload = json.loads(process.stdout)
        if process.returncode and "error" not in payload:
            raise ValueError("Worker failed without an error envelope")
        return payload
    except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
        return {"error": {"type": type(exc).__name__, "reason": "Isolated worker failed or exceeded 60 seconds; no live service was used."}}


def run_case(case: GoldenCase) -> dict:
    run = execute_case(case)
    if "error" in run:
        assertions = [AssertionResult(evaluator="ExecutionSafetyEvaluator", metric="execution_safety", assertion="isolated_execution", passed=False, expected="valid isolated AgentRunResult", actual=run["error"], reason="Execution/fixture error; business ground truth was not changed")]
    else:
        safety = run.get("safety", {})
        safe = safety.get("database_url") == "sqlite+aiosqlite:///:memory:" and safety.get("real_key_loaded") is False and safety.get("settings_without_dotenv", 0) >= 1 and safety.get("memory_engines", 0) >= 1 and safety.get("blocked_http_attempts") == 0
        assertions = [AssertionResult(evaluator="ExecutionSafetyEvaluator", metric="execution_safety", assertion="isolated_execution", passed=safe, expected="in-memory DB; dotenv/keys not loaded; no HTTP attempt", actual=safety, reason="Isolation guards verified" if safe else "Safety guard evidence missing or network attempted")]
        assertions += evaluate(case, run)
    return {"case_id": case.case_id, "category": case.category, "query": run.get("query", case.query), "query_template": case.query, "provenance": case.provenance, "passed": all(item.passed for item in assertions), "assertions": [item.model_dump() for item in assertions], "execution": {key: run[key] for key in ("result", "error", "safety", "mock_calls") if key in run}}


def score(passed, total):
    return {"passed": passed, "total": total, "rate": round(passed / total, 6) if total else None}


def summarize(rows):
    assertions = [item for row in rows for item in row["assertions"]]
    metrics, categories = defaultdict(list), defaultdict(list)
    for row in rows:
        categories[row["category"]].append(row["passed"])
        if row["category"] == "semantic_understanding":
            route = row.get("execution", {}).get("result", {}).get("understanding", {}).get("route")
            name = {"DETERMINISTIC_FAST_PATH": "rule_fast_path_case_accuracy", "SEMANTIC_PLANNER": "semantic_fallback_case_accuracy", "CLARIFICATION": "semantic_clarification_case_accuracy"}.get(route)
            if name:
                metrics[name].append(row["passed"])
        by_metric = defaultdict(list)
        for assertion in row["assertions"]:
            by_metric[assertion["metric"]].append(assertion["passed"])
        for metric, results in by_metric.items():
            metrics[metric].append(all(results))
    return {
        "overall_case_pass_rate": score(sum(row["passed"] for row in rows), len(rows)),
        "assertion_pass_rate": score(sum(row["passed"] for row in assertions), len(assertions)),
        "metrics": {name: score(sum(results), len(results)) for name, results in sorted(metrics.items())},
        "categories": {name: score(sum(results), len(results)) for name, results in sorted(categories.items())},
        "metric_definition": "Each metric uses eligible cases, passing only when ALL assertions for that metric pass. Inapplicable metrics are omitted, never counted as passes.",
    }


def markdown_report(report):
    summary = report["summary"]
    overall = summary["overall_case_pass_rate"]
    lines = ["# Golden Agent Evaluation v1", "", f"UTC: {report['created_at']}", "", "Mock Enterprise Dataset ≠ real Ozon data. Deterministic judge only; no real GLM stability conclusion; not Production Ready.", "", f"Total Cases: {overall['total']} | Passed: {overall['passed']} | Failed: {overall['total'] - overall['passed']} | Pass Rate: {overall['rate']:.2%}", "", "## Metrics", "", "| Metric | Passed / Eligible | Rate |", "| --- | --- | --- |"]
    for name, item in {"case_pass_rate": overall, "assertion_pass_rate": summary["assertion_pass_rate"], **summary["metrics"]}.items():
        lines.append(f"| {name} | {item['passed']} / {item['total']} | {item['rate']:.2%} |")
    lines += ["", summary["metric_definition"], "", "## Categories", "", "| Category | Passed / Total | Rate |", "| --- | --- | --- |"]
    for name, item in summary["categories"].items():
        lines.append(f"| {name} | {item['passed']} / {item['total']} | {item['rate']:.2%} |")
    lines += ["", "## Failed Cases", ""]
    failed = [row for row in report["cases"] if not row["passed"]]
    if not failed:
        lines.append("None.")
    for row in failed:
        lines += [f"### {row['case_id']}", "", f"Query: {row['query']}", ""]
        for item in row["assertions"]:
            if not item["passed"]:
                lines += [f"- {item['evaluator']} / {item['assertion']}: {item['reason']}", f"  - expected: {json.dumps(item['expected'], ensure_ascii=False)}", f"  - actual: {json.dumps(item['actual'], ensure_ascii=False)}"]
    lines += ["", "## Case Index", "", "| Case | Category | Result |", "| --- | --- | --- |"]
    lines += [f"| {row['case_id']} | {row['category']} | {'PASS' if row['passed'] else 'FAIL'} |" for row in report["cases"]]
    lines += ["", "## Provenance", "", f"Dataset SHA256: {report['dataset_sha256']}", "", "Ground truth: manually confirmed stability cases, fixed Mock catalogue values, and explicit deterministic rules. Filter oracle evaluates pre-query catalogue fields independently, not the Agent filter tool. Metric denominators and full expected/actual assertions are retained in JSON."]
    return "\n".join(lines) + "\n"


def write_reports(report, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stem = "eval_v1_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S_%fZ")
    json_path, md_path = directory / f"{stem}.json", directory / f"{stem}.md"
    with json_path.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    with md_path.open("x", encoding="utf-8") as stream:
        stream.write(markdown_report(report))
    return json_path, md_path


def run_suite(dataset=DEFAULT_CASES, *, case_id=None, category=None, report_dir=None):
    dataset = Path(dataset)
    cases = select_cases(load_cases(dataset), case_id, category)
    rows = []
    for case in cases:
        row = run_case(case)
        rows.append(row)
        failures = [item["evaluator"] for item in row["assertions"] if not item["passed"]]
        print(f"{'PASS' if row['passed'] else 'FAIL'} {case.case_id} ({case.category})" + (f" — {', '.join(sorted(set(failures)))}" if failures else ""), flush=True)
    report = {"suite_version": "golden-v1", "dataset_kind": "mock_not_real_ozon", "judge_mode": "deterministic_no_llm_judge", "created_at": datetime.now(UTC).isoformat(), "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(), "fixture_source_sha256": hashlib.sha256((BACKEND / "app/services/demo_dataset.py").read_bytes()).hexdigest(), "summary": summarize(rows), "cases": rows}
    paths = write_reports(report, report_dir or BACKEND / "evals/reports")
    overall = report["summary"]["overall_case_pass_rate"]
    print(f"Total {overall['total']} | Passed {overall['passed']} | Failed {overall['total']-overall['passed']} | Case pass rate {overall['rate']:.2%}")
    print(f"JSON: {paths[0]}\nMarkdown: {paths[1]}")
    return report, paths


def main():
    parser = argparse.ArgumentParser(description="Isolated Golden Agent Evaluation; no dotenv, production DB or real LLM")
    parser.add_argument("--case-id")
    parser.add_argument("--category")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        try:
            from .fixtures import execute_isolated
            result = execute_isolated(GoldenCase.model_validate_json(sys.stdin.read()))
            print(json.dumps(result, ensure_ascii=False))
            return 0
        except Exception as exc:
            # Process boundary: keep failure visible but never echo environment or traceback.
            print(json.dumps({"error": {"type": type(exc).__name__, "reason": "Fixture/Agent execution failed in isolated worker"}}))
            return 2
    try:
        report, _ = run_suite(args.dataset, case_id=args.case_id, category=args.category, report_dir=args.report_dir)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0 if all(row["passed"] for row in report["cases"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
