from collections import Counter
from datetime import UTC, datetime
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.agent.tool_registry import TOOL_REGISTRY
from app.agent.tools import compare_products, rule_agent_ask
from app.agent.zhipu_agent import zhipu_agent_ask
from app.core.config import settings
from app.db.models import AgentRun, Company, ConversationMessage, ConversationSession, Memory, ProductDecision, ProductRelation
from app.db.session import get_session
from app.repositories.products import ProductRepository, product_view
from app.schemas.products import AgentAsk, CapturePacket, CompareRequest, DecisionCreate, DemoSeedRequest, ProductIn, ProductPatch
from app.services.demo_dataset import enterprise_demo_candidates
from app.services.provenance import snapshot_view

router = APIRouter(prefix="/api/v1")
ROLE_ORDER = {"viewer":0,"operator":1,"analyst":2,"reviewer":3,"company_admin":4,"super_admin":5}

async def context(x_company_id: str | None = Header(default=None), x_user_id: str | None = Header(default=None), x_role: str | None = Header(default=None)) -> dict:
    # 开发阶段使用显式请求头或 .env 默认值；生产必须替换为 JWT/OAuth 中间件，不接受客户端伪造角色头。
    return {"company_id":x_company_id or settings.default_company_id,"user_id":x_user_id or settings.default_user_id,"role":x_role or settings.default_role}

def require_role(ctx: dict, minimum: str) -> None:
    if ROLE_ORDER.get(ctx["role"], -1) < ROLE_ORDER[minimum]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail={"code":"FORBIDDEN","message":"当前角色无权执行该操作。"})

def capture_to_product(packet: CapturePacket) -> ProductIn:
    snapshot=packet.snapshot or {}; master=packet.master or {}; catalog=master.get("catalog",{}) if isinstance(master,dict) else {}; identity=master.get("identity",{}) if isinstance(master,dict) else {}
    commission=float(snapshot.get("commissionRate",0) or 0)
    if commission > 1: commission /= 100
    product_id=str(snapshot.get("ozonProductId") or snapshot.get("productId") or identity.get("ozonProductId") or "").strip()
    visible_competitors=snapshot.get("visibleCompetitors", snapshot.get("competitorEntries", []))
    visible_competitors=visible_competitors if isinstance(visible_competitors,list) else []
    lowest_visible_price=float(snapshot.get("visibleLowestCompetitorPriceRub",0) or 0)
    manual_competitor_count=int(float(snapshot.get("competitorCount",0) or 0))
    competitor_count=manual_competitor_count or len(visible_competitors)
    competition_evidence=str(snapshot.get("competitionEvidence") or "")
    if visible_competitors:
        source_note=f"当前可见 Ozon 页面已选择 {len(visible_competitors)} 条相似商品/其他卖家条目"
        competition_evidence=f"{competition_evidence}；{source_note}".strip("；")
    return ProductIn(
        external_product_id=product_id or str(snapshot.get("masterKey") or "").replace("ozon-url:","url:"),
        title=str(snapshot.get("title") or identity.get("title") or "").strip(),
        brand=str(snapshot.get("brand") or catalog.get("brand") or "").strip(),
        category_path=str(snapshot.get("categoryPath") or snapshot.get("category") or catalog.get("categoryPath") or "").strip(),
        sku=str(snapshot.get("sku") or ""), url=str(snapshot.get("url") or identity.get("url") or ""), main_image_url=str(snapshot.get("imageUrl") or snapshot.get("image") or ""),
        current_price=float(snapshot.get("currentPriceRub",snapshot.get("priceRub",0)) or 0), regular_price=float(snapshot.get("regularPriceRub",0) or 0),
        rating=float(snapshot.get("rating",0) or 0), review_count=int(float(snapshot.get("reviewCount",0) or 0)), latest_30d_sales=float(snapshot.get("monthlySales",0) or 0), sales_growth_rate=float(snapshot.get("salesGrowthRate",0) or 0),
        sales_source=str(snapshot.get("salesSource") or "not_provided"), sales_evidence=str(snapshot.get("salesEvidence") or ""), competitor_count=competitor_count, competition_evidence=competition_evidence, competitor_entries=visible_competitors, visible_lowest_competitor_price=lowest_visible_price,
        compliance_status={"通过":"approved","未通过":"rejected","待核验":"pending"}.get(str(snapshot.get("complianceStatus") or "pending"),str(snapshot.get("complianceStatus") or "pending")), compliance_note=str(snapshot.get("complianceEvidence") or ""),
        procurement_cost=float(snapshot.get("estimatedCostRub",0) or 0), fulfillment_cost=float(snapshot.get("shippingCostRub",0) or 0), advertising_cost=float(snapshot.get("adCostRub",0) or 0), platform_commission_rate=commission,
        target_margin_rate=float(snapshot.get("targetMarginRate",0.30) or 0.30), lifecycle_status=str(snapshot.get("listingStatus") or "candidate"), tags=snapshot.get("tags",catalog.get("tags",[])) if isinstance(snapshot.get("tags",catalog.get("tags",[])),list) else [item.strip() for item in str(snapshot.get("tags","")).replace("，",",").split(",") if item.strip()], field_lineage=packet.fieldLineage or {}, raw_payload=snapshot, captured_at=packet.capturedAt,
    )


def legacy_demo_candidates() -> list[ProductIn]:
    """Local-only portfolio used to demonstrate the full decision workflow."""

    common = {
        "platform_commission_rate": 0.18,
        "target_margin_rate": 0.30,
        "sales_source": "demo_authorized_seller_report",
        "field_lineage": {"currentPriceRub": "demo_visible_page", "latest30dSales": "demo_authorized_seller_report"},
    }
    rows = [
        {
            "external_product_id": "demo-ozon-1001", "title": "便携式电动冲牙器", "brand": "AquaNova", "category_path": "个护健康 > 口腔护理 > 冲牙器", "current_price": 3290, "regular_price": 3990,
            "rating": 4.8, "review_count": 1260, "latest_30d_sales": 168, "sales_growth_rate": 12.4, "sales_evidence": "演示：授权店铺近30天订单汇总", "competitor_count": 14,
            "competition_evidence": "演示：当前页面确认14个同口径竞品，主流价位2890–3690 RUB", "visible_lowest_competitor_price": 2890, "procurement_cost": 790, "fulfillment_cost": 420,
            "advertising_cost": 260, "compliance_status": "approved", "compliance_note": "演示：俄文标签与EAC资料已人工核验", "tags": ["高增长", "个护", "演示数据"],
        },
        {
            "external_product_id": "demo-ozon-1002", "title": "宠物互动漏食球", "brand": "PawJoy", "category_path": "宠物用品 > 猫狗玩具 > 益智玩具", "current_price": 1190, "regular_price": 1490,
            "rating": 4.7, "review_count": 842, "latest_30d_sales": 236, "sales_growth_rate": 18.2, "sales_evidence": "演示：授权店铺销量报表", "competitor_count": 23,
            "competition_evidence": "演示：当前可见竞品23个，差异点为可拆洗结构", "visible_lowest_competitor_price": 890, "procurement_cost": 210, "fulfillment_cost": 190,
            "advertising_cost": 120, "compliance_status": "pending", "compliance_note": "演示：材质安全证明待补充", "tags": ["宠物", "增长机会", "演示数据"],
        },
        {
            "external_product_id": "demo-ozon-1003", "title": "儿童磁力积木套装", "brand": "MagiKids", "category_path": "母婴玩具 > 益智玩具 > 磁力积木", "current_price": 4590, "regular_price": 5290,
            "rating": 4.6, "review_count": 416, "latest_30d_sales": 74, "sales_growth_rate": -4.8, "sales_evidence": "演示：授权店铺销量报表", "competitor_count": 31,
            "competition_evidence": "演示：同质竞品密集，头部商品评论壁垒明显", "visible_lowest_competitor_price": 3190, "procurement_cost": 1380, "fulfillment_cost": 690,
            "advertising_cost": 360, "compliance_status": "rejected", "compliance_note": "演示：儿童用品证书范围不匹配，禁止进入", "tags": ["合规阻断", "玩具", "演示数据"],
        },
        {
            "external_product_id": "demo-ozon-1004", "title": "真空压缩收纳袋 8件套", "brand": "HomeFold", "category_path": "家居收纳 > 收纳袋 > 真空压缩袋", "current_price": 1890, "regular_price": 2190,
            "rating": 4.9, "review_count": 2180, "latest_30d_sales": 312, "sales_growth_rate": 7.6, "sales_evidence": "演示：授权店铺销量报表", "competitor_count": 42,
            "competition_evidence": "演示：价格竞争激烈，需验证加厚材质溢价", "visible_lowest_competitor_price": 1290, "procurement_cost": 430, "fulfillment_cost": 310,
            "advertising_cost": 170, "compliance_status": "approved", "compliance_note": "演示：包装与材质说明已核验", "tags": ["家居", "稳定销量", "演示数据"],
        },
        {
            "external_product_id": "demo-ozon-1005", "title": "汽车座椅缝隙收纳盒", "brand": "RoadMate", "category_path": "汽车用品 > 车内收纳 > 座椅收纳", "current_price": 2490, "regular_price": 2890,
            "rating": 4.5, "review_count": 96, "latest_30d_sales": 0, "sales_growth_rate": 0, "sales_source": "not_provided", "sales_evidence": "", "competitor_count": 17,
            "competition_evidence": "演示：当前可见17个相似款，皮纹与杯架组合可差异化", "visible_lowest_competitor_price": 1690, "procurement_cost": 620, "fulfillment_cost": 380,
            "advertising_cost": 190, "compliance_status": "approved", "compliance_note": "演示：常规消费品资料已核验", "tags": ["汽车用品", "待补销量", "演示数据"],
        },
        {
            "external_product_id": "demo-ozon-1006", "title": "硅胶空气炸锅垫 2件套", "brand": "CookEase", "category_path": "厨房用品 > 烘焙工具 > 硅胶垫", "current_price": 990, "regular_price": 1290,
            "rating": 4.7, "review_count": 1560, "latest_30d_sales": 421, "sales_growth_rate": 3.2, "sales_evidence": "演示：授权店铺销量报表", "competitor_count": 56,
            "competition_evidence": "演示：红海类目，当前可见价格带690–1190 RUB", "visible_lowest_competitor_price": 690, "procurement_cost": 150, "fulfillment_cost": 170,
            "advertising_cost": 90, "compliance_status": "approved", "compliance_note": "演示：食品接触材料说明已人工核验", "tags": ["厨房", "红海竞争", "演示数据"],
        },
    ]
    return [
        ProductIn(**{**common, **row, "raw_payload": {"demo_data": True, "demo_notice": "仅用于本地产品演示，不代表真实 Ozon 市场事实。"}})
        for row in rows
    ]


def demo_candidates() -> list[ProductIn]:
    """Deterministic 60-item enterprise dataset; keeps legacy IDs for compatibility."""
    return enterprise_demo_candidates()

async def agent_provider_status(session: AsyncSession, company_id: str) -> dict:
    latest = await session.scalar(
        select(AgentRun).where(
            AgentRun.company_id == company_id,
            or_(AgentRun.active_provider == "zhipu", AgentRun.fallback_reason != ""),
        ).order_by(desc(AgentRun.created_at)).limit(1)
    )
    configured = settings.llm_provider.lower() == "zhipu" and bool(settings.zhipu_api_key)
    if not configured:
        return {"configured": False, "reachable": None, "last_call_success": False, "active_provider": "deterministic_planner", "active_model": "agent-v2-rule-planner", "response_mode": "rule_engine", "fallback_reason": "KEY_MISSING", "latency_ms": None, "token_usage": {}, "status": "unconfigured", "notice": "未配置智谱，当前使用确定性 Planner。", "last_checked_at": None}
    if not latest:
        return {"configured": True, "reachable": None, "last_call_success": None, "active_provider": "zhipu", "active_model": settings.zhipu_model, "response_mode": "configured_unverified", "fallback_reason": None, "latency_ms": None, "token_usage": {}, "status": "configured", "notice": "智谱已配置，但当前企业尚无可审计的实际调用记录。", "last_checked_at": None}
    success = latest.response_mode == "glm_success"
    reason = latest.fallback_reason or None
    messages = {
        "AUTHENTICATION_FAILED": "智谱认证失败；最近回答已安全回退。",
        "MODEL_UNAVAILABLE": "智谱模型不可用；最近回答已安全回退。",
        "RATE_LIMITED": "智谱额度或频率受限；最近回答已安全回退。",
        "NETWORK_TIMEOUT": "智谱网络请求超时；最近回答已安全回退。",
        "INVALID_RESPONSE": "智谱响应格式不合规；最近回答已安全回退。",
        "PROVIDER_UNAVAILABLE": "智谱服务暂时不可用；最近回答已安全回退。",
        "PROVIDER_ERROR": "智谱调用失败；最近回答已安全回退。",
    }
    return {
        "configured": True,
        "reachable": False if reason == "NETWORK_TIMEOUT" else True if success or reason else None,
        "last_call_success": success,
        "active_provider": latest.active_provider,
        "active_model": latest.model,
        "response_mode": latest.response_mode,
        "fallback_reason": reason,
        "latency_ms": latest.latency_ms,
        "token_usage": latest.token_usage or {},
        "status": "ready" if success else "error",
        "notice": "智谱最近一次受控调用成功。" if success else messages.get(reason or "", "智谱最近调用状态未知，当前回答可能来自回退。"),
        "last_checked_at": latest.created_at,
    }


@router.get("/health")
async def health(session: AsyncSession = Depends(get_session)):
    status_payload = await agent_provider_status(session, settings.default_company_id)
    return {"success": True, "data": {"status": "ok", "agent_mode": status_payload["response_mode"], "llm_provider": settings.llm_provider, "llm_configured": status_payload["configured"], **status_payload}}


@router.get("/agent/status")
async def agent_status(ctx: dict = Depends(context), session: AsyncSession = Depends(get_session)):
    return {"success": True, "data": await agent_provider_status(session, ctx["company_id"])}


@router.get("/dashboard/overview")
async def dashboard_overview(ctx: dict = Depends(context), session: AsyncSession = Depends(get_session)):
    repo = ProductRepository(session, ctx["company_id"])
    products = await repo.list(limit=500)
    views = [product_view(product, await repo.latest_analysis(product.id)) for product in products]
    latest_decisions: dict[str, ProductDecision] = {}
    decision_rows = list((await session.scalars(
        select(ProductDecision).where(ProductDecision.company_id == ctx["company_id"]).order_by(desc(ProductDecision.created_at))
    )).all())
    for row in decision_rows:
        latest_decisions.setdefault(row.product_id, row)

    analyzed = [item for item in views if item.get("analysis")]
    scored = [item["analysis"]["recommendation_score"] for item in analyzed]
    margins = [item["current_margin_rate"] for item in views if item.get("current_price", 0) > 0 and item.get("procurement_cost", 0) > 0]
    verified_profit = sum(
        item.get("latest_30d_sales", 0) * item.get("current_price", 0) * item.get("current_margin_rate", 0)
        for item in views if item.get("sales_source") not in {"", "not_provided", "未提供"}
    )
    pipeline = Counter(item.get("lifecycle_status") or "candidate" for item in views)
    categories: dict[str, dict] = {}
    for item in views:
        category = (item.get("category_path") or "待归类").split(">")[0].strip() or "待归类"
        bucket = categories.setdefault(category, {"name": category, "count": 0, "scores": [], "sales": 0.0})
        bucket["count"] += 1
        bucket["sales"] += float(item.get("latest_30d_sales") or 0)
        if item.get("analysis"): bucket["scores"].append(float(item["analysis"].get("recommendation_score") or 0))
    category_rows = [
        {"name": bucket["name"], "count": bucket["count"], "sales": round(bucket["sales"], 1), "average_score": round(sum(bucket["scores"]) / len(bucket["scores"]), 1) if bucket["scores"] else 0}
        for bucket in categories.values()
    ]
    category_rows.sort(key=lambda item: (item["average_score"], item["sales"]), reverse=True)

    coverage = {key: 0 for key in ("identity", "demand", "profit", "competition", "compliance")}
    for item in analyzed:
        groups = item["analysis"].get("evidence_completeness", {}).get("groups", {})
        for key in coverage:
            coverage[key] += 1 if groups.get(key, {}).get("ready") else 0
    denominator = max(len(views), 1)
    coverage_rows = [{"key": key, "percent": round(value / denominator * 100)} for key, value in coverage.items()]

    queue = []
    for item in views:
        analysis = item.get("analysis") or {}
        decision = latest_decisions.get(item["id"])
        if not decision or decision.decision_status in {"pending", "needs_more_data"}:
            queue.append({
                "product_id": item["id"], "title": item["title"], "brand": item.get("brand"),
                "score": analysis.get("recommendation_score"), "risk_level": analysis.get("risk_level", "not_analyzed"),
                "completeness": analysis.get("evidence_completeness", {}).get("percent", 0),
                "decision_status": decision.decision_status if decision else "unsubmitted",
                "missing_count": len(analysis.get("missing_data", [])),
            })
    queue.sort(key=lambda item: (item["risk_level"] != "high", -item["missing_count"]))

    agent_runs = list((await session.scalars(
        select(AgentRun).where(AgentRun.company_id == ctx["company_id"]).order_by(desc(AgentRun.created_at)).limit(8)
    )).all())
    provider_payload = await agent_provider_status(session, ctx["company_id"])
    recent_activity = [
        {"type": "agent", "title": "AI 决策任务", "detail": row.query[:90], "status": "failed" if row.error else "completed", "created_at": row.created_at}
        for row in agent_runs
    ]
    recent_activity.extend(
        {"type": "product", "title": "候选商品更新", "detail": item["title"], "status": item.get("lifecycle_status", "candidate"), "created_at": item["updated_at"]}
        for item in views[:6]
    )
    recent_activity.sort(key=lambda item: item["created_at"], reverse=True)

    return {"success": True, "data": {
        "metrics": {
            "total_products": len(views), "analyzed_products": len(analyzed), "pending_reviews": len(queue),
            "high_risk_products": sum(1 for item in analyzed if item["analysis"].get("risk_level") == "high"),
            "average_score": round(sum(scored) / len(scored), 1) if scored else None,
            "average_margin_rate": round(sum(margins) / len(margins), 4) if margins else None,
            "verified_monthly_profit_potential": round(verified_profit, 2),
            "approved_decisions": sum(1 for row in latest_decisions.values() if row.decision_status == "approved"),
        },
        "pipeline": [{"status": key, "count": value} for key, value in pipeline.items()],
        "categories": category_rows[:8], "data_coverage": coverage_rows, "review_queue": queue[:8],
        "recent_activity": recent_activity[:10],
        "agent": {
            "provider": settings.llm_provider,
            "model": provider_payload["active_model"],
            "runs": len(agent_runs),
            "status_code": provider_payload["fallback_reason"],
            **provider_payload,
        },
        "is_demo_empty": len(views) == 0,
        "data_disclosure": {"contains_mock": any(item["data_trust"]["is_mock"] for item in views), "notice": "含演示 / Mock 数据，汇总值不代表真实 Ozon 市场。" if any(item["data_trust"]["is_mock"] for item in views) else "企业录入数据；来源声明不等于已通过外部真实性验证。"},
    }}


@router.get("/agent/tools")
async def list_agent_tools(ctx: dict = Depends(context)):
    tools = []
    for spec in TOOL_REGISTRY.all():
        tools.append({
            "name": spec.tool_name, "description": spec.description, "permission": spec.required_permission,
            "allowed": spec.allows(ctx["role"]), "risk_level": spec.risk_level, "timeout_seconds": spec.timeout_seconds,
            "idempotent": spec.idempotent, "version": spec.version,
        })
    return {"success": True, "data": tools}


@router.post("/demo/seed")
async def seed_demo(data: DemoSeedRequest, ctx: dict = Depends(context), session: AsyncSession = Depends(get_session)):
    require_role(ctx, "company_admin")
    if settings.app_env not in {"development", "test"}:
        raise HTTPException(status_code=403, detail={"code": "DEMO_DISABLED", "message": "演示数据只允许在开发或测试环境使用。"})
    repo = ProductRepository(session, ctx["company_id"])
    created, updated, rows = 0, 0, []
    for candidate in demo_candidates():
        existed = await repo.get_by_external_id(candidate.external_product_id)
        if existed and not data.replace_demo:
            rows.append(product_view(existed, await repo.latest_analysis(existed.id)))
            continue
        product = await repo.upsert(candidate)
        analysis = await repo.analyze(product)
        rows.append(product_view(product, analysis))
        if existed: updated += 1
        else: created += 1
    return {"success": True, "data": {"created": created, "updated": updated, "products": rows, "notice": "这些记录仅为本地演示数据，不代表真实 Ozon 市场事实。"}}

@router.get("/companies/me")
async def company_me(ctx: dict = Depends(context), session: AsyncSession = Depends(get_session)):
    company=await session.get(Company,ctx["company_id"])
    if not company: company=Company(id=ctx["company_id"],name="本地中贸通试点企业"); session.add(company); await session.commit(); await session.refresh(company)
    return {"success":True,"data":{"id":company.id,"name":company.name,"role":ctx["role"],"mode":settings.app_env}}

@router.get("/products")
async def list_products(q: str = "", ctx: dict = Depends(context), session: AsyncSession = Depends(get_session)):
    repo=ProductRepository(session,ctx["company_id"]); rows=[]
    for product in await repo.list(q): rows.append(product_view(product,await repo.latest_analysis(product.id)))
    return {"success":True,"data":rows}

@router.post("/products", status_code=status.HTTP_201_CREATED)
async def create_product(data: ProductIn, ctx: dict = Depends(context), session: AsyncSession = Depends(get_session)):
    require_role(ctx,"operator"); product=await ProductRepository(session,ctx["company_id"]).upsert(data)
    return {"success":True,"data":product_view(product)}

@router.get("/products/{product_id}")
async def get_product(product_id: str, ctx: dict = Depends(context), session: AsyncSession = Depends(get_session)):
    repo=ProductRepository(session,ctx["company_id"]); product=await repo.get(product_id)
    if not product: raise HTTPException(404,detail={"code":"PRODUCT_NOT_FOUND","message":"商品不存在或不属于当前企业。"})
    return {"success":True,"data":product_view(product,await repo.latest_analysis(product.id))}

@router.patch("/products/{product_id}")
async def patch_product(product_id: str, data: ProductPatch, ctx: dict = Depends(context), session: AsyncSession = Depends(get_session)):
    require_role(ctx,"operator"); repo=ProductRepository(session,ctx["company_id"]); product=await repo.get(product_id)
    if not product: raise HTTPException(404,detail={"code":"PRODUCT_NOT_FOUND","message":"商品不存在或不属于当前企业。"})
    product=await repo.patch(product,data); return {"success":True,"data":product_view(product)}

@router.post("/products/{product_id}/analyze")
async def analyze_product(product_id: str, ctx: dict = Depends(context), session: AsyncSession = Depends(get_session)):
    require_role(ctx,"analyst"); repo=ProductRepository(session,ctx["company_id"]); product=await repo.get(product_id)
    if not product: raise HTTPException(404,detail={"code":"PRODUCT_NOT_FOUND","message":"商品不存在或不属于当前企业。"})
    analysis=await repo.analyze(product)
    payload=product_view(product,analysis)["analysis"]
    payload["visible_competitors"]=(product.raw_payload or {}).get("visibleCompetitors",[])
    payload["visible_lowest_competitor_price"]=(product.raw_payload or {}).get("visibleLowestCompetitorPriceRub",0)
    return {"success":True,"data":payload}

@router.get("/products/{product_id}/analysis")
async def get_analysis(product_id: str, ctx: dict = Depends(context), session: AsyncSession = Depends(get_session)):
    repo=ProductRepository(session,ctx["company_id"]); analysis=await repo.latest_analysis(product_id)
    if not analysis: raise HTTPException(404,detail={"code":"ANALYSIS_NOT_FOUND","message":"尚未分析该商品。"})
    product=await ProductRepository(session,ctx["company_id"]).get(product_id)
    return {"success":True,"data":{"algorithm_version":analysis.algorithm_version,"weights":analysis.weights_json,"scores":{"demand":analysis.demand_score,"profit":analysis.profit_score,"competition":analysis.competition_score,"compliance":analysis.compliance_score,"total":analysis.total_score},"risk_level":analysis.risk_level,"risks":analysis.risks_json,"evidence":product_view(product, analysis)["analysis"]["evidence"],"input_snapshot":analysis.input_snapshot,"visible_competitors":(product.raw_payload or {}).get("visibleCompetitors",[]) if product else [],"visible_lowest_competitor_price":(product.raw_payload or {}).get("visibleLowestCompetitorPriceRub",0) if product else 0,"created_at":analysis.created_at}}


@router.get("/products/{product_id}/history")
async def product_history(product_id: str, ctx: dict = Depends(context), session: AsyncSession = Depends(get_session)):
    repo = ProductRepository(session, ctx["company_id"])
    product = await repo.get(product_id)
    if not product:
        raise HTTPException(404, detail={"code": "PRODUCT_NOT_FOUND", "message": "商品不存在或不属于当前企业。"})
    rows = await repo.history(product_id)
    return {"success": True, "data": [snapshot_view(row) for row in rows]}


@router.get("/decisions")
async def list_decisions(ctx: dict = Depends(context), session: AsyncSession = Depends(get_session)):
    repo = ProductRepository(session, ctx["company_id"])
    products = await repo.list(limit=500)
    decision_rows = list((await session.scalars(
        select(ProductDecision).where(ProductDecision.company_id == ctx["company_id"]).order_by(desc(ProductDecision.created_at))
    )).all())
    latest: dict[str, ProductDecision] = {}
    for row in decision_rows:
        latest.setdefault(row.product_id, row)
    payload = []
    for product in products:
        analysis = await repo.latest_analysis(product.id)
        view = product_view(product, analysis)
        decision = latest.get(product.id)
        analysis_payload = view.get("analysis") or {}
        gates = {
            "analysis_complete": analysis_payload.get("total_score") is not None,
            "compliance_approved": product.compliance_status in {"approved", "通过"},
            "evidence_complete": analysis_payload.get("evidence_completeness", {}).get("grade") == "complete",
        }
        payload.append({
            "product": {"id": product.id, "title": product.title, "brand": product.brand, "category_path": product.category_path, "lifecycle_status": product.lifecycle_status},
            "analysis": analysis_payload,
            "decision": ({"id": decision.id, "status": decision.decision_status, "recommendation": decision.recommendation, "reason": decision.reason, "reviewer_id": decision.reviewer_id, "reviewed_at": decision.reviewed_at, "created_at": decision.created_at} if decision else None),
            "gates": gates, "can_approve": all(gates.values()),
        })
    return {"success": True, "data": payload}


@router.post("/products/{product_id}/decisions")
async def create_decision(product_id: str, data: DecisionCreate, ctx: dict = Depends(context), session: AsyncSession = Depends(get_session)):
    require_role(ctx, "reviewer")
    repo = ProductRepository(session, ctx["company_id"])
    product = await repo.get(product_id)
    if not product:
        raise HTTPException(404, detail={"code": "PRODUCT_NOT_FOUND", "message": "商品不存在或不属于当前企业。"})
    analysis = await repo.latest_analysis(product.id)
    if data.action == "approve":
        gates = {
            "analysis_complete": bool(analysis and analysis.total_score is not None),
            "compliance_approved": product.compliance_status in {"approved", "通过"},
            "evidence_complete": bool(analysis and (analysis.input_snapshot or {}).get("evidence_completeness", {}).get("grade") == "complete"),
        }
        if not all(gates.values()):
            raise HTTPException(status_code=409, detail={"code": "DECISION_GATED", "message": "分析、证据或合规闸门未通过，不能批准进入。", "gates": gates})
    mapping = {
        "approve": ("enter", "approved", "ready_to_list"),
        "reject": ("reject", "rejected", "abandoned"),
        "needs_more_data": ("hold", "needs_more_data", "candidate"),
    }
    recommendation, decision_status, lifecycle = mapping[data.action]
    decision = ProductDecision(
        company_id=ctx["company_id"], product_id=product.id, recommendation=recommendation,
        decision_status=decision_status, reason=data.reason.strip(), reviewer_id=ctx["user_id"],
        source="human_review", reviewed_at=datetime.now(UTC).replace(tzinfo=None),
    )
    product.lifecycle_status = lifecycle
    session.add(decision)
    await session.flush()
    if data.action == "reject":
        session.add(Memory(company_id=ctx["company_id"], memory_type="abandoned_product", source_type="decision", source_id=decision.id, content=f"人工驳回商品：{product.title}；原因：{data.reason.strip()}。", metadata_json={"product_id": product.id, "reason": data.reason.strip()}))
    await session.commit()
    await session.refresh(decision)
    return {"success": True, "data": {"id": decision.id, "product_id": product.id, "status": decision.decision_status, "recommendation": decision.recommendation, "reason": decision.reason, "reviewed_at": decision.reviewed_at, "lifecycle_status": product.lifecycle_status}}

@router.post("/products/compare")
async def compare(data: CompareRequest, ctx: dict = Depends(context), session: AsyncSession = Depends(get_session)):
    require_role(ctx,"viewer")
    return {"success":True,"data":await compare_products(ProductRepository(session,ctx["company_id"]),data.product_ids,ctx["role"])}

@router.post("/extension/capture")
@router.post("/extension/import")
async def extension_capture(packet: CapturePacket, ctx: dict = Depends(context), session: AsyncSession = Depends(get_session)):
    require_role(ctx,"operator"); repo=ProductRepository(session,ctx["company_id"]); product=await repo.upsert(capture_to_product(packet))
    relations=packet.snapshot.get("relations",[]) if isinstance(packet.snapshot,dict) else []
    for relation in relations if isinstance(relations,list) else []:
        if not isinstance(relation,dict) or not relation.get("type") or not relation.get("targetOzonProductId"): continue
        target=await repo.get_by_external_id(str(relation["targetOzonProductId"]))
        exists=await session.scalar(select(ProductRelation).where(ProductRelation.company_id==ctx["company_id"],ProductRelation.source_product_id==product.id,ProductRelation.target_product_id==(target.id if target else None),ProductRelation.relation_type==str(relation["type"])))
        if not exists: session.add(ProductRelation(company_id=ctx["company_id"],source_product_id=product.id,target_product_id=target.id if target else None,relation_type=str(relation["type"]),evidence=str(relation.get("evidence", "")),source="extension_or_enterprise_confirmed"))
    await session.commit()
    analysis=await repo.analyze(product)
    return {"success":True,"data":{"product":product_view(product,analysis),"backend_url":f"http://{settings.app_host}:{settings.app_port}/#product-{product.id}"}}

@router.post("/agent/sessions")
async def create_agent_session(ctx: dict = Depends(context), session: AsyncSession = Depends(get_session)):
    conversation=ConversationSession(company_id=ctx["company_id"],user_id=ctx["user_id"]); session.add(conversation); await session.commit(); return {"success":True,"data":{"id":conversation.id,"title":conversation.title}}

@router.get("/agent/sessions")
async def list_agent_sessions(ctx: dict = Depends(context), session: AsyncSession = Depends(get_session)):
    rows=list((await session.scalars(select(ConversationSession).where(ConversationSession.company_id == ctx["company_id"]).order_by(ConversationSession.updated_at.desc()))).all())
    return {"success":True,"data":[{"id":row.id,"title":row.title,"goal_summary":row.goal_summary,"last_product_ids":row.last_product_ids,"conclusion_summary":row.conclusion_summary,"state":row.state_json,"updated_at":row.updated_at} for row in rows]}


@router.get("/agent/sessions/{session_id}/messages")
async def agent_session_messages(session_id: str, ctx: dict = Depends(context), session: AsyncSession = Depends(get_session)):
    conversation = await session.scalar(select(ConversationSession).where(ConversationSession.id == session_id, ConversationSession.company_id == ctx["company_id"]))
    if not conversation:
        raise HTTPException(404, detail={"code": "SESSION_NOT_FOUND", "message": "会话不存在或不属于当前企业。"})
    rows = list((await session.scalars(select(ConversationMessage).where(ConversationMessage.company_id == ctx["company_id"], ConversationMessage.session_id == session_id).order_by(ConversationMessage.created_at))).all())
    return {"success": True, "data": [{"id": row.id, "role": row.role, "content": row.content, "metadata": row.metadata_json, "created_at": row.created_at} for row in rows]}

@router.post("/agent/ask")
async def ask_agent(data: AgentAsk, ctx: dict = Depends(context), session: AsyncSession = Depends(get_session)):
    require_role(ctx,"analyst")
    handler=zhipu_agent_ask if settings.llm_provider.lower()=="zhipu" else rule_agent_ask
    return {"success":True,"data":await handler(session,ctx["company_id"],ctx["user_id"],ctx["role"],data.query,data.session_id,data.selected_product_ids)}

@router.get("/memory/search")
async def memory_search(q: str, ctx: dict = Depends(context), session: AsyncSession = Depends(get_session)):
    require_role(ctx,"analyst"); rows=list((await session.scalars(select(Memory).where(Memory.company_id == ctx["company_id"],Memory.content.ilike(f"%{q}%")).limit(30))).all())
    return {"success":True,"data":[{"id":row.id,"memory_type":row.memory_type,"content":row.content,"metadata":row.metadata_json,"created_at":row.created_at} for row in rows],"mode":"lexical_fallback","notice":"配置 pgvector embedding 后可升级为语义检索。"}
