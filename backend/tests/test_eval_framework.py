"""Framework tests; the Golden dataset itself is executed by the CLI runner."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from evals.evaluators import IntentEvaluator, FactGroundingEvaluator
from evals.runner import BACKEND, DEFAULT_CASES, run_case, select_cases, summarize, write_reports
from evals.schemas import GoldenCase, load_cases


def case_fixture():
    return GoldenCase(case_id="TEST_COUNT", category="simple_fact", query="公司一共有多少商品？", provenance="Deterministic mock fixture row count; not live Ozon data", expected={"intent": "company_product_count", "required_facts": {"fact.value": 60}})


def test_golden_jsonl_schema_and_coverage():
    cases = load_cases(DEFAULT_CASES)
    assert 20 <= len(cases) <= 30
    assert {case.category for case in cases} == {"simple_fact", "entity", "filtering", "comparison", "policy", "data_sufficiency", "state", "fallback"}
    assert all("not real Ozon" in case.provenance for case in cases)


def test_duplicate_case_id_rejected(tmp_path):
    path = tmp_path / "duplicate.jsonl"
    line = case_fixture().model_dump_json()
    path.write_text(line + "\n" + line, encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate case_id"):
        load_cases(path)


@pytest.mark.parametrize("expected", [{"response_type": "invented_type"}, {"tool_call_count_max": "one"}, {"made_up_rule": True}, {"filter_truth": {}}, {"required_facts": {"secret.token": "x"}}, {}])
def test_invalid_expected_structure_rejected(expected):
    data = case_fixture().model_dump()
    data["expected"] = expected
    with pytest.raises(ValidationError):
        GoldenCase.model_validate(data)


def test_invalid_jsonl_has_line_number(tmp_path):
    path = tmp_path / "broken.jsonl"
    path.write_text('{"not": "a case"}', encoding="utf-8")
    with pytest.raises(ValueError, match="line 1"):
        load_cases(path)


def test_case_id_and_category_filters():
    cases = load_cases(DEFAULT_CASES)
    assert [case.case_id for case in select_cases(cases, case_id="SF01")] == ["SF01"]
    selected = select_cases(cases, category="fallback")
    assert len(selected) == 2 and all(case.category == "fallback" for case in selected)
    with pytest.raises(ValueError, match="No cases"):
        select_cases(cases, case_id="DOES_NOT_EXIST")


def test_failed_evaluator_retains_expected_actual_reason():
    result = IntentEvaluator().evaluate(case_fixture(), {"result": {"intent": "product_comparison"}})[0]
    assert not result.passed
    assert result.expected == "company_product_count"
    assert result.actual == "product_comparison"
    assert result.reason


def test_independent_filter_oracle_detects_relaxed_conditions():
    case = case_fixture().model_copy(update={"expected": case_fixture().expected.model_validate({"filter_truth": {"min_margin_rate": 0.3, "risk_level": "low", "compliance_status": "approved"}})})
    catalog = [{"id": "a", "title": "eligible", "price": 100, "margin": 0.4, "risk": "low", "compliance": "approved"}, {"id": "b", "title": "wrong risk", "price": 100, "margin": 0.4, "risk": "high", "compliance": "approved"}]
    result = {"products": [{"id": p["id"], "title": p["title"], "current_price": p["price"]} for p in catalog], "matched_count": 2, "total_count": 2, "displayed_count": 2, "filter_criteria": {"min_margin_rate": 0.3, "risk_level": "low", "compliance_status": "approved"}}
    assertions = FactGroundingEvaluator().evaluate(case, {"result": result, "catalog": catalog})
    assert not next(item for item in assertions if item.assertion == "filter_exact_ids").passed


def test_metrics_use_all_required_assertions_and_eligible_cases(tmp_path):
    first = IntentEvaluator().evaluate(case_fixture(), {"result": {"intent": "product_price"}})[0].model_dump()
    second = {**first, "passed": True, "assertion": "second assertion"}
    rows = [{"case_id": "T1", "category": "simple_fact", "query": "count", "passed": False, "assertions": [first, second]}]
    summary = summarize(rows)
    assert summary["overall_case_pass_rate"]["rate"] == 0
    assert summary["assertion_pass_rate"]["rate"] == 0.5
    assert summary["metrics"]["intent_accuracy"]["rate"] == 0
    assert "entity_resolution_accuracy" not in summary["metrics"]
    report = {"created_at": "test", "dataset_sha256": "mock-hash", "summary": summary, "cases": rows}
    json_path, md_path = write_reports(report, tmp_path)
    assert json.loads(json_path.read_text(encoding="utf-8"))["cases"][0]["passed"] is False
    text = md_path.read_text(encoding="utf-8")
    assert "Failed: 1" in text and "expected:" in text and "actual:" in text and "IntentEvaluator" in text


def test_runner_cases_are_fresh_and_count_does_not_accumulate():
    case = case_fixture()
    added = case.model_dump()
    added["setup"]["additions"] = [{"external_product_id": "isolation-extra", "title": "隔离测试台灯", "current_price": 250}]
    added["expected"]["required_facts"]["fact.value"] = 61
    first = run_case(GoldenCase.model_validate(added))
    second = run_case(case)
    third = run_case(case)
    assert first["passed"] and second["passed"] and third["passed"], [row.get("execution") for row in (first, second, third)]
    assert first["execution"]["result"]["fact"]["value"] == 61
    assert second["execution"]["result"]["fact"]["value"] == third["execution"]["result"]["fact"]["value"] == 60
    assert second["execution"]["result"]["run_id"] != third["execution"]["result"]["run_id"]


def test_worker_never_reads_dotenv_key_or_production_db(tmp_path, monkeypatch):
    sentinel = tmp_path / "production.sqlite"
    sentinel.write_bytes(b"do not change production")
    secret = "FAKE_SECRET_MUST_NOT_BE_READ"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{sentinel.as_posix()}")
    monkeypatch.setenv("ZHIPU_API_KEY", secret)
    script = r'''
import builtins, json, pathlib, sys
from unittest.mock import patch
from evals.fixtures import execute_isolated
from evals.schemas import GoldenCase
original_open=builtins.open
original_path_open=pathlib.Path.open
def guarded_open(file,*args,**kwargs):
    if str(file).endswith('.env'): raise AssertionError('dotenv read attempted')
    return original_open(file,*args,**kwargs)
def guarded_path_open(file,*args,**kwargs):
    if file.name=='.env': raise AssertionError('dotenv read attempted')
    return original_path_open(file,*args,**kwargs)
with patch('builtins.open',guarded_open),patch.object(pathlib.Path,'open',guarded_path_open):
    result=execute_isolated(GoldenCase.model_validate_json(sys.stdin.read()))
print(json.dumps(result))
'''
    run = subprocess.run([sys.executable, "-X", "utf8", "-c", script], input=case_fixture().model_dump_json(), text=True, encoding="utf-8", capture_output=True, cwd=BACKEND, timeout=60)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result["safety"]["real_key_loaded"] is False
    assert result["safety"]["database_url"] == "sqlite+aiosqlite:///:memory:"
    assert result["safety"]["blocked_http_attempts"] == 0
    assert secret not in run.stdout + run.stderr
    assert sentinel.read_bytes() == b"do not change production"
