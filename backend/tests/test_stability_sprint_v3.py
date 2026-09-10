"""Entity semantics/scope regression with synthetic, tenant-isolated data; no live LLM."""

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent.entity_resolution import resolve_entity, resolve_query_entities
from app.agent.intent_engine import parse_intent
from app.agent import zhipu_agent as zhipu_module
from app.schemas.agent import EntityResolutionResult
from pydantic import ValidationError
from test_stability_sprint import ask, by_title, deterministic_client, seed


def product(title, product_id="p-primary", external_id="catalog-372"):
    return SimpleNamespace(id=product_id, title=title, external_product_id=external_id, sku=f"sku-{product_id}")


@pytest.mark.parametrize("title,mention,status", [
    ("便携温湿度计", "不存在的量子餐桌", "NOT_FOUND"),
    ("便携温湿度计", "便携温湿度计", "EXACT_MATCH"),
    ("便携温湿度计", "温湿度计", "UNIQUE_ALIAS_MATCH"),
    ("桌面理线器", "理线器", "UNIQUE_ALIAS_MATCH"),
    ("USB 桌面风扇 3件装", "ｕｓｂ桌面风扇３件装", "NORMALIZED_MATCH"),
    ("便携温湿度计", "便携温湿度汁", "FUZZY_UNIQUE_MATCH"),
    ("智能温湿度计", "智能温度湿度计", "FUZZY_UNIQUE_MATCH"),
    ("桌面文件收纳架", "桌面收纳架", "LOW_CONFIDENCE"),
    ("桌面文件收纳架", "桌面那个", "LOW_CONFIDENCE"),
    ("硅胶空气炸锅垫", "火星空气炸锅", "NOT_FOUND"),
    ("手机 Model 19", "手机 Model 20", "NOT_FOUND"),
], ids=["entity_not_found", "entity_exact", "entity_alias_unique", "short_alias", "entity_normalized", "entity_fuzzy_unique", "expanded_normalization", "entity_low_confidence", "underspecified", "accessory_not_parent", "model_number_not_typo"])
def test_entity_result_semantics(title, mention, status):
    item = product(title)
    result = resolve_entity([item], mention)
    assert result.status == status
    assert (result.product_id == item.id) == result.resolved
    if not result.resolved:
        assert result.product_id is None


def test_entity_ambiguous_candidates_are_bounded_and_exact_wins():
    items = [product(f"系列{index}蓝牙音箱", f"p-{index}", f"external-{index}") for index in range(7)]
    result = resolve_entity(items, "蓝牙音箱")
    assert result.status == "AMBIGUOUS"
    assert len(result.candidates) == 5
    exact = resolve_entity(items, items[3].title)
    assert exact.status == "EXACT_MATCH" and exact.product_id == items[3].id


def test_resolution_contract_rejects_contradictory_identity():
    with pytest.raises(ValidationError):
        EntityResolutionResult(mention="查询", status="NOT_FOUND", product_id="wrong-id")
    with pytest.raises(ValidationError):
        EntityResolutionResult(mention="查询", status="AMBIGUOUS", candidates=[])


def test_exact_name_with_query_grammar_words_is_atomic():
    target = product("茶和咖啡价格标签套装")
    resolution = resolve_query_entities([target], f"{target.title}多少钱？")
    assert not resolution.has_unresolved
    assert [item.id for item in resolution.products] == [target.id]


@pytest.mark.parametrize("identity", ["p-primary", "catalog-372", "sku-p-primary"])
def test_identifiers_are_exact_and_not_special_demo_ids(identity):
    result = resolve_entity([product("商务台灯")], identity)
    assert result.status == "EXACT_MATCH"
    assert result.product_id == "p-primary"


@pytest.mark.parametrize("ending", ["的利润", "，只比较利润和ROI", "的ROI", "，只比较利润，不要分析竞争、趋势和合规"])
def test_profit_comparison_scope_without_magic_question(ending):
    names = ["便携温湿度计", "桌面理线器"]
    query = f"比较{names[0]}和{names[1]}{ending}。"
    parsed = parse_intent(query)
    resolved = resolve_query_entities([product(name, f"p-{i}") for i, name in enumerate(names)], query)
    assert parsed.name == "profit_comparison"
    assert parsed.policy.requested_dimensions == ("profit", "roi")
    assert set(parsed.policy.allowed_tools) == {"compare_products", "calculate_profit"}
    assert len(resolved.products) == 2 and not resolved.has_unresolved


def test_full_dimensions_require_explicit_full_scope():
    assert parse_intent("全面比较商品甲和商品乙").policy.requested_dimensions == ("profit", "demand", "competition", "compliance", "risk")
    assert parse_intent("比较商品甲和商品乙").policy.requested_dimensions == ("identity", "price")


@pytest.mark.parametrize("unknown,expected", [("未登记的量子餐桌", "NOT_FOUND"), ("蓝牙音箱", "AMBIGUOUS")])
def test_partial_multi_entity_preserves_each_outcome(unknown, expected):
    items = [product("便携温湿度计"), product("便携蓝牙音箱", "p-2"), product("桌面蓝牙音箱", "p-3")]
    result = resolve_query_entities(items, f"比较{items[0].title}和{unknown}的利润")
    assert [item.status for item in result.requested_entities] == ["EXACT_MATCH", expected]
    assert [item.id for item in result.products] == [items[0].id]
    assert result.has_unresolved


def create_candidate(client, headers, title, external_id):
    response = client.post("/api/v1/products", headers=headers, json={"external_product_id": external_id, "title": title, "current_price": 273})
    assert response.status_code == 201, response.text
    return response.json()["data"]


@pytest.mark.parametrize("mode", ["NOT_FOUND", "AMBIGUOUS", "LOW_CONFIDENCE"])
def test_partial_comparison_blocks_tools_and_explains_known_entity(deterministic_client, mode):
    headers, items = seed(deterministic_client, f"v3-partial-{mode}")
    known = by_title(items, "智能温湿度计")
    if mode == "NOT_FOUND":
        mention = "未登记的量子餐桌"
    elif mode == "AMBIGUOUS":
        mention = "蓝牙音箱"
        create_candidate(deterministic_client, headers, "室外防水蓝牙音箱", "v3-speaker-other")
    else:
        mention = "桌面收纳架"
    result = ask(deterministic_client, headers, f"比较{known['title']}和{mention}的利润", selected_product_ids=[p["id"] for p in items[:4]])
    assert [item["status"] for item in result["requested_entities"]] == ["EXACT_MATCH", mode]
    assert result["entities"][0]["product_id"] == known["id"]
    assert known["title"] in result["answer"] and mention in result["answer"]
    assert "未执行商品比较" in result["answer"]
    assert result["products"] == [] and result["matched_count"] == 0
    assert result["tool_call_count"] == 0 and not result["task_completed"]
    if mode == "AMBIGUOUS":
        assert result["clarification_code"] == "ENTITY_AMBIGUOUS"
    elif mode == "LOW_CONFIDENCE":
        assert result["clarification_code"] == "ENTITY_LOW_CONFIDENCE"
    else:
        assert result["clarification_code"] is None
    assert any(row["event"] == "entity_resolution_blocked" for row in result["trace"])


def test_no_partial_comparison_even_with_two_resolved_entities(deterministic_client):
    headers, items = seed(deterministic_client, "v3-three-entities")
    names = [items[0]["title"], items[1]["title"], "未登记的量子餐桌"]
    result = ask(deterministic_client, headers, f"比较{'和'.join(names)}的利润")
    assert len(result["entities"]) == 2 and len(result["requested_entities"]) == 3
    assert not result["task_completed"] and result["tool_call_count"] == 0


def test_selected_state_does_not_override_explicit_unknown_entity(deterministic_client):
    headers, items = seed(deterministic_client, "v3-state")
    previous = ask(deterministic_client, headers, f"{items[0]['title']}售价是多少？")
    result = ask(deterministic_client, headers, "不存在的量子餐桌多少钱？", selected_product_ids=[items[0]["id"]], session_id=previous["session_id"])
    assert result["response_type"] == "not_found"
    assert result["selection_source"] == "none" and result["entities"] == []
    assert result["products"] == [] and result["tool_call_count"] == 0
    assert result["clarification_code"] is None
    assert "未找到" in result["answer"]


def test_selected_count_does_not_truncate_explicit_entities(deterministic_client):
    headers, items = seed(deterministic_client, "v3-explicit-three")
    targets = items[:3]
    result = ask(deterministic_client, headers, f"比较{'和'.join(item['title'] for item in targets)}的利润", selected_product_ids=[item["id"] for item in targets[:2]])
    assert set(result["product_ids"]) == {item["id"] for item in targets}
    assert result["tool_call_count"] == 4


def test_selection_reference_simulation_is_not_a_false_missing_entity(deterministic_client):
    headers, items = seed(deterministic_client, "v3-reference-simulation")
    result = ask(deterministic_client, headers, "如果售价调整到 139 RUB，净利润和风险如何变化？", selected_product_ids=[items[0]["id"]])
    assert result["simulation"]["proposed_price"] == 139
    assert result["task_completed"] and result["tool_call_count"] == 2


def test_unresolved_comparison_never_calls_provider(deterministic_client, monkeypatch):
    from app.api import router as router_module

    headers, items = seed(deterministic_client, "v3-no-provider")
    monkeypatch.setattr(router_module.settings, "llm_provider", "zhipu")
    monkeypatch.setattr(zhipu_module.settings, "zhipu_api_key", "mock-never-sent")
    def forbidden_client(*args, **kwargs):
        pytest.fail("Unresolved identities must not reach the model")
    monkeypatch.setattr(zhipu_module.httpx, "AsyncClient", forbidden_client)
    result = ask(deterministic_client, headers, f"比较{items[0]['title']}和未登记的量子餐桌的利润")
    assert result["response_mode"] == "rule_engine" and result["tool_call_count"] == 0


def test_entity_resolution_is_tenant_scoped(deterministic_client):
    headers_a, _ = seed(deterministic_client, "v3-tenant-a")
    headers_b, _ = seed(deterministic_client, "v3-tenant-b")
    secret = create_candidate(deterministic_client, headers_a, "专属紫晶控制台", "tenant-only-912")
    for identity in (secret["title"], secret["id"], secret["external_product_id"]):
        result = ask(deterministic_client, headers_b, f"{identity}售价是多少？")
        assert result["response_type"] == "not_found"
        assert result["requested_entities"][0]["candidates"] == []
        assert result["products"] == []


def render_result(result):
    node = shutil.which("node")
    assert node, "Node is required for actual renderer regression (no silent skip)"
    source_path = Path(__file__).parents[1] / "app" / "web" / "app.js"
    # Execute the actual result-rendering functions, with browser-only dependencies stubbed.
    script = r"""
const fs=require('node:fs'),vm=require('node:vm');
const data=JSON.parse(fs.readFileSync(0,'utf8')),source=fs.readFileSync(process.argv[1],'utf8'),output={innerHTML:''};
const esc=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const context={$:()=>output,esc,clean:x=>x,apiDate:x=>new Date(x),money:x=>String(x),percent:x=>String(x),score:x=>String(x),safeUrl:x=>x||'',thumb:()=>'',gradeClass:()=>'',riskBadge:()=>'',decisionBadge:x=>esc(x),riskNames:{low:'低风险',medium:'中风险',high:'高风险'},toolBusinessNames:{},listItems:items=>items.map(x=>`<li>${esc(x)}</li>`).join('')};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('function agentProductMetrics('),source.indexOf('async function askAgent(')),context);
context.renderAgentResult(data);process.stdout.write(output.innerHTML);
"""
    run = subprocess.run([node, "-e", script, str(source_path)], input=json.dumps(result), text=True, encoding="utf-8", capture_output=True, check=True)
    return run.stdout


@pytest.mark.parametrize("dimension,question", [("price", "售价是多少"), ("risk", "风险等级是多少")])
def test_simple_fact_scope_in_backend_and_actual_renderer(deterministic_client, dimension, question):
    headers, items = seed(deterministic_client, f"v3-fact-{dimension}")
    target = by_title(items, "桌面理线器")
    result = ask(deterministic_client, headers, f"{target['title']}{question}？")
    assert result["response_type"] == "simple_fact" and result["requested_dimensions"] == [dimension]
    assert result["tool_call_count"] == 1 and result["decision_status"] == "NOT_APPLICABLE"
    assert not result["human_review_required"] and not result["warnings"] and not result["missing_data"]
    assert result["fact"]["source"] and result["fact"]["product_id"] == target["id"]
    html = render_result(result)
    for unwanted in ("推荐度", "净利润", "销量趋势", "企业选品决策报告", "人工审核", "NOT_RECOMMENDED"):
        assert unwanted not in html
    assert ("风险等级" if dimension == "risk" else "售价") in html


def test_profit_comparison_actual_renderer_and_tool_scope(deterministic_client):
    headers, items = seed(deterministic_client, "v3-profit-scope")
    targets = [by_title(items, name) for name in ("智能温湿度计", "桌面理线器")]
    result = ask(deterministic_client, headers, f"比较{targets[0]['title']}和{targets[1]['title']}的利润。")
    assert result["requested_dimensions"] == ["profit", "roi"]
    assert result["response_type"] == "comparison_result" and result["tool_call_count"] == 3
    assert set(result["product_ids"]) == {item["id"] for item in targets}
    assert {item["tool_name"] for item in result["tool_results"]} == {"compare_products", "calculate_profit"}
    html = render_result(result)
    assert "ROI" in html and "净利润" in html
    for unwanted in ("需求评分", "竞争评分", "销量趋势", "推荐度", "风险等级"):
        assert unwanted not in html


@pytest.mark.parametrize("status,label", [("NOT_FOUND", "未找到指定商品"), ("AMBIGUOUS", "多个可能商品"), ("LOW_CONFIDENCE", "置信度不足")])
def test_clarification_renderer_consumes_typed_candidates(status, label):
    result = {"response_type": "not_found" if status == "NOT_FOUND" else "clarification", "answer": "后端解析结果", "requested_entities": [{"mention": "查询名称", "status": status, "candidates": [] if status == "NOT_FOUND" else [{"product_id": "id-abc", "name": "<不可信商品名>"}]}]}
    html = render_result(result)
    assert label in html
    assert "<不可信商品名>" not in html
    if status != "NOT_FOUND":
        assert "&lt;不可信商品名&gt;" in html and "id-abc" in html


def test_generic_comparison_renderer_does_not_invent_full_scope():
    result = {"response_type": "comparison_result", "answer": "基础对比", "requested_dimensions": ["identity", "price"], "display_scope": ["products"], "products": [{"id": "p-a", "title": "独立测试商品", "current_price": 381, "currency": "RUB", "decision_status": "REVIEW_REQUIRED"}]}
    html = render_result(result)
    assert "381 RUB" in html
    for unwanted in ("推荐度", "净利润", "需求评分", "竞争评分", "风险等级"):
        assert unwanted not in html


def test_act3_deterministic_replay(deterministic_client):
    """Replay the supplied cases against test-only seeded data, not the live database."""
    headers, products = seed(deterministic_client, "v3-act3-replay")
    selected = [item["id"] for item in products[:4]]
    unknown = "火星牌量子空气炸锅"
    price_mentions = {
        1: unknown, 2: "苹果 iPhone 20 Ultra", 3: "温湿度计", 4: "理线器",
        5: "智能温度湿度计", 6: "蓝牙音箱", 7: "充电器", 8: "65W 氮化镓充电器 5件装",
        9: "桌面理线器", 10: unknown, 11: "火星空气炸锅", 12: "桌面收纳架",
        17: "那个电子产品", 18: "智能温湿度汁", 19: "桌面那个", 20: by_title(products, "桌面理线器")["external_product_id"],
    }
    queries = {index: f"{mention}多少钱？" for index, mention in price_mentions.items()}
    queries[13] = "桌面理线器" + "风险等级是多少？"
    for index, second in ((14, "桌面理线器"), (15, unknown), (16, "蓝牙音箱")):
        queries[index] = f"比较智能温湿度计和{second}的利润。"
    rows = []
    for index in sorted(queries):
        result = ask(deterministic_client, headers, queries[index], selected_product_ids=selected if index in {9, 10} else [])
        statuses = [item["status"] for item in result["requested_entities"]]
        if index in {1, 2, 10, 11, 15}:
            assert "NOT_FOUND" in statuses and result["tool_call_count"] == 0
        elif index in {3, 4, 5, 6, 7, 8, 9, 13, 18, 20}:
            assert result["response_type"] == "simple_fact" and result["tool_call_count"] == 1
        elif index in {14, 16}:
            assert result["response_type"] == "comparison_result" and result["requested_dimensions"] == ["profit", "roi"]
        elif index in {12, 19}:
            assert "LOW_CONFIDENCE" in statuses and result["tool_call_count"] == 0
        elif index == 17:
            assert result["tool_call_count"] == 0 and not result["task_completed"]
        rows.append({"case": f"C{index}", "status": statuses, "type": result["response_type"], "tools": result["tool_call_count"], "titles": [p["title"] for p in result["products"]]})
    print("ACT3_REPLAY=" + json.dumps(rows, ensure_ascii=False))
