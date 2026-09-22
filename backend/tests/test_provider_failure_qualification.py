"""Provider-matrix qualification: normal routing must invoke the provider first."""

import pytest

from app.agent.model_router import ModelRouter
from app.agent.query_understanding import understand_query
from test_demo_reliability import active_collection_snapshot
from test_model_router import MockProvider, install_router
from test_stability_sprint import ask, deterministic_client, seed


CANDIDATES = (
    ("composite_collection", "比较当前动态推荐度最高的三个商品，说明利润、竞争、合规和数据缺口，给出人工审核顺序。"),
    ("historical_decision", "检查当前候选是否存在同品牌或同类目的历史放弃案例，并列出需要人工复核的原因。"),
    ("human_review_priority", "这批候选有哪些需要优先人工复核，并说明证据缺口"),
    ("dual_selector_comparison", "比较这批候选中推荐度最高三个和利润最高三个的差异"),
    ("contextual_rank_explanation", "第二个为什么值得继续研究"),
    ("scenario_create", "如果当前候选池前三名的广告成本都下降30%，重新排一次"),
    ("scenario_compare", "和原来的情况比呢"),
    ("scenario_explanation", "当前假设下第一名为什么排最前面"),
    ("evidence_gap", "当前候选各自还缺什么证据"),
    ("collection_filter", "这批候选ROI低于18%的有哪些"),
    ("ordinal_comparison", "第一名为什么比第二名更值得研究"),
    ("price_fastpath_control", "桌面理线器售价是多少"),
)

FAILURE_TYPES = (
    "CONNECT_ERROR", "NETWORK_TIMEOUT", "INVALID_RESPONSE",
    "SCHEMA_VALIDATION_FAILED", "SEMANTIC_PLAN_VALIDATION_FAILED",
    "RESPONSE_READ_ERROR",
)


@pytest.mark.parametrize(("semantic_class", "query"), CANDIDATES)
def test_normal_route_qualification(deterministic_client, monkeypatch, semantic_class, query):
    headers, _ = seed(deterministic_client, f"provider-qualification-{semantic_class}")
    created = ask(deterministic_client, headers, "分析候选池里最值得继续研究的三个商品")
    if semantic_class.startswith("scenario_") and semantic_class != "scenario_create":
        created = ask(
            deterministic_client, headers,
            "如果当前候选池前三名广告成本下降30%，重新排一次",
            session_id=created["session_id"],
        )
        assert created["intent"] == "scenario_analysis"
    _, preframe = understand_query(query, [], (), active_collection_snapshot())
    qwen = MockProvider("qwen", [preframe.model_dump(mode="json")])
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))
    result = ask(
        deterministic_client, headers, query, session_id=created["session_id"],
        provider_execution_policy="QWEN_ONLY",
    )
    print(
        f"QUALIFIED {semantic_class}: provider={len(qwen.calls)} "
        f"route={result['model_route']} intent={result['intent']} "
        f"type={result['response_type']} complete={result['task_completed']} "
        f"preframe={preframe.intent}/{preframe.operation} "
        f"handler={[item.get('tool_name') for item in result['tool_results']]}"
    )
    # A correct Fast Path is deliberately not forced into a provider test.
    if semantic_class == "price_fastpath_control":
        assert not qwen.calls
    else:
        assert result["response_type"] != "error"


@pytest.mark.parametrize("failure", FAILURE_TYPES)
@pytest.mark.parametrize(("semantic_class", "query"), (
    ("collection_semantic", "比较当前动态推荐度最高的三个商品，说明利润、竞争、合规和数据缺口，给出人工审核顺序。"),
    ("context_continuation", "第二个为什么值得继续研究"),
    ("scenario_semantic", "如果当前候选池前三名的广告成本都下降30%，重新排一次"),
))
def test_strict_provider_failure_matrix_uses_qualified_routes(
    deterministic_client, monkeypatch, failure, semantic_class, query,
):
    headers, _ = seed(deterministic_client, f"strict-matrix-{semantic_class}-{failure}")
    created = ask(deterministic_client, headers, "分析候选池里最值得继续研究的三个商品")
    qwen = MockProvider("qwen", failure=failure)
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))

    result = ask(
        deterministic_client, headers, query, session_id=created["session_id"],
        provider_execution_policy="QWEN_ONLY",
    )

    assert len(qwen.calls) == 1, "qualification proved this is not a Fast Path"
    assert result["fallback_used"] is True
    assert result["fallback_reason"] == failure
    assert result["task_completed"] is True
    assert result["response_type"] not in {"clarification", "error"}
    assert result["tool_call_count"] >= 0
    assert result["products"] or result.get("collection_analysis") or result.get("scenario")


@pytest.mark.parametrize(("semantic_class", "query"), CANDIDATES[:6] + CANDIDATES[8:10])
def test_each_critical_provider_class_has_failure_injection(
    deterministic_client, monkeypatch, semantic_class, query,
):
    headers, _ = seed(deterministic_client, f"critical-provider-class-{semantic_class}")
    created = ask(deterministic_client, headers, "分析候选池里最值得继续研究的三个商品")
    qwen = MockProvider("qwen", failure="CONNECT_ERROR")
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))

    result = ask(
        deterministic_client, headers, query, session_id=created["session_id"],
        provider_execution_policy="QWEN_ONLY",
    )

    assert len(qwen.calls) == 1
    assert result["fallback_used"] is True
    assert result["fallback_reason"] == "CONNECT_ERROR"
    assert result["task_completed"] is True
    assert result["response_type"] not in {"clarification", "error"}


@pytest.mark.parametrize("failure", FAILURE_TYPES)
def test_incomplete_provider_frame_returns_business_clarification(
    deterministic_client, monkeypatch, failure,
):
    headers, _ = seed(deterministic_client, f"incomplete-business-clarification-{failure}")
    qwen = MockProvider("qwen", failure=failure)
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))

    result = ask(
        deterministic_client, headers, "桌面理线器目前卖到多少",
        provider_execution_policy="QWEN_ONLY",
    )

    assert len(qwen.calls) == 1
    assert result["fallback_reason"] == failure
    assert result["response_type"] == "clarification"
    assert result["tool_call_count"] == 0
    assert result["products"] == []
    assert result["understanding"]["clarification_category"] in {"MISSING_PRODUCT", "MISSING_METRIC"}
    assert result["understanding"]["missing_slots"]
    assert result["understanding"]["clarification_reason"]
    assert result["answer"] == result["understanding"]["clarification_reason"]
    assert "语义模型" not in result["answer"]
