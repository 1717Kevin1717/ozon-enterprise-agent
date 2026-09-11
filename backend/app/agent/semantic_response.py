"""Compact projections of validated tools and existing evidence; no recalculation."""
from app.agent.query_understanding import SEMANTIC_TYPES
from app.schemas.provenance import FieldEvidence
from app.services.provenance import evidence_notices, scoped_fields


COST_LABELS = {
    "procurement_cost": "采购成本",
    "shipping_cost": "配送费",
    "fulfillment_cost": "履约费",
    "platform_commission": "平台佣金",
    "platform_fee": "平台服务费",
    "advertising_cost": "广告费",
    "warehousing_cost": "仓储费",
    "tax_cost": "税费",
    "return_loss_reserve": "退货损失准备",
    "other_cost": "其他成本",
}


def _money(value) -> str:
    return "未知" if value is None else f"{float(value):.2f} RUB"


def _rate(value) -> str:
    return f"{float(value or 0):.2%}"


def _business_cost_items(calculation: dict, *, limit: int | None = None) -> list[dict]:
    items = [item for item in calculation.get("cost_breakdown", []) if float(item.get("amount") or 0) > 0]
    return items if limit is None else items[:limit]


def _cost_item_text(item: dict) -> str:
    label = COST_LABELS.get(item.get("field"), "其他成本")
    share = item.get("share_of_price")
    return f"{label} {_money(item.get('amount'))}" + (f"（占售价 {_rate(share)}）" if share is not None else "")


def _evidence_gap_text(calculation: dict) -> str:
    gaps = [COST_LABELS.get(field, field) for field in calculation.get("input_gaps", [])]
    if not gaps:
        return ""
    return f"\n数据提示：{ '、'.join(gaps) }尚无可验证输入；现有计算按后端已披露的默认规则处理，正式决策前需补齐。"


def _profit_answer(title: str, analysis: dict) -> str:
    calculation = analysis.get("calculation_evidence") or {}
    net_profit = calculation.get("result", analysis.get("net_profit"))
    net_margin = calculation.get("net_margin", analysis.get("net_margin"))
    roi = calculation.get("roi", analysis.get("roi"))
    top = _business_cost_items(calculation, limit=3)
    pressure = "、".join(COST_LABELS.get(item.get("field"), "其他成本") for item in top)
    text = f"{title}当前单件净利润约 {_money(net_profit)}，净利率约 {_rate(net_margin)}，ROI 约 {_rate(roi)}。"
    if pressure:
        text += f" 当前成本压力主要来自{pressure}。"
    return text + _evidence_gap_text(calculation)


def _calculation_answer(title: str, analysis: dict) -> str:
    calculation = analysis.get("calculation_evidence") or {}
    required = ("sale_price", "total_cost", "result", "roi")
    if any(calculation.get(field) is None for field in required):
        return f"{title}本轮没有取得完整的后端计算依据，因此不展示可能误导的 0 值；请重新执行利润计算。"
    items = _business_cost_items(calculation)
    lines = [
        f"{title}当前单件净利润约 {_money(calculation.get('result', analysis.get('net_profit')))}，净利率约 {_rate(calculation.get('net_margin', analysis.get('net_margin')))}。",
        f"计算逻辑：售价 {_money(calculation.get('sale_price'))} - 成本合计 {_money(calculation.get('total_cost'))} = 净利润 {_money(calculation.get('result'))}。",
        f"ROI 计算：净利润 {_money(calculation.get('result'))} ÷ 成本合计 {_money(calculation.get('total_cost'))} = {_rate(calculation.get('roi'))}。",
    ]
    if items:
        lines.append("实际计入的成本：" + "；".join(_cost_item_text(item) for item in items) + "。")
    return "\n".join(lines) + _evidence_gap_text(calculation)


def _cost_breakdown_answer(title: str, analysis: dict) -> str:
    calculation = analysis.get("calculation_evidence") or {}
    items = _business_cost_items(calculation, limit=3)
    if not items:
        return f"{title}目前没有足够的后端成本输入，暂时无法判断主要成本压力。" + _evidence_gap_text(calculation)
    lines = [f"{title}目前成本压力主要来自以下几项："]
    lines.extend(f"{index}. {_cost_item_text(item)}" for index, item in enumerate(items, 1))
    leaders = "、".join(COST_LABELS.get(item.get("field"), "其他成本") for item in items[:3])
    lines.append(f"简要结论：{leaders}是当前利润空间最主要的压力来源；排序完全来自本轮后端成本数据。")
    return "\n".join(lines) + _evidence_gap_text(calculation)


def scope_semantic_response(payload, views, understanding, entity_blocked):
    intent = understanding.intent
    if intent not in SEMANTIC_TYPES | {"unknown", "product_detail"}:
        return
    if intent == "selection_recommendation":
        # Candidate discovery and the recommendation gate already produced a
        # complete deterministic decision report.  A semantic interpretation
        # must not replace that report merely because no single-product view
        # was materialized for a catalogue-wide filter.
        return
    if understanding.operation in {"ARGMAX", "ARGMIN", "EXPLAIN_RANKING"}:
        return
    if intent == "product_detail" and not ({"profit", "roi"} & set(understanding.requested_dimensions)):
        # Risk facts and snapshot sufficiency already have narrower, validated
        # response contracts in the deterministic backend renderer.  The
        # semantic renderer is only needed for conversational profit follow-ups.
        return
    if entity_blocked:
        return
    reset = {"warnings": [], "risks": [], "missing_data": [], "next_actions": [], "evidence": [], "trust_notices": []}
    if intent != "decision_explanation":
        reset.update(decision_summary={}, decision_status="NOT_APPLICABLE", human_review_required=False, requires_human_review=False)
    payload.update(**reset)
    payload["display_scope"] = ["answer", "evidence", "source", "tool_summary", *understanding.requested_dimensions]
    if intent == "unknown":
        payload.update(response_type="clarification", task_completed=False,
                       answer=understanding.clarification_reason or "我还不能确定这个问题的意图。请说明要查询的商品，以及价格、来源、计算依据或推荐原因等具体内容。")
    elif intent == "data_quality_policy":
        payload.update(response_type="policy_answer", task_completed=True,
            answer="不能。数据完整度（Completeness）只说明字段是否填写，不等于证据充分性（Sufficiency）、时效（Freshness）、来源可追溯性（Provenance）或正式推荐（Recommendation）。完整度100%也不能绕过利润要求、合规硬性阻断和人工审核；来源为Mock的数据不代表真实市场。")
    elif not views:
        payload.update(response_type="clarification", task_completed=False,
            answer="请指定一个商品或明确选择要检查的商品；当前没有可唯一确定的查询对象。")
    else:
        payload["response_type"] = intent
        payload["task_completed"] = True
        parts = []
        for view in views:
            trust, analysis = view.get("data_trust") or {}, view.get("analysis") or {}
            dimensions = understanding.requested_dimensions
            if intent == "data_quality_answer" and "price" not in dimensions:
                fields = list(trust.get("fields", {}).values())
            elif intent == "provenance_fact" and dimensions == ["provenance"]:
                fields = list(trust.get("fields", {}).values())
            elif intent == "decision_explanation":
                # Gate reasons come from the existing decision engine, never an LLM.
                fields = scoped_fields(trust, ("profit", "compliance", "risk"))
            else:
                fields = scoped_fields(trust, dimensions)
            notices = [item.model_dump() for item in evidence_notices([FieldEvidence.model_validate(f) for f in fields])]
            payload["trust_notices"].extend(notices)
            title = view["title"]
            recommendation = (payload.get("task_state") or {}).get("active_recommendation") or {}
            if intent == "calculation_explanation":
                calculation = analysis.get("calculation_evidence") or {}
                if understanding.metric == "cost_breakdown" and calculation.get("cost_breakdown"):
                    text = _cost_breakdown_answer(title, analysis)
                else:
                    text = _calculation_answer(title, analysis)
            elif intent == "decision_explanation":
                product = next(p for p in payload["products"] if p["id"] == view["id"])
                payload["decision_status"] = product["decision_status"]
                if "freshness" in dimensions and recommendation:
                    critical_names = set(recommendation.get("evidence_fields") or [])
                    critical = [field for field in fields if not critical_names or field.get("field") in critical_names]
                    stale = [field.get("field") for field in critical if (field.get("freshness") or {}).get("status") == "STALE"]
                    unknown = [field.get("field") for field in critical if (field.get("freshness") or {}).get("status") == "UNKNOWN"]
                    if stale or unknown:
                        affected = "、".join([*stale, *unknown][:8]) or "关键字段"
                        text = f"不会直接维持正式推荐。{affected}的时效已过期或无法确认，后端策略会把该结论降级为证据不足/人工复核，重新采集并通过推荐门禁后才能恢复。"
                    else:
                        text = f"当前推荐所用关键字段均在已配置有效期内，但这只代表时效通过；决策状态仍为 {product['decision_status']}，最终动作继续由人工审核。"
                elif recommendation:
                    text = (
                        f"当前选择它是因为后端候选排序中推荐度为 {product['score']:.1f}，"
                        f"净利率 {product['current_margin_rate']:.1%}、风险 {product['risk_level']}、"
                        f"合规状态 {product['compliance_status']}，推荐门禁结果为 {product['decision_status']}。"
                        "这是继续调研建议，不替代最终人工决策。"
                    )
                else:
                    reasons = [item.get("message", "") for item in analysis.get("risks", []) if item.get("code") not in {"STATIC_SALES_GROWTH_DECLINE", "DECLINING_DEMAND"}]
                    missing = analysis.get("missing_data") or []
                    text = f"当前决策状态 {product['decision_status']}。依据：" + "；".join(reasons)
                    if missing:
                        text += "；缺失证据：" + "、".join(str(item.get("label") or item.get("field") or item.get("message") or "未命名证据") for item in missing)
                    if not reasons and not missing:
                        text += "当前保存分析未列出额外阻断原因，不假设存在‘不推荐’结论。"
            elif intent == "data_quality_answer":
                groups = {status: [f["field"] for f in fields if f.get("freshness", {}).get("status") == status] for status in ("FRESH", "STALE", "UNKNOWN")}
                text = "；".join(f"{status}（{len(names)}项）：{'、'.join(names) or '无'}" for status, names in groups.items())
                text += "。FRESH仅表示在当前有效期内，不代表真实性已认证；UNKNOWN不是新鲜。"
            elif intent == "provenance_fact" and recommendation:
                lines = []
                for field in fields:
                    freshness = (field.get("freshness") or {}).get("status", "UNKNOWN")
                    derived = "派生指标" if field.get("derived_from") else "原始/录入字段"
                    mock = "Mock" if field.get("is_mock") else str(field.get("source_type") or "unknown")
                    lines.append(f"{field.get('field')}：{field.get('provider', 'unknown')}，{derived}，{mock}，时效 {freshness}")
                text = "当前推荐证据来源：" + "；".join(lines[:12]) + "。"
                if not lines:
                    text = "当前推荐结果没有可验证的字段来源，不能把推荐视为已证实事实。"
            else:
                if "sales_snapshot" in dimensions:
                    suff = payload.get("data_sufficiency") or {}
                    text = f"企业报告记录增长率 {suff.get('reported_metric')}%（来源：{suff.get('reported_metric_source', 'unknown')}），它不是系统快照趋势。当前 {suff.get('snapshot_count', 0)} 次快照，观察趋势 {suff.get('observed_trend', 'INSUFFICIENT_DATA')}。"
                    if suff.get("status") == "INSUFFICIENT_DATA":
                        text += "证据不足，无法根据快照验证涨跌。"
                elif intent == "product_detail" and ("profit" in dimensions or "roi" in dimensions):
                    text = _profit_answer(title, analysis)
                else:
                    text = "；".join(f"{f['field']}={f.get('value')}；来源类型 {f.get('source_type')}；provider {f.get('provider')}；采集时间 {f.get('collected_at') or '未知'}；时效 {f.get('freshness', {}).get('status', 'UNKNOWN')}" for f in fields if f["field"] in {"current_price", "net_margin", "net_profit"})
                text += " " + trust.get("disclosure", "来源未知。")
                if trust.get("is_mock"):
                    text += " 不是实时 Ozon 数据，不能作为真实 Ozon 原始页面凭证。"
                else:
                    text += " 来源声明和有效期不等于实时核验；当前未请求外部平台。"
            parts.append(text if text.startswith(title) else f"{title}：{text}")
            payload["evidence"].append({"product_id": view["id"], "title": title, "source": "validated_backend_evidence", "summary": text,
                "fields": fields, "disclosure": trust.get("disclosure", ""), "url": "" if trust.get("is_mock") else next((f.get("source_url") for f in fields if f.get("source_url")), "")})
        payload["answer"] = "\n".join(parts)
    payload["conclusion"] = payload["answer"]
