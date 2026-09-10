import pytest

from app.agent.context import build_context_snapshot, resolve_context_reference
from app.agent.query_understanding import understand_query
from app.db.models import ConversationSession
from test_stability_sprint import ask, by_title, deterministic_client, seed


def _catalog(client, company):
    headers, items = seed(client, company)
    return headers, items, by_title(items, "桌面理线器"), by_title(items, "智能温湿度计")


@pytest.mark.parametrize("follow_up", ["那利润呢？", "利润方面呢？"])
def test_ctx01_ellipsis_inherits_last_explicit_entity(deterministic_client, follow_up):
    headers, _, cable, _ = _catalog(deterministic_client, "context-ellipsis-" + str(abs(hash(follow_up))))
    first = ask(deterministic_client, headers, cable["title"] + "多少钱？")
    result = ask(deterministic_client, headers, follow_up, session_id=first["session_id"])
    assert result["product_ids"] == [cable["id"]]
    assert result["context_source"] == "last_explicit_entity"
    assert result["requested_dimensions"] == ["profit", "roi"]


@pytest.mark.parametrize("follow_up", ["这个利润怎么算出来的？", "这个数怎么来的？", "利润依据是什么？"])
def test_ctx02_calculation_explanation_inherits_product(deterministic_client, follow_up):
    headers, _, cable, _ = _catalog(deterministic_client, "context-calc-" + str(abs(hash(follow_up))))
    first = ask(deterministic_client, headers, cable["title"] + "利润怎么样？")
    result = ask(deterministic_client, headers, follow_up, session_id=first["session_id"])
    assert result["product_ids"] == [cable["id"]]
    assert result["response_type"] == "calculation_explanation"
    assert result["context_source"] in {"last_explicit_entity", "last_resolved_entity"}


def test_ctx03_pronoun_cost_explanation(deterministic_client):
    headers, _, cable, _ = _catalog(deterministic_client, "context-pronoun-cost")
    first = ask(deterministic_client, headers, cable["title"] + "利润怎么样？")
    result = ask(deterministic_client, headers, "它主要亏在哪些成本上？", session_id=first["session_id"])
    assert result["product_ids"] == [cable["id"]]
    assert result["response_type"] == "calculation_explanation"


def test_ctx04_new_session_has_no_ellipsis_referent(deterministic_client):
    headers, _, _, _ = _catalog(deterministic_client, "context-empty-session")
    session_id = deterministic_client.post("/api/v1/agent/sessions", headers=headers, json={}).json()["data"]["id"]
    result = ask(deterministic_client, headers, "那利润呢？", session_id=session_id)
    assert result["response_type"] == "clarification"
    assert result["product_ids"] == []


def test_ctx05_explicit_replacement_inherits_price_dimension(deterministic_client):
    headers, _, cable, meter = _catalog(deterministic_client, "context-replace")
    first = ask(deterministic_client, headers, meter["title"] + "多少钱？")
    result = ask(deterministic_client, headers, "换成" + cable["title"] + "呢？", session_id=first["session_id"])
    assert result["product_ids"] == [cable["id"]]
    assert result["requested_dimensions"] == ["price"]
    assert result["response_type"] == "simple_fact"
    assert result["context_source"] == "explicit_query"


def test_ctx06_pronoun_switches_to_net_margin(deterministic_client):
    headers, _, cable, _ = _catalog(deterministic_client, "context-net-margin")
    first = ask(deterministic_client, headers, cable["title"] + "多少钱？")
    result = ask(deterministic_client, headers, "不是，我想看它的净利率。", session_id=first["session_id"])
    assert result["product_ids"] == [cable["id"]]
    assert result["requested_dimensions"] == ["profit", "roi"]


def test_ctx07_plural_reference_binds_current_ui_selection(deterministic_client):
    headers, items, _, _ = _catalog(deterministic_client, "context-ui-selection")
    selected = [item["id"] for item in items[:4]]
    result = ask(deterministic_client, headers, "这几个里面谁利润最好？", selected_product_ids=selected, selection_revision=1)
    assert result["context_source"] == "ui_selection"
    assert set(result["product_ids"]) == set(selected)
    assert result["understanding"]["entity_mentions"] == []


def test_ctx08_ordinal_uses_last_comparison_order(deterministic_client):
    headers, _, cable, meter = _catalog(deterministic_client, "context-ordinal")
    first = ask(deterministic_client, headers, f"对比{cable['title']}和{meter['title']}")
    result = ask(deterministic_client, headers, "第二个呢？为什么这么低？", session_id=first["session_id"])
    assert result["product_ids"] == [meter["id"]]
    assert result["context_source"] == "ordinal_reference"
    assert result["response_type"] == "decision_explanation"


def test_ctx09_explicit_entity_overrides_ui_selection(deterministic_client):
    headers, items, _, meter = _catalog(deterministic_client, "context-explicit-priority")
    selected = [item["id"] for item in items[:4] if item["id"] != meter["id"]]
    result = ask(deterministic_client, headers, "先别看这几个，" + meter["title"] + "多少钱？", selected_product_ids=selected)
    assert result["product_ids"] == [meter["id"]]
    assert result["context_source"] == "explicit_query"


def test_ctx10_updated_selection_is_the_only_comparison_scope(deterministic_client):
    headers, items, _, _ = _catalog(deterministic_client, "context-selection-update")
    selected = [item["id"] for item in items[2:4]]
    result = ask(deterministic_client, headers, "现在比较一下。", selected_product_ids=selected, selection_revision=2)
    assert result["response_type"] == "comparison_result"
    assert result["context_source"] == "ui_selection"
    assert result["product_ids"] == selected


@pytest.mark.parametrize("query", [
    "一个商品评分很高但合规失败，能不能先卖了再补审核？",
    "为什么完整度100%也不能说明这个数据靠谱？",
    "如果价格是三个月前采的，还能作为选品依据吗？",
])
def test_ctx11_policy_questions_need_no_product_entity(deterministic_client, query):
    headers, _, _, _ = _catalog(deterministic_client, "context-policy-" + str(abs(hash(query))))
    result = ask(deterministic_client, headers, query)
    assert result["response_type"] == "policy_answer"
    assert result["product_ids"] == []
    assert result["tool_call_count"] == 0


def test_ctx13_new_session_does_not_inherit_context_or_implicit_selection(deterministic_client):
    headers, items, cable, _ = _catalog(deterministic_client, "context-session-isolation")
    old = ask(deterministic_client, headers, cable["title"] + "多少钱？")
    selected = [item["id"] for item in items[:2]]
    new_session = deterministic_client.post("/api/v1/agent/sessions", headers=headers, json={}).json()["data"]["id"]
    result = ask(
        deterministic_client, headers, "这个可靠吗？", session_id=new_session,
        selected_product_ids=selected, selection_revision=1, selection_bound_session_id=old["session_id"],
    )
    assert result["response_type"] == "clarification"
    assert result["product_ids"] == []
    explicit = ask(
        deterministic_client, headers, "用对比中心这几个比较一下。", session_id=new_session,
        selected_product_ids=selected, selection_revision=1,
    )
    assert explicit["context_source"] == "ui_selection"
    assert explicit["product_ids"] == selected


def test_ctx20_single_choice_inherits_last_comparison_and_keeps_gate(deterministic_client):
    headers, _, cable, meter = _catalog(deterministic_client, "context-single-choice")
    first = ask(deterministic_client, headers, f"对比{cable['title']}和{meter['title']}")
    result = ask(deterministic_client, headers, "如果只能留一个呢？", session_id=first["session_id"])
    assert result["response_type"] == "decision_report"
    assert result["context_source"] == "last_comparison"
    assert result["product_ids"]
    assert result["decision_status"] != "NOT_APPLICABLE"
