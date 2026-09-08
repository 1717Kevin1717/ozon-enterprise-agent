"""Compact projections of validated tools and existing evidence; no recalculation."""
from app.agent.query_understanding import SEMANTIC_TYPES
from app.schemas.provenance import FieldEvidence
from app.services.provenance import evidence_notices, scoped_fields


def scope_semantic_response(payload, views, understanding, entity_blocked):
    intent = understanding.intent
    if intent not in SEMANTIC_TYPES | {"unknown"}:
        return
    if entity_blocked:
        return
    payload.update(decision_summary={}, decision_status="NOT_APPLICABLE", human_review_required=False,
                   requires_human_review=False, warnings=[], risks=[], missing_data=[], next_actions=[], evidence=[], trust_notices=[])
    payload["display_scope"] = ["answer", "evidence", "source", "tool_summary", *understanding.requested_dimensions]
    if intent == "unknown":
        payload.update(response_type="clarification", task_completed=False,
                       answer="我还不能确定这个问题的意图。请说明要查询的商品，以及价格、来源、计算依据或推荐原因等具体内容。")
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
            if intent == "calculation_explanation":
                text = "；".join(f"{f['field']} = {f.get('value')}；公式：{f.get('calculation') or '计算公式未保存'}；输入：" + "、".join(f"{i['field']}={i.get('value')}（{i.get('provider', 'unknown')}）" for i in f.get('inputs', [])) for f in fields)
            elif intent == "decision_explanation":
                product = next(p for p in payload["products"] if p["id"] == view["id"])
                payload["decision_status"] = product["decision_status"]
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
            else:
                if "sales_snapshot" in dimensions:
                    suff = payload.get("data_sufficiency") or {}
                    text = f"企业报告记录增长率 {suff.get('reported_metric')}%（来源：{suff.get('reported_metric_source', 'unknown')}），它不是系统快照趋势。当前 {suff.get('snapshot_count', 0)} 次快照，观察趋势 {suff.get('observed_trend', 'INSUFFICIENT_DATA')}。"
                    if suff.get("status") == "INSUFFICIENT_DATA":
                        text += "证据不足，无法根据快照验证涨跌。"
                else:
                    text = "；".join(f"{f['field']}={f.get('value')}；来源类型 {f.get('source_type')}；provider {f.get('provider')}；采集时间 {f.get('collected_at') or '未知'}；时效 {f.get('freshness', {}).get('status', 'UNKNOWN')}" for f in fields if f["field"] in {"current_price", "net_margin", "net_profit"})
                text += " " + trust.get("disclosure", "来源未知。")
                if trust.get("is_mock"):
                    text += " 不是实时 Ozon 数据，不能作为真实 Ozon 原始页面凭证。"
                else:
                    text += " 来源声明和有效期不等于实时核验；当前未请求外部平台。"
            parts.append(f"{title}：{text}")
            payload["evidence"].append({"product_id": view["id"], "title": title, "source": "validated_backend_evidence", "summary": text,
                "fields": fields, "disclosure": trust.get("disclosure", ""), "url": "" if trust.get("is_mock") else next((f.get("source_url") for f in fields if f.get("source_url")), "")})
        payload["answer"] = "\n".join(parts)
    payload["conclusion"] = payload["answer"]
