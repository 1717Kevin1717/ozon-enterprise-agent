from urllib.parse import quote
from datetime import UTC, datetime

from app.connectors.images import ImageProvider, PublicDemoImageProvider
from app.schemas.products import ProductIn


CATEGORY_PRODUCTS = {
    "厨房用品 > 厨房工具": ["硅胶空气炸锅垫", "玻璃油壶", "抽拉式调味架", "不锈钢滤篮", "多功能切菜器", "密封保鲜盒", "厨房电子秤", "刀具收纳架", "可折叠沥水篮", "咖啡压粉器", "烘焙量杯套装"],
    "宠物用品 > 日常护理": ["宠物互动漏食球", "猫咪饮水机", "宠物除毛刷", "慢食防噎碗", "猫砂防带出垫", "宠物指甲剪", "外出折叠水碗", "宠物清洁手套", "狗狗嗅闻垫", "宠物航空箱垫", "宠物喂药器"],
    "汽车用品 > 车内配件": ["汽车座椅缝隙收纳盒", "磁吸手机支架", "后备箱收纳箱", "车载垃圾桶", "方向盘防滑套", "车门防撞条", "汽车清洁刷套装", "遮阳挡隔热板", "车载杯架扩展器", "安全带护肩套", "车内除尘软胶"],
    "收纳用品 > 家庭收纳": ["真空压缩收纳袋", "抽屉分隔板", "透明鞋盒套装", "衣柜悬挂收纳袋", "桌面文件收纳架", "床底带轮收纳箱", "数据线整理盒", "厨房夹缝置物架", "可叠加衣物收纳筐", "旅行衣物收纳包", "化妆品旋转收纳盒"],
    "电子产品 > 消费电子配件": ["65W 氮化镓充电器", "七合一 USB-C 扩展坞", "降噪蓝牙耳机", "可调光阅读灯", "机械键盘", "无线充电支架", "便携蓝牙音箱", "智能温湿度计", "桌面理线器", "高清网络摄像头", "可充电鼠标"],
}


def _reference_url(name: str) -> str:
    return f"https://www.ozon.ru/search/?text={quote(name)}"


def _base_lineage(image_provider: str) -> dict[str, str]:
    return {
        "currentPriceRub": "mock_enterprise_catalog",
        "latest30dSales": "mock_authorized_seller_report",
        "search_volume": "mock_market_report",
        "competitor_price_avg": "mock_competitor_set",
        "main_image_url": image_provider,
    }


def _preserved_candidates(provider: ImageProvider) -> list[ProductIn]:
    rows = [
        ("demo-ozon-1001", "便携式电动冲牙器", "AquaNova", "电子产品 > 个护电子", 3290, 790, 168, 12.4, 14, 2890, 3380, 42, 35, "approved", "EAC-2026-DEMO"),
        ("demo-ozon-1002", "宠物互动漏食球", "PawJoy", "宠物用品 > 日常护理", 1190, 210, 236, 18.2, 23, 890, 1160, 58, 52, "pending", ""),
        ("demo-ozon-1003", "儿童磁力积木套装", "MagiKids", "电子产品 > 益智设备", 4590, 1380, 74, -4.8, 31, 3190, 4210, 78, 72, "rejected", "CERT-MISMATCH"),
        ("demo-ozon-1004", "真空压缩收纳袋 8件套", "HomeFold", "收纳用品 > 家庭收纳", 1890, 430, 312, 7.6, 42, 1290, 1820, 75, 69, "approved", "MATERIAL-DEMO"),
        ("demo-ozon-1005", "汽车座椅缝隙收纳盒", "RoadMate", "汽车用品 > 车内配件", 2490, 620, 118, 4.0, 17, 1690, 2380, 46, 40, "approved", "MATERIAL-DEMO"),
        ("demo-ozon-1006", "硅胶空气炸锅垫 2件套", "CookEase", "厨房用品 > 厨房工具", 990, 150, 421, 3.2, 56, 690, 970, 88, 82, "approved", "FOOD-CONTACT-DEMO"),
    ]
    products: list[ProductIn] = []
    for index, (external_id, name, brand, category, price, cost, sales, growth, competitors, competitor_min, competitor_avg, saturation, pressure, compliance, certificate) in enumerate(rows):
        image = provider.for_category(category, index)
        products.append(ProductIn(
            captured_at=datetime.now(UTC),
            external_product_id=external_id,
            title=name,
            brand=brand,
            category_path=category,
            url=_reference_url(name),
            main_image_url=image.url,
            image_urls=[image.url],
            seller_name="Mock Enterprise Store",
            current_price=price,
            regular_price=round(price * 1.18, 2),
            competitor_price_min=competitor_min,
            competitor_price_avg=competitor_avg,
            rating=round(4.4 + (index % 5) * 0.1, 1),
            review_count=420 + index * 270,
            latest_30d_sales=sales,
            sales_growth_rate=growth,
            search_volume=18_000 + index * 3_700,
            trend_score=62 + index * 4,
            sales_source="mock_authorized_seller_report",
            sales_evidence="Mock Seller 报表：2026-08-01 至 2026-08-31；仅用于本地测试。",
            competitor_count=competitors,
            price_competition_score=pressure,
            market_saturation=saturation,
            competition_evidence=f"Mock 市场样本：人工构造 {competitors} 个同口径竞品用于测试评分差异。",
            visible_lowest_competitor_price=competitor_min,
            compliance_status=compliance,
            compliance_note="Mock 合规审核已通过。" if compliance == "approved" else "Mock 合规材料待人工补充。" if compliance == "pending" else "Mock 证书范围不匹配，已阻断。",
            certificates=[certificate] if certificate else [],
            manual_risk_level="high" if compliance == "rejected" else "medium" if compliance == "pending" else "low",
            procurement_cost=cost,
            shipping_cost=round(price * 0.10, 2),
            platform_fee=35,
            advertising_cost=round(price * (0.07 + index % 3 * 0.015), 2),
            warehousing_cost=round(price * 0.025, 2),
            tax_cost=round(price * 0.035, 2),
            return_loss_reserve=round(price * 0.02, 2),
            other_cost=20,
            platform_commission_rate=0.18,
            target_margin_rate=0.30,
            tags=[category.split(">")[0].strip(), "Mock Dataset", "可解释评分"],
            field_lineage=_base_lineage(image.provider),
            raw_payload={"demo_data": True, "dataset_version": "enterprise-mock-v2-60", "image_notice": image.source_notice, "verified_product_image": image.verified_product_match},
        ))
    return products


def enterprise_demo_candidates(provider: ImageProvider | None = None) -> list[ProductIn]:
    provider = provider or PublicDemoImageProvider()
    products = _preserved_candidates(provider)
    brand_roots = ["Nova", "Urban", "Prime", "Eco", "Smart", "Pro"]
    generated_index = 0
    for category, names in CATEGORY_PRODUCTS.items():
        for name in names:
            generated_index += 1
            index = generated_index
            price = 790 + (index * 317) % 4_600
            cost_ratio = [0.22, 0.30, 0.38, 0.48, 0.58][index % 5]
            cost = round(price * cost_ratio, 2)
            competitor_avg = round(price * [0.86, 0.95, 1.03, 1.12][index % 4], 2)
            competitor_min = round(competitor_avg * (0.72 + (index % 3) * 0.05), 2)
            compliance = "rejected" if index % 17 == 0 else "pending" if index % 11 == 0 else "approved"
            image = provider.for_category(category, index)
            sales = 25 + (index * 47) % 520
            growth = round(-12 + (index * 3.7) % 38, 1)
            competitors = 5 + (index * 7) % 58
            saturation = 24 + (index * 9) % 72
            pressure = 18 + (index * 11) % 78
            product_name = f"{name} {2 + index % 7}件装" if index % 3 == 0 else name
            external_id = f"demo-ozon-{2000 + index}"
            products.append(ProductIn(
                captured_at=datetime.now(UTC),
                external_product_id=external_id,
                title=product_name,
                brand=f"{brand_roots[index % len(brand_roots)]}{category[:1]}Lab",
                category_path=category,
                sku=f"MOCK-{2000 + index}",
                url=_reference_url(product_name),
                main_image_url=image.url,
                image_urls=[image.url],
                seller_name=f"Mock Store {1 + index % 4}",
                current_price=price,
                regular_price=round(price * (1.08 + (index % 4) * 0.05), 2),
                competitor_price_min=competitor_min,
                competitor_price_avg=competitor_avg,
                rating=round(3.8 + (index % 12) * 0.1, 1),
                review_count=35 + (index * 113) % 4_500,
                latest_30d_sales=sales,
                sales_growth_rate=growth,
                search_volume=4_000 + (index * 4_913) % 95_000,
                trend_score=30 + (index * 7) % 68,
                sales_source="mock_authorized_seller_report",
                sales_evidence=f"Mock Seller 月报条目 {external_id}，统计周期 2026-08。",
                competitor_count=competitors,
                price_competition_score=pressure,
                market_saturation=saturation,
                competition_evidence=f"Mock 竞品集 {external_id}：{competitors} 条同类样本，供确定性测试使用。",
                visible_lowest_competitor_price=competitor_min,
                compliance_status=compliance,
                compliance_note="Mock 人工合规审核已通过。" if compliance == "approved" else "Mock 合规材料待补。" if compliance == "pending" else "Mock 禁限售/证书校验失败。",
                certificates=[f"MOCK-CERT-{2000 + index}"] if compliance == "approved" else [],
                manual_risk_level="high" if compliance == "rejected" else "medium" if compliance == "pending" else "low",
                procurement_cost=cost,
                shipping_cost=round(price * (0.07 + index % 4 * 0.015), 2),
                platform_fee=25 + index % 5 * 8,
                advertising_cost=round(price * (0.04 + index % 6 * 0.018), 2),
                warehousing_cost=round(price * (0.015 + index % 3 * 0.01), 2),
                tax_cost=round(price * 0.035, 2),
                return_loss_reserve=round(price * (0.01 + index % 4 * 0.008), 2),
                other_cost=10 + index % 6 * 6,
                platform_commission_rate=0.18,
                target_margin_rate=[0.25, 0.30, 0.35][index % 3],
                lifecycle_status="abandoned" if compliance == "rejected" else "candidate",
                tags=[category.split(">")[0].strip(), "Mock Dataset", "高竞争" if saturation > 70 else "机会观察"],
                field_lineage=_base_lineage(image.provider),
                raw_payload={"demo_data": True, "dataset_version": "enterprise-mock-v2-60", "image_notice": image.source_notice, "verified_product_image": image.verified_product_match, "abandonReason": "合规样本阻断" if compliance == "rejected" else ""},
            ))
            if len(products) == 60:
                return products
    return products[:60]
