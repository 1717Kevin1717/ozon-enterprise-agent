import asyncio
import json

import pytest

from app.agent.query_understanding import understand_query, semantic_fallback, validate_semantic_plan
from test_stability_sprint import deterministic_client, seed, ask, by_title
from test_stability_sprint_v3 import product, render_result


@pytest.mark.parametrize('suffix,intent', [
    ('这个价格是真实平台实时价格吗？', 'provenance_fact'),
    ('价格哪里来的？', 'provenance_fact'), ('这个价格是什么来源？', 'provenance_fact'),
    ('为什么不推荐？把依据告诉我。', 'decision_explanation'),
    ('为什么不能上？', 'decision_explanation'), ('主要卡在哪里？', 'decision_explanation'),
    ('的净利润率是怎么得出来的？', 'calculation_explanation'),
    ('利润率怎么算？', 'calculation_explanation'), ('利润的计算依据是什么？', 'calculation_explanation'),
    ('数据旧了吗？', 'data_quality_answer'), ('数据多久以前采的？', 'data_quality_answer'),
    ('的 -12.3% 销量增速是系统快照趋势吗？', 'provenance_fact'),
])
def test_generic_semantic_spans_and_paraphrases(suffix, intent):
    item = product('旅行充电器65W4件装')
    parsed, understood = understand_query(item.title + suffix, [item])
    assert parsed.name == intent
    assert understood.entity_mentions == [item.title]
    assert understood.route == 'DETERMINISTIC_FAST_PATH'
    if '%' in suffix:
        assert understood.metric_values == [-12.3]


def test_unknown_sentence_is_not_a_product():
    _, understood = understand_query('把经营表现的来龙去脉说清楚？', [product('旅行充电器')])
    assert understood.route == 'SEMANTIC_PLANNER' and not understood.entity_mentions


def semantic_mock(**updates):
    value = dict(intent='provenance_fact', question_type='source_verification', entity_mentions=['旅行充电器'], references=[], requested_dimensions=['price', 'provenance'], confidence=.9, route='SEMANTIC_PLANNER', planned_tools=['get_product'])
    return {**value, **updates}


@pytest.mark.parametrize('raw', ['not json', semantic_mock(intent='invented'), semantic_mock(planned_tools=['delete_all']), semantic_mock(entity_mentions=['invented-product']), semantic_mock(requested_dimensions=['recommendation']), {**semantic_mock(), 'price': 999}])
def test_semantic_plan_rejects_invalid_or_fabricated_output(raw):
    with pytest.raises(ValueError):
        validate_semantic_plan(raw, '旅行充电器价格的来龙去脉？')


def test_valid_plan_deduplicates_tools():
    _, result = validate_semantic_plan(semantic_mock(planned_tools=['get_product', 'get_product']), '旅行充电器价格的来龙去脉？')
    assert result.planned_tools == ['get_product'] and result.planner_calls == 1


@pytest.mark.parametrize('mode', ['timeout', 'malformed', 'unavailable'])
def test_planner_failure_is_clarification(mode):
    _, understood = understand_query('解释经营表现的来龙去脉？', [])
    async def planner(*args):
        if mode == 'timeout':
            raise TimeoutError('controlled')
        return '{broken'
    parsed, result = asyncio.run(semantic_fallback('解释经营表现的来龙去脉？', understood, None if mode == 'unavailable' else planner))
    assert parsed.name == 'unknown' and result.route == 'CLARIFICATION'
    assert result.planner_calls <= 1


@pytest.mark.parametrize('question,intent', [('的价格数据来自哪里？','provenance_fact'),('的净利润率是怎么得出来的？','calculation_explanation'),('为什么不推荐？','decision_explanation')])
def test_backend_and_renderer_are_compact(deterministic_client, question, intent):
    headers, items = seed(deterministic_client, 'intelligence-scope-'+intent)
    target = by_title(items, '智能温湿度计')
    result = ask(deterministic_client, headers, target['title']+question)
    assert result['intent'] == result['response_type'] == intent
    assert result['product_ids'] == [target['id']] and result['task_completed']
    html = render_result(result)
    assert '企业选品决策报告' not in html and '需求评分' not in html
    assert not result['human_review_required']
    if intent == 'provenance_fact':
        assert '不是实时 Ozon' in result['answer'] and 'mock_enterprise_catalog' in result['answer']
    if intent == 'calculation_explanation':
        assert all(label in result['answer'] for label in ('净利润', '净利率', '计算逻辑', '采购成本'))
        assert 'net_profit / price' not in result['answer'] and 'procurement_cost' not in result['answer']
        assert '采购成本' in html
        assert not result['warnings'] and not result['missing_data']


def test_reference_priority_and_new_session_isolation(deterministic_client):
    headers, items = seed(deterministic_client, 'intelligence-reference')
    first, second = items[:2]
    prior = ask(deterministic_client, headers, first['title']+'售价是多少？')
    result = ask(deterministic_client, headers, '这个价格靠谱吗？', session_id=prior['session_id'])
    assert result['product_ids'] == [first['id']] and result['selection_source'] == 'session_reference'
    selected = ask(deterministic_client, headers, '这个价格靠谱吗？', session_id=prior['session_id'], selected_product_ids=[second['id']])
    assert selected['product_ids'] == [second['id']] and selected['selection_source'] == 'explicit_selection'
    explicit = ask(deterministic_client, headers, first['title']+'这个价格真实吗？', session_id=prior['session_id'], selected_product_ids=[second['id']])
    assert explicit['product_ids'] == [first['id']]
    blank = ask(deterministic_client, headers, '这个价格靠谱吗？')
    assert blank['response_type'] == 'clarification' and blank['product_ids'] == []


def test_reported_metric_stays_distinct_from_snapshot(deterministic_client):
    headers, items = seed(deterministic_client, 'intelligence-trend')
    target = by_title(items, '智能温湿度计')
    result = ask(deterministic_client, headers, target['title']+'的 -9.6% 销量增速是系统快照趋势吗？')
    assert result['understanding']['entity_mentions'] == [target['title']]
    assert result['data_sufficiency']['snapshot_count'] == 1
    assert result['data_sufficiency']['observed_trend'] == 'INSUFFICIENT_DATA'
    assert '不是系统快照趋势' in render_result(result)


def test_freshness_plural_context_and_generic_quality_policy(deterministic_client):
    headers, items = seed(deterministic_client, 'intelligence-freshness')
    query = '哪些数据已经过期，哪些还是新鲜的？'
    blank = ask(deterministic_client, headers, query)
    assert blank['response_type'] == 'clarification' and blank['requested_entities'] == []
    selected = ask(deterministic_client, headers, query, selected_product_ids=[p['id'] for p in items[:2]])
    assert selected['response_type'] == 'data_quality_answer' and selected['matched_count'] == 2
    assert 'UNKNOWN' in selected['answer']
    policy = ask(deterministic_client, headers, '这个商品完整度100%，是不是可以直接上架？')
    assert policy['response_type'] == 'policy_answer' and policy['tool_call_count'] == 0


def test_backend_error_is_safe_not_fabricated(deterministic_client, monkeypatch):
    from app.agent import tools
    headers, items = seed(deterministic_client, 'intelligence-error')
    async def broken(*args):
        raise RuntimeError('private-db-traceback')
    monkeypatch.setattr(tools, 'get_product', broken)
    result = ask(deterministic_client, headers, items[0]['title']+'价格来自哪里？')
    assert result['response_type'] == 'error' and not result['task_completed']
    assert 'private-db-traceback' not in json.dumps(result)


@pytest.mark.parametrize('mode', ['valid', 'malformed', 'unknown_intent', 'illegal_tool', 'missing_entity', 'ambiguous', 'timeout', 'repeated_tool', 'unavailable'])
def test_semantic_planner_mock_full_path(deterministic_client, monkeypatch, mode):
    from app.api import router
    from app.agent import zhipu_agent
    headers, items = seed(deterministic_client, 'intelligence-mock-'+mode)
    target = by_title(items, '桌面理线器')
    mention = target['title']
    if mode == 'ambiguous':
        mention = '蓝牙音箱'
        from test_stability_sprint_v3 import create_candidate
        create_candidate(deterministic_client, headers, '户外蓝牙音箱', 'intelligence-speaker')
    query = f'请解释{mention}报价的来龙去脉？'
    calls = []
    async def planner(*args):
        calls.append(1)
        if mode == 'timeout':
            raise TimeoutError('private-provider-error')
        if mode == 'malformed':
            return 'broken json'
        value = semantic_mock(entity_mentions=[mention])
        if mode == 'unknown_intent': value['intent'] = 'invented'
        if mode == 'illegal_tool': value['planned_tools'] = ['delete_all']
        if mode == 'missing_entity': value['entity_mentions'] = []
        if mode == 'repeated_tool': value['planned_tools'] = ['get_product', 'get_product']
        return value
    monkeypatch.setattr(router.settings, 'llm_provider', 'zhipu')
    monkeypatch.setattr(zhipu_agent.settings, 'zhipu_api_key', '' if mode == 'unavailable' else 'mock-not-real')
    monkeypatch.setattr(zhipu_agent, '_semantic_plan', planner)
    result = ask(deterministic_client, headers, query, selected_product_ids=[target['id']])
    assert len(calls) == (0 if mode == 'unavailable' else 1)
    assert result['duplicate_tool_execution'] == 0
    assert 'private-provider-error' not in json.dumps(result)
    if mode in {'valid', 'repeated_tool'}:
        assert result['response_type'] == 'provenance_fact' and result['product_ids'] == [target['id']]
        assert result['tool_call_count'] == 1 and 'mock_enterprise_catalog' in result['answer']
    else:
        assert result['response_type'] == 'clarification' and result['tool_call_count'] == 0
        if mode == 'ambiguous':
            assert result['requested_entities'][0]['status'] == 'AMBIGUOUS'


def test_session_reference_is_user_scoped(deterministic_client):
    headers, items = seed(deterministic_client, 'intelligence-user-scope')
    prior = ask(deterministic_client, headers, items[0]['title']+'售价是多少？')
    other = {**headers, 'X-User-ID': 'another-user'}
    result = ask(deterministic_client, other, '这个价格靠谱吗？', session_id=prior['session_id'])
    assert result['response_type'] == 'clarification' and not result['product_ids']
    assert result['session_id'] != prior['session_id']


def test_reference_inherits_metric_and_audit_uses_final_evidence(deterministic_client):
    headers, items = seed(deterministic_client, 'intelligence-field-reference')
    target = by_title(items, '智能温湿度计')
    prior = ask(deterministic_client, headers, target['title']+'的净利润率怎么算？')
    result = ask(deterministic_client, headers, '这个指标靠谱吗？', session_id=prior['session_id'])
    assert result['understanding']['reference_field'] == 'net_margin'
    assert result['requested_dimensions'] == ['profit', 'provenance']
    fields = {field['field'] for row in result['evidence'] for field in row['fields']}
    assert fields == {'net_profit', 'net_margin'}
    explanation = ask(deterministic_client, headers, '它为什么不推荐？', session_id=prior['session_id'])
    trace = next(item for item in explanation['trace'] if item['event'] == 'evidence_bound')
    assert trace['evidence_ids'] == [field['evidence_id'] for row in explanation['evidence'] for field in row['fields']]
