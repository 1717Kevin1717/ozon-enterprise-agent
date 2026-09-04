import re
from difflib import SequenceMatcher
from dataclasses import dataclass, field

from app.db.models import Product


def normalize_entity(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (value or "").casefold())


def product_aliases(product: Product) -> tuple[str, ...]:
    title = normalize_entity(product.title)
    base = re.sub(r"\d+(?:件|个|只|套|支|片|枚)装$", "", title)
    aliases = [title, base, normalize_entity(product.external_product_id), normalize_entity(product.sku)]
    return tuple(dict.fromkeys(item for item in aliases if len(item) >= 3))


@dataclass
class EntityResolution:
    products: list[Product] = field(default_factory=list)
    ambiguous: list[dict] = field(default_factory=list)
    method: str = "none"


def resolve_query_entities(products: list[Product], query: str, limit: int = 10) -> EntityResolution:
    normalized_query = normalize_entity(query)
    positions: dict[str, tuple[int, int, Product]] = {}
    alias_map: dict[str, list[Product]] = {}
    for product in products:
        for alias in product_aliases(product):
            if alias in normalized_query:
                alias_map.setdefault(alias, []).append(product)
    ambiguous = []
    for alias, matches in alias_map.items():
        unique = {item.id: item for item in matches}
        if len(unique) > 1:
            ambiguous.append({"alias": alias, "candidates": [{"id": item.id, "title": item.title} for item in unique.values()]})
            continue
        product = next(iter(unique.values()))
        candidate = (normalized_query.find(alias), -len(alias), product)
        current = positions.get(product.id)
        if current is None or candidate[:2] < current[:2]:
            positions[product.id] = candidate
    resolved = [item[2] for item in sorted(positions.values(), key=lambda value: value[:2])][:limit]
    if not resolved and not ambiguous:
        entity_text = normalized_query
        for phrase in ("售价是多少", "价格是多少", "售价多少", "价格多少", "卖多少钱", "现在售价", "利润怎么样", "利润如何", "分析一下", "商品"):
            entity_text = entity_text.replace(normalize_entity(phrase), "")
        scored: list[tuple[float, Product]] = []
        if len(entity_text) >= 4:
            for product in products:
                score = max((SequenceMatcher(None, entity_text, alias).ratio() for alias in product_aliases(product)), default=0)
                if score >= 0.84:
                    scored.append((score, product))
        scored.sort(key=lambda item: item[0], reverse=True)
        if scored and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.08):
            return EntityResolution([scored[0][1]], [], "normalized_unique_similarity")
        if len(scored) > 1:
            return EntityResolution([], [{"alias": entity_text, "candidates": [{"id": item.id, "title": item.title} for _, item in scored[:5]]}], "ambiguous_similarity")
    return EntityResolution(resolved, ambiguous, "normalized_exact" if resolved else "none")
