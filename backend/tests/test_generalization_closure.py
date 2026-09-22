"""Semantic-class regression guards; no provider calls or market data."""

import pytest

from app.agent.query_understanding import understand_query
from app.agent.model_router import ModelRouter
from app.schemas.agent import ContextSnapshot, TaskState
from test_model_router import MockProvider, install_router
from test_demo_reliability import active_collection_snapshot
from test_stability_sprint import ask, deterministic_client, seed


@pytest.mark.parametrize(("query", "operation", "ordinals"), [
    ("目前榜首是谁", "LOOKUP_ORDINAL_MEMBER", [1]),
    ("排名第一的候选是谁", "LOOKUP_ORDINAL_MEMBER", [1]),
    ("Top1是哪款", "LOOKUP_ORDINAL_MEMBER", [1]),
    ("第一位为什么值得进一步验证", "EXPLAIN_RANKING", [1]),
    ("榜首依据什么排在前面", "EXPLAIN_RANKING", [1]),
    ("前两个哪个利润更好", "COMPARE_COLLECTION_MEMBERS", [1, 2]),
    ("Top1 vs Top2 差在哪", "EXPLAIN_RANKING", [1, 2]),
    ("把第一位和第二位对比", "COMPARE_COLLECTION_MEMBERS", [1, 2]),
])
def test_rank_reference_semantics(query, operation, ordinals):
    parsed, frame = understand_query(query, [], (), active_collection_snapshot())
    assert parsed.name == "collection_analysis"
    assert frame.operation == operation
    assert frame.ordinal_references == ordinals
    assert frame.entity_mentions == []
    assert frame.collection_reference == "current_collection"


@pytest.mark.parametrize("query", [
    "帮忙看这批候选中ROI未达目标的有哪些",
    "这批商品里投资回报率低于目标的是谁",
    "请列出当前集合里ROI不达目标的产品",
])
def test_missing_roi_target_requires_precise_clarification(query):
    parsed, frame = understand_query(query, [], (), active_collection_snapshot())
    assert parsed.name == "unknown"
    assert frame.route == "CLARIFICATION"
    assert frame.entity_mentions == []
    assert "ROI" in frame.clarification_reason
    assert "多少" in frame.clarification_reason


@pytest.mark.parametrize(("query", "operator", "value"), [
    ("这批候选中ROI低于18%的有哪些", "LT", 0.18),
    ("当前候选集合ROI不低于25%的商品", "GTE", 0.25),
    ("这批商品投资回报率超过40%的候选", "GT", 0.40),
])
def test_roi_numeric_filter_has_owned_metric_span(query, operator, value):
    parsed, frame = understand_query(query, [], (), active_collection_snapshot())
    assert parsed.name == "collection_analysis"
    assert frame.entity_mentions == []
    assert frame.collection_filters == [{
        "field": "roi", "operator": operator, "value": value,
        "reference_field": None, "reference_value_source": "QUERY",
    }]


def test_collection_ordinal_lookup_and_comparison_use_current_order(deterministic_client):
    headers, _ = seed(deterministic_client, "closure-ordinal")
    created = ask(deterministic_client, headers, "分析候选池里最值得继续研究的三个商品")
    ordered = created["task_state"]["active_collection"]["top_product_ids"]
    lookup = ask(deterministic_client, headers, "目前第一名是谁", session_id=created["session_id"])
    assert lookup["intent"] == "collection_analysis"
    assert lookup["product_ids"] == [ordered[0]]
    assert lookup["task_state"]["active_collection"]["top_product_ids"] == ordered
    compared = ask(deterministic_client, headers, "比较第一名与第二名", session_id=created["session_id"])
    assert compared["intent"] == "collection_analysis"
    assert set(compared["product_ids"]) == set(ordered[:2])
    assert compared["task_state"]["active_collection"]["top_product_ids"] == ordered


def test_collection_roi_target_does_not_execute_or_guess(deterministic_client):
    headers, _ = seed(deterministic_client, "closure-roi-target")
    created = ask(deterministic_client, headers, "分析候选池里最值得继续研究的三个商品")
    result = ask(deterministic_client, headers, "这批候选中ROI低于目标的有哪些", session_id=created["session_id"])
    assert result["response_type"] == "clarification"
    assert result["tool_call_count"] == 0
    assert result["understanding"]["entity_mentions"] == []
    assert "ROI" in result["answer"]


# Sixty previously unlisted paraphrases, grouped by semantic class rather than
# individual expected answers. They exercise frame identity, not HTTP status.
UNSEEN_LOOKUP = (
    "刚才榜首是哪款", "现在排第一位的是什么", "刚才第一名是哪件",
    "第二位是哪个", "当前榜首商品是谁", "最前面那款是谁",
    "排在第2位的是哪款", "目前Top1是哪一个", "第一位是哪件商品",
    "第二名对应哪款商品",
)
UNSEEN_EXPLANATION = (
    "第一名因何值得验证", "第一位为什么排这里", "榜首为何靠前",
    "Top1排序依据是什么", "第二名为什么落后", "排第2位的依据",
    "最前面的为什么值得研究", "第一名凭什么领先", "排名第一的原因",
    "第二位为何在这里",
)
UNSEEN_COMPARISON = (
    "第一名和第二名比一下", "Top1与Top2哪个好", "前两个哪个利润更好",
    "榜首跟第二名差在哪", "第一位为什么高过第二位", "前两名对比",
    "第一名与第二名ROI谁高", "第二名和第一名相比", "Top1 vs Top2",
    "把排第1位与第2位比较",
)
UNSEEN_FILTER = (
    "这批候选ROI低于12%的有哪些", "当前集合ROI高于21%的商品",
    "这批商品ROI不低于30%的款", "候选池中ROI超过16%的有哪些",
    "当前候选ROI未达25%的商品", "这批候选净利率未达到目标的商品",
    "当前集合中利润率低于目标的有哪些", "这批商品净利率没达目标的",
    "候选池净利润率不达标的有谁", "当前候选净利率不足目标的商品",
    "这批候选中高风险的商品", "当前集合里低风险的商品",
    "这批商品风险等级为中的有哪些", "候选池风险是高的商品",
    "当前候选中风险为低的款",
)
UNSEEN_AMBIGUITY = (
    "这批候选ROI不达目标的有谁", "当前集合投资回报率低于目标的款",
    "候选池里ROI未达目标的商品", "这批商品ROI高于目标的有哪些",
    "当前候选中投资回报率不达目标的", "这批候选里ROI小于目标的款",
    "候选池ROI超过目标的有哪些", "当前集合ROI未达到目标的商品",
    "这批商品投资回报率高于目标的是谁", "候选池的ROI低于目标有哪些",
)
UNSEEN_CONTRAST = (
    "桌面理线器当前价钱是多少", "智能温湿度计的售价多少",
    "对比桌面理线器和智能温湿度计的利润", "便携蓝牙音箱目前什么价位",
    "桌面理线器风险等级多少",
)


def test_sixty_unseen_semantic_class_generalization():
    assert sum(map(len, (UNSEEN_LOOKUP, UNSEEN_EXPLANATION, UNSEEN_COMPARISON,
                         UNSEEN_FILTER, UNSEEN_AMBIGUITY, UNSEEN_CONTRAST))) == 60
    snapshot = active_collection_snapshot()
    for query in UNSEEN_LOOKUP:
        parsed, frame = understand_query(query, [], (), snapshot)
        assert (parsed.name, frame.operation, frame.entity_mentions) == (
            "collection_analysis", "LOOKUP_ORDINAL_MEMBER", [],
        ), query
    for query in UNSEEN_EXPLANATION:
        parsed, frame = understand_query(query, [], (), snapshot)
        assert (parsed.name, frame.operation, frame.entity_mentions) == (
            "collection_analysis", "EXPLAIN_RANKING", [],
        ), query
    for query in UNSEEN_COMPARISON:
        parsed, frame = understand_query(query, [], (), snapshot)
        assert parsed.name == "collection_analysis" and len(frame.ordinal_references) == 2, query
        assert frame.entity_mentions == [], query
    for query in UNSEEN_FILTER:
        parsed, frame = understand_query(query, [], (), snapshot)
        assert parsed.name == "collection_analysis" and frame.collection_filters, query
        assert frame.entity_mentions == [], query
    for query in UNSEEN_AMBIGUITY:
        parsed, frame = understand_query(query, [], (), snapshot)
        assert parsed.name == "unknown" and frame.route == "CLARIFICATION", query
        assert frame.entity_mentions == [], query
    for query in UNSEEN_CONTRAST:
        parsed, frame = understand_query(query, [], (), snapshot)
        assert parsed.name != "collection_analysis", query


ADVERSARIAL_REFERENCES = (
    "呃，Top1是哪款", "刚才榜首呢", "嗯，第二名是谁",
    "现在排第一位的是啥", "排第一的那个商品是哪款",
)
ADVERSARIAL_EXPLANATIONS = (
    "Top1凭什么领先", "第二名为啥值得看", "榜首依据啥排的",
    "前两个对比下", "Top1和Top2比比",
)
ADVERSARIAL_THRESHOLDS = (
    "这批ROI低于目标的有吗", "候选池投资回报率不达目标的呢",
    "这批商品ROI超过目标的有谁", "当前集合ROI低于目标咋办",
    "候选池里ROI未达到目标的有哪些",
)
ADVERSARIAL_NUMERIC = (
    "这批商品ROI不到15%的有谁", "候选池ROI高于22%的呢",
    "当前集合ROI至少30%的有哪些", "这批候选ROI超过35%的商品",
    "候选池中ROI不高于18%的有几个",
)


def test_twenty_adversarial_natural_language_variants():
    assert sum(map(len, (ADVERSARIAL_REFERENCES, ADVERSARIAL_EXPLANATIONS,
                         ADVERSARIAL_THRESHOLDS, ADVERSARIAL_NUMERIC))) == 20
    snapshot = active_collection_snapshot()
    for query in ADVERSARIAL_REFERENCES:
        parsed, frame = understand_query(query, [], (), snapshot)
        assert parsed.name == "collection_analysis" and frame.operation == "LOOKUP_ORDINAL_MEMBER", query
        assert frame.entity_mentions == [], query
    for query in ADVERSARIAL_EXPLANATIONS:
        parsed, frame = understand_query(query, [], (), snapshot)
        assert parsed.name == "collection_analysis" and frame.operation in {
            "EXPLAIN_RANKING", "COMPARE_COLLECTION_MEMBERS",
        }, query
        assert frame.entity_mentions == [], query
    for query in ADVERSARIAL_THRESHOLDS:
        parsed, frame = understand_query(query, [], (), snapshot)
        assert parsed.name == "unknown" and frame.route == "CLARIFICATION", query
        assert frame.entity_mentions == [], query
    for query in ADVERSARIAL_NUMERIC:
        parsed, frame = understand_query(query, [], (), snapshot)
        assert parsed.name == "collection_analysis" and frame.collection_filters, query
        assert frame.entity_mentions == [], query


@pytest.mark.parametrize(("query", "reason_part"), [
    ("这批商品里ROI低于目标的有哪些", "ROI"),
    ("候选池投资回报率未达目标的有哪些", "ROI"),
    ("当前候选中ROI高于目标的是谁", "ROI"),
    ("这批商品ROI不达目标", "ROI"),
    ("当前集合ROI超过目标有哪些", "ROI"),
    ("利润不好的有哪些", "净利润"),
    ("请找利润不佳的商品", "净利润"),
    ("利润较差的是谁", "净利润"),
    ("哪些商品利润差劲", "净利润"),
    ("这批候选利润差的有哪些", "净利润"),
    ("第一名是谁", "排名"),
    ("目前第二名是哪款", "排名"),
    ("Top1为何靠前", "排名"),
    ("第一名与第二名比较", "排名"),
    ("最前面的商品是谁", "排名"),
])
def test_ambiguity_never_guesses_without_metric_or_session_context(query, reason_part):
    empty = ContextSnapshot(
        session_id="isolated", company_id="tenant", user_id="user",
        current_query=query, task_state=TaskState(),
    )
    parsed, frame = understand_query(query, [], (), empty)
    assert parsed.name == "unknown"
    assert frame.route == "CLARIFICATION"
    assert frame.entity_mentions == []
    assert reason_part in frame.clarification_reason


@pytest.mark.parametrize("query", [
    "这批商品净利率未达目标的有哪些",
    "候选池净利润率低于目标的是谁",
    "当前候选ROI低于20%的商品",
    "这批商品风险为低的有哪些",
    "当前候选利润最高的三个商品",
])
def test_explicit_metric_or_selector_does_not_trigger_ambiguity(query):
    parsed, frame = understand_query(query, [], (), active_collection_snapshot())
    assert parsed.name == "collection_analysis"
    assert frame.route != "CLARIFICATION"
    assert frame.entity_mentions == []


PROVIDER_FAILURES = (
    "CONNECT_ERROR", "NETWORK_TIMEOUT", "INVALID_RESPONSE",
    "SCHEMA_VALIDATION_FAILED", "SEMANTIC_PLAN_VALIDATION_FAILED",
    "RESPONSE_READ_ERROR",
)


@pytest.mark.parametrize("failure", PROVIDER_FAILURES)
@pytest.mark.parametrize("semantic_class", ("collection_filter", "scenario_create"))
def test_provider_failure_matrix_reuses_only_complete_safe_frames(
    deterministic_client, monkeypatch, failure, semantic_class,
):
    headers, _ = seed(deterministic_client, f"closure-matrix-{semantic_class}-{failure}")
    created = ask(deterministic_client, headers, "分析候选池里最值得继续研究的三个商品")
    qwen = MockProvider("qwen", failure=failure)
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))
    query = (
        "这批候选ROI低于18%的有哪些"
        if semantic_class == "collection_filter" else
        "如果当前候选池前三名的广告成本都下降30%，重新排一次"
    )
    result = ask(deterministic_client, headers, query, session_id=created["session_id"],
                 provider_execution_policy="QWEN_ONLY")
    assert len(qwen.calls) == 1
    assert result["fallback_used"] is True
    assert result["fallback_reason"] == failure
    assert result["task_completed"] is True
    assert result["response_type"] != "clarification"
    assert result["intent"] == ("collection_analysis" if semantic_class == "collection_filter" else "scenario_analysis")


@pytest.mark.parametrize("failure", PROVIDER_FAILURES)
@pytest.mark.parametrize("semantic_class", ("simple_fact", "context_continuation"))
def test_provider_failure_matrix_fact_and_context_do_not_guess(
    deterministic_client, monkeypatch, failure, semantic_class,
):
    headers, _ = seed(deterministic_client, f"closure-fact-matrix-{semantic_class}-{failure}")
    previous = None
    if semantic_class == "context_continuation":
        previous = ask(deterministic_client, headers, "桌面理线器售价是多少")
    qwen = MockProvider("qwen", failure=failure)
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))
    query = "桌面理线器目前卖到多少" if semantic_class == "simple_fact" else "这款现在什么价位"
    result = ask(deterministic_client, headers, query,
                 session_id=previous["session_id"] if previous else None,
                 provider_execution_policy="QWEN_ONLY")
    assert len(qwen.calls) == 1
    assert result["fallback_reason"] == failure
    assert result["response_type"] != "error"
    if semantic_class == "simple_fact":
        # The local frame does not yet identify this colloquial price task;
        # preserve the established technical-failure taxonomy, not a fake
        # user clarification or an invented product fact.
        assert result["response_type"] == "clarification"
        assert result["tool_call_count"] == 0
        assert result["clarification_code"] is None
        assert result["products"] == []
    else:
        assert result["intent"] in {"product_price", "product_detail"}
        assert result["products"]


@pytest.mark.parametrize(("semantic_class", "query", "expected_intent"), [
    ("collection_selector", "这批候选里净利润最高三个", "collection_analysis"),
    ("composite_analysis", "这批候选中利润最高三个并说明证据缺口", "collection_analysis"),
    ("historical_decision", "这批候选有没有类似历史放弃商品", "collection_analysis"),
    ("human_review_priority", "这批候选哪些应该优先人工复核", "collection_analysis"),
    ("ordinal_lookup", "现在第一名是谁", "collection_analysis"),
    ("ordinal_comparison", "第一名和第二名比较", "collection_analysis"),
    ("scenario_followup", "现在第一名是谁", "scenario_analysis"),
    ("scenario_compare", "和原来的情况比呢", "scenario_analysis"),
])
def test_provider_failure_matrix_class_coverage(
    deterministic_client, monkeypatch, semantic_class, query, expected_intent,
):
    headers, _ = seed(deterministic_client, f"closure-class-{semantic_class}")
    created = ask(deterministic_client, headers, "分析候选池里最值得继续研究的三个商品")
    if semantic_class.startswith("scenario_"):
        created = ask(
            deterministic_client, headers,
            "如果当前候选池前三名广告成本下降30%，重新排一次",
            session_id=created["session_id"],
        )
        assert created["intent"] == "scenario_analysis"
    qwen = MockProvider("qwen", failure="CONNECT_ERROR")
    install_router(monkeypatch, ModelRouter([qwen], primary_name="qwen", reasoning_name="deepseek"))
    result = ask(deterministic_client, headers, query, session_id=created["session_id"],
                 provider_execution_policy="QWEN_ONLY")
    assert result["intent"] == expected_intent
    assert result["task_completed"] is True
    assert result["response_type"] != "error"
    if qwen.calls:
        assert len(qwen.calls) == 1
        assert result["fallback_reason"] == "CONNECT_ERROR"
    else:
        # High-confidence rule routes require no provider; an injected outage
        # cannot interrupt them or become a fabricated tool result.
        assert result["fallback_reason"] is None


NEGATIVE_INTENT_CONTRASTS = (
    ("桌面理线器售价是多少", "product_price"),
    ("智能温湿度计目前卖多少钱", "product_price"),
    ("便携蓝牙音箱现在什么价位", "product_price"),
    ("桌面理线器现在的价格多少", "product_price"),
    ("智能温湿度计售价多少", "product_price"),
    ("比较桌面理线器和智能温湿度计的利润", "profit_comparison"),
    ("对比桌面理线器与便携蓝牙音箱的利润", "profit_comparison"),
    ("桌面理线器和智能温湿度计只比较净利润", "profit_comparison"),
    ("比较智能温湿度计和桌面理线器的 ROI", "profit_comparison"),
    ("桌面理线器与便携蓝牙音箱只看利润", "profit_comparison"),
    ("第一名是否就代表正式推荐上架", "recommendation_policy"),
    ("排名第一是不是表示合规可以上架", "recommendation_policy"),
    ("第一名意味着一定推荐吗", "recommendation_policy"),
    ("分数高是不是等于最终决策通过", "recommendation_policy"),
    ("相对排名最高能否绕过人工审核", "recommendation_policy"),
    ("找净利率高于30%的商品", "product_filter"),
    ("筛出低风险且合规通过的商品", "product_filter"),
    ("从公司商品库找利润率超过20%的商品", "product_filter"),
    ("筛选风险低的企业商品", "product_filter"),
    ("找出评分达到60分以上的商品", "product_filter"),
    ("假设桌面理线器售价增加10%", "scenario_analysis"),
    ("如果智能温湿度计广告成本下降20%", "scenario_analysis"),
    ("假如桌面理线器采购成本减少5%", "scenario_analysis"),
    ("假设便携蓝牙音箱平台费用减半", "scenario_analysis"),
    ("如果当前候选池前三名广告成本下降30%", "scenario_analysis"),
    ("候选池中推荐度最高三个商品", "collection_analysis"),
    ("这批候选中净利润最低的两个", "collection_analysis"),
    ("当前集合风险最低的三个商品", "collection_analysis"),
    ("这批商品中谁缺少竞争证据", "collection_analysis"),
    ("候选池哪些需要优先人工复核", "collection_analysis"),
)


@pytest.mark.parametrize(("query", "expected_intent"), NEGATIVE_INTENT_CONTRASTS)
def test_thirty_negative_intent_contrasts(query, expected_intent):
    parsed, frame = understand_query(query, [], (), active_collection_snapshot())
    assert parsed.name == expected_intent
    if expected_intent in {"recommendation_policy", "profit_comparison", "product_price"}:
        assert frame.operation != "LOOKUP_ORDINAL_MEMBER"
