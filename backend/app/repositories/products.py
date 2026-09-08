from datetime import UTC, datetime
from copy import deepcopy
from typing import List
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import Memory, Product, ProductAnalysis, ProductSnapshot
from app.schemas.products import ProductIn, ProductPatch
from app.services.decision_engine import ALGORITHM_VERSION, WEIGHTS, analyze, to_dict
from app.services.provenance import build_trust, capture_lineage

class ProductRepository:
    def __init__(self, session: AsyncSession, company_id: str):
        self.session, self.company_id = session, company_id

    async def list(self, keyword: str = "", limit: int = 100) -> list[Product]:
        stmt = select(Product).where(Product.company_id == self.company_id).order_by(desc(Product.updated_at)).limit(limit)
        if keyword:
            token = f"%{keyword.strip()}%"
            stmt = stmt.where(Product.title.ilike(token) | Product.brand.ilike(token) | Product.category_path.ilike(token))
        return list((await self.session.scalars(stmt)).all())

    async def get(self, product_id: str) -> Product | None:
        return await self.session.scalar(select(Product).where(Product.id == product_id, Product.company_id == self.company_id))

    async def get_by_external_id(self, external_product_id: str) -> Product | None:
        return await self.session.scalar(select(Product).where(Product.external_product_id == external_product_id, Product.company_id == self.company_id))

    async def upsert(self, data: ProductIn) -> Product:
        product = await self.get_by_external_id(data.external_product_id)
        values = data.model_dump(exclude={"captured_at", "competitor_entries", "visible_lowest_competitor_price"})
        raw_payload = dict(data.raw_payload or {})
        raw_payload["visibleCompetitors"] = data.competitor_entries
        raw_payload["visibleLowestCompetitorPriceRub"] = data.visible_lowest_competitor_price
        if product and product.raw_payload:
            merged_payload = dict(product.raw_payload)
            merged_payload.update(raw_payload)
            raw_payload = merged_payload
        values["raw_payload"] = raw_payload
        raw_payload["_provided_fields"] = sorted(data.model_fields_set)
        values["field_lineage"] = capture_lineage(data)
        if product is None:
            product = Product(company_id=self.company_id, **values)
            self.session.add(product)
            await self.session.flush()
        else:
            for key, value in values.items(): setattr(product, key, value)
        await self._snapshot(product, raw_payload, data.captured_at)
        self.session.add(Memory(company_id=self.company_id, memory_type="product_fact", source_type="product", source_id=product.id, content=f"商品：{product.title}；品牌：{product.brand or '待确认'}；类目：{product.category_path or '待确认'}；当前价：{product.current_price} RUB；生命周期：{product.lifecycle_status}；销量来源：{product.sales_source or '未提供'}。", metadata_json={"external_product_id":product.external_product_id,"tags":product.tags or []}))
        abandon_reason=(data.raw_payload or {}).get("abandonReason", "")
        if product.lifecycle_status in {"abandoned", "已放弃"}:
            self.session.add(Memory(company_id=self.company_id, memory_type="abandoned_product", source_type="product", source_id=product.id, content=f"已放弃商品：{product.title}；品牌：{product.brand or '待确认'}；类目：{product.category_path or '待确认'}；失败原因：{abandon_reason or '未记录'}。", metadata_json={"product_id":product.id,"reason":abandon_reason,"tags":product.tags or []}))
        await self.session.commit()
        await self.session.refresh(product)
        return product

    async def patch(self, product: Product, data: ProductPatch) -> Product:
        values=data.model_dump(exclude_unset=True)
        entries=values.pop("competitor_entries", None)
        lowest=values.pop("visible_lowest_competitor_price", None)
        raw_payload=values.pop("raw_payload", None)
        if entries is not None or lowest is not None or raw_payload is not None:
            merged=dict(product.raw_payload or {})
            if isinstance(raw_payload,dict): merged.update(raw_payload)
            if entries is not None: merged["visibleCompetitors"]=entries
            if lowest is not None: merged["visibleLowestCompetitorPriceRub"]=lowest
            values["raw_payload"]=merged
        changed = {key for key, value in values.items() if key != "raw_payload" and getattr(product, key, None) != value}
        patch_input = data.model_copy(update={"raw_payload": values.get("raw_payload", product.raw_payload)})
        product.field_lineage = {**(product.field_lineage or {}), **capture_lineage(patch_input, changed_fields=changed)}
        updated_raw = dict(values.get("raw_payload", product.raw_payload) or {})
        updated_raw["_provided_fields"] = sorted(set(updated_raw.get("_provided_fields", [])) | changed)
        values["raw_payload"] = updated_raw
        for key, value in values.items(): setattr(product, key, value)
        await self._snapshot(product, product.raw_payload, None)
        await self.session.commit(); await self.session.refresh(product)
        return product

    async def _snapshot(self, product: Product, raw_payload: dict, captured_at: datetime | None) -> None:
        captured_at = captured_at or datetime.now(UTC).replace(tzinfo=None)
        snapshot_payload = {**(raw_payload or {}), "_field_lineage": product.field_lineage or {}}
        self.session.add(ProductSnapshot(company_id=self.company_id, product_id=product.id, captured_at=captured_at, price=product.current_price, rating=product.rating, review_count=product.review_count, sales_30d=product.latest_30d_sales, competitor_count=product.competitor_count, raw_payload=snapshot_payload))

    async def analyze(self, product: Product) -> ProductAnalysis:
        result = analyze(product)
        product.current_margin_rate = result.current_margin_rate
        product.price_position = result.price_position
        product.data_quality = result.evidence_completeness["grade"]
        record = ProductAnalysis(
            company_id=self.company_id,
            product_id=product.id,
            version=ALGORITHM_VERSION,
            demand_score=result.demand_score,
            profit_score=result.profit_score,
            competition_score=result.competition_score,
            compliance_score=result.compliance_score,
            risk_score=result.risk_score,
            total_score=result.total_score,
            recommendation_grade=result.recommendation_grade,
            risk_level=result.risk_level,
            gross_profit=result.gross_profit,
            gross_margin=result.gross_margin,
            net_profit=result.net_profit,
            net_margin=result.net_margin,
            roi=result.roi,
            data_completeness=result.data_completeness,
            missing_fields=result.missing_fields,
            score_explanations=result.score_explanations,
            risks_json=result.risks,
            evidence_json=result.evidence,
            input_snapshot={
                "price": product.current_price,
                "competitor_prices": {"min": product.competitor_price_min, "avg": product.competitor_price_avg},
                "costs": {
                    "procurement": product.procurement_cost,
                    "fulfillment": product.fulfillment_cost,
                    "shipping": product.shipping_cost,
                    "platform_fee": product.platform_fee,
                    "commission_rate": product.platform_commission_rate,
                    "advertising": product.advertising_cost,
                    "warehousing": product.warehousing_cost,
                    "tax": product.tax_cost,
                    "return_loss_reserve": product.return_loss_reserve,
                    "other": product.other_cost,
                },
                "sales": product.latest_30d_sales,
                "search_volume": product.search_volume,
                "competitors": product.competitor_count,
                "market_saturation": product.market_saturation,
                "compliance": product.compliance_status,
                "evidence_completeness": result.evidence_completeness,
                "missing_data": result.missing_data,
                "missing_fields": result.missing_fields,
                "data_confidence": result.data_confidence,
                "base_score": result.base_score,
                "recommendation_score": result.recommendation_score,
                "recommendation": result.recommendation,
            },
            weights_json=WEIGHTS,
            algorithm_version=ALGORITHM_VERSION,
        )
        self.session.add(record); await self.session.commit(); await self.session.refresh(record)
        self.session.add(Memory(company_id=self.company_id, memory_type="analysis_summary", source_type="product_analysis", source_id=record.id, content=f"分析：{product.title}；算法：{ALGORITHM_VERSION}；综合分：{result.total_score if result.total_score is not None else '待补数'}；风险等级：{result.risk_level}；推荐：{result.recommendation}。", metadata_json={"product_id":product.id,"algorithm_version":ALGORITHM_VERSION,"risks":result.risks}))
        await self.session.commit()
        return record

    async def latest_analysis(self, product_id: str) -> ProductAnalysis | None:
        return await self.session.scalar(select(ProductAnalysis).where(ProductAnalysis.company_id == self.company_id, ProductAnalysis.product_id == product_id).order_by(desc(ProductAnalysis.created_at)))

    async def history(self, product_id: str) -> List[ProductSnapshot]:
        return list((await self.session.scalars(select(ProductSnapshot).where(ProductSnapshot.company_id == self.company_id, ProductSnapshot.product_id == product_id).order_by(ProductSnapshot.captured_at))).all())

def product_view(product: Product, analysis: ProductAnalysis | None = None) -> dict:
    result = {key: getattr(product, key) for key in [
        "id","external_product_id","sku","title","brand","category","category_path","url","main_image_url","image_urls","seller_name","currency",
        "current_price","regular_price","competitor_price_min","competitor_price_avg","price_position","rating","review_count","latest_30d_sales","sales_growth_rate","search_volume","trend_score",
        "competitor_count","price_competition_score","market_saturation","compliance_status","compliance_note","certificates","manual_risk_level",
        "procurement_cost","fulfillment_cost","shipping_cost","platform_fee","advertising_cost","warehousing_cost","tax_cost","return_loss_reserve","other_cost",
        "platform_commission_rate","target_margin_rate","current_margin_rate","lifecycle_status","data_quality","tags","field_lineage","sales_source","sales_evidence","competition_evidence",
        "sync_status","last_sync_at","source_updated_at","raw_payload","created_at","updated_at"
    ]}
    result.update({
        "product_id": product.id,
        "name": product.title,
        "image_url": product.main_image_url,
        "cost_price": product.procurement_cost,
        "sale_price": product.current_price,
        "monthly_sales": product.latest_30d_sales,
        "sales_growth": product.sales_growth_rate,
    })
    raw_payload = product.raw_payload or {}
    result["visible_competitors"] = raw_payload.get("visibleCompetitors", [])
    result["visible_lowest_competitor_price"] = raw_payload.get("visibleLowestCompetitorPriceRub", 0)
    if analysis:
        meta=analysis.input_snapshot or {}
        result["analysis"] = {
            "algorithm_version": analysis.algorithm_version,
            "demand_score": analysis.demand_score,
            "profit_score": analysis.profit_score,
            "competition_score": analysis.competition_score,
            "compliance_score": analysis.compliance_score,
            "risk_score": analysis.risk_score,
            "total_score": analysis.total_score,
            "recommendation_score": meta.get("recommendation_score", analysis.total_score or 0),
            "recommendation_grade": analysis.recommendation_grade,
            "risk_level": analysis.risk_level,
            "gross_profit": analysis.gross_profit,
            "gross_margin": analysis.gross_margin,
            "net_profit": analysis.net_profit,
            "net_margin": analysis.net_margin,
            "roi": analysis.roi,
            "data_completeness": analysis.data_completeness,
            "missing_fields": analysis.missing_fields,
            "score_explanations": analysis.score_explanations,
            "risks": analysis.risks_json,
            "evidence": analysis.evidence_json,
            "evidence_completeness": meta.get("evidence_completeness", {}),
            "missing_data": meta.get("missing_data", []),
            "data_confidence": meta.get("data_confidence", 0),
            "base_score": meta.get("base_score", 0),
            "recommendation": meta.get("recommendation", "review_required"),
            "created_at": analysis.created_at,
        }
    current_analysis = result.get("analysis") or to_dict(analyze(product))
    result["data_trust"] = build_trust(product, current_analysis, saved_fields=(analysis.evidence_json or {}).get("field_provenance") if analysis else None, legacy_analysis=analysis is not None).model_dump(mode="json")
    # Legacy labels such as enterprise_verified are not proof of external authenticity.
    if analysis:
        projected = deepcopy(analysis.evidence_json or {})
        for dimension in ("demand", "profit", "competition", "compliance", "risk"):
            for item in projected.get(dimension, {}).get("fields", []):
                source = result["data_trust"]["fields"].get(item.get("field"), {})
                item["source"] = source.get("provider", "unverified_input")
                item["is_mock"] = source.get("is_mock", result["data_trust"]["is_mock"])
        result["analysis"]["evidence"] = {**projected, "field_provenance": result["data_trust"]["fields"], "disclosure": result["data_trust"]["disclosure"]}
    return result
