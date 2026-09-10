import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from app.db.models import Product
from app.schemas.agent import EntityCandidate, EntityResolutionResult


def normalize_entity(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value)


def product_aliases(product: Product) -> tuple[str, ...]:
    title = normalize_entity(product.title)
    base = re.sub(r"\d+(?:件|个|只|套|支|片|枚)装$", "", title)
    return tuple(dict.fromkeys(item for item in (title, base) if item))


# Query grammar, not catalogue-specific aliases. Preserve unknown name qualifiers.
_FIELD_SUFFIX = re.compile(
    r"(?:的)?(?:当前|目前|现在)?(?:为什么|为何|怎么|主要卡在|主要亏|亏在|数据|这个价格|这个指标|售价|价格|卖多少钱|多少钱|风险等级|风险级别|"
    r"净利润|净利率|利润率|利润|roi|销量|有(?:多少|几).*?快照|是否|能不能|值得|详情|怎么样|如何|哪个好|哪个更好|中(?:选出|挑选|选择)|选出)", re.I
)
_REFERENCE = re.compile(r"(?:(?:和|与)?(?:刚才|当前|已选|那个|这个|那两个|这两个|这几个|这些|我选的这些|这\d+个|这四个|它|对比中心|第[一二两三四五六七八九十\d]+个|前一个|后一个|那利润|现在比较)(?:那|这|的)?(?:几个|两个)?(?:商品|产品|候选)?(?:相比|一下|呢)?)\Z")
_GENERIC = {"商品", "产品", "用品", "电子产品", "桌面", "收纳", "东西"}


def query_mentions(query: str, protected_names: tuple[str, ...] = ()) -> list[str]:
    text = query.strip()
    protected = {}
    for name in sorted(set(protected_names), key=len, reverse=True):
        if name and name in text:
            marker = f"__ENTITY_{len(protected)}__"
            text = text.replace(name, marker)
            protected[marker] = name
    # Percent values in metric questions are not model/pack-size digits. Names
    # already protected above retain legitimate 65W / 4件装 specifications.
    text = re.sub(r"(?:的)?\s*[+-]?\d+(?:\.\d+)?\s*[%％]", "", text)
    text = re.sub(r"^(?:先别看|不要看|不看|忽略|排除)[^,，;；]+[,，;；]\s*", "", text)
    text = re.sub(r"^(?:(?:请问|请|帮我|帮忙|查看|查询|分析一下|分析|比较|对比|如果|假如|把|将|从|换成|改成)\s*)+", "", text)
    text = _FIELD_SUFFIX.split(text, maxsplit=1)[0]
    text = re.split(r"[,，;；]", text, maxsplit=1)[0].strip(" 的：:。？！?!\"“”")
    text = re.sub(r"(?:这|那)(?:个|条|项)$", "", text).rstrip(" 的")
    if not text or _REFERENCE.fullmatch(text) or text in {"候选池", "企业商品库", "公司商品库"}:
        return []
    mentions = re.split(r"以及|和|与|、|\s+vs\.?\s+", text, flags=re.I)
    restored = []
    for part in mentions:
        part = part.strip(" 的呢：:。？！?!\"“”")
        for marker, name in protected.items():
            part = part.replace(marker, name)
        if part and not _REFERENCE.fullmatch(part) and not re.search(
            r"^(?:不是|我主要看|我想看|这几个里面谁|这些商品(?:里)?谁|现在比较|用对比中心|排第[一二两三四五六七八九十\d]+个|如果只能留一个|一个商品|某个商品|先别看)", part
        ):
            restored.append(part)
    return list(dict.fromkeys(restored))


@dataclass
class EntityResolution:
    products: list[Product] = field(default_factory=list)
    requested_entities: list[EntityResolutionResult] = field(default_factory=list)

    @property
    def has_unresolved(self) -> bool:
        return any(not item.resolved for item in self.requested_entities)

    @property
    def ambiguous(self) -> list[dict]:
        return [item.model_dump() for item in self.requested_entities if item.status == "AMBIGUOUS"]


def _result(mention: str, matches: list[tuple[float, Product]], status: str) -> EntityResolutionResult:
    if not matches:
        return EntityResolutionResult(mention=mention, status="NOT_FOUND")
    score, product = matches[0]
    if status not in {"AMBIGUOUS", "LOW_CONFIDENCE"} and len(matches) == 1:
        return EntityResolutionResult(mention=mention, status=status, product_id=product.id, name=product.title, confidence=score)
    return EntityResolutionResult(
        mention=mention, status="LOW_CONFIDENCE" if status == "LOW_CONFIDENCE" else "AMBIGUOUS", confidence=score,
        candidates=[EntityCandidate(product_id=item.id, name=item.title, confidence=value) for value, item in matches[:5]],
    )


def resolve_entity(products: list[Product], mention: str) -> EntityResolutionResult:
    # IDs are exact identities, never fuzzy aliases. Callers supply tenant-scoped products.
    ids = [p for p in products if mention in {p.id, p.external_product_id, p.sku}]
    exact = ids or [p for p in products if mention == p.title]
    if exact:
        return _result(mention, [(1.0, p) for p in exact], "EXACT_MATCH")
    normalized = normalize_entity(mention)
    exact = [p for p in products if normalized == normalize_entity(p.title)]
    if exact:
        return _result(mention, [(0.99, p) for p in exact], "NORMALIZED_MATCH")
    generic = normalized in _GENERIC or any(token in normalized for token in ("那个", "这个", "某个"))
    lookup = re.sub(r"那个|这个|某个", "", normalized)
    # Only shorten toward a meaningful name suffix, never discard user qualifiers
    # or match a base product to an accessory having a different trailing head.
    aliases = [p for p in products if any(alias.endswith(lookup) for alias in product_aliases(p))] if len(lookup) >= 3 else []
    if aliases and not generic:
        return _result(mention, [(0.95, p) for p in aliases], "UNIQUE_ALIAS_MATCH")
    if generic:
        possible = [p for p in products if lookup and lookup in normalize_entity(p.title)]
        return _result(mention, [(0.5, p) for p in possible], "LOW_CONFIDENCE")
    scored = []
    if len(normalized) >= 4:
        for product in products:
            # Numbers identify models/variants; a different model is not a typo.
            scores = []
            for alias in product_aliases(product):
                if re.findall(r"\d+", normalized) != re.findall(r"\d+", alias):
                    continue
                matcher = SequenceMatcher(None, normalized, alias)
                edits = sum(max(end_a - start_a, end_b - start_b) for tag, start_a, end_a, start_b, end_b in matcher.get_opcodes() if tag != "equal")
                score = matcher.ratio()
                # Dropping several qualifier characters is not a harmless one-character typo.
                scores.append(min(score, 0.81) if edits > 1 and score < 0.9 else score)
            score = max(scores, default=0)
            if score >= 0.65:
                scored.append((score, product))
    scored.sort(key=lambda item: (-item[0], item[1].title, item[1].id))
    high = [item for item in scored if item[0] >= 0.82]
    if high and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.08):
        return _result(mention, high[:1], "FUZZY_UNIQUE_MATCH")
    if len(high) > 1:
        return _result(mention, high, "AMBIGUOUS")
    return _result(mention, scored, "LOW_CONFIDENCE")


def resolve_query_entities(products: list[Product], query: str, limit: int = 10) -> EntityResolution:
    names = tuple(value for p in products for value in (p.title, p.id, p.external_product_id, p.sku) if value)
    results = [resolve_entity(products, mention) for mention in query_mentions(query, names)]
    by_id = {p.id: p for p in products}
    resolved_ids = list(dict.fromkeys(item.product_id for item in results if item.resolved))
    return EntityResolution([by_id[pid] for pid in resolved_ids[:limit]], results)


def resolution_message(resolution: EntityResolution, *, comparison: bool = False) -> str:
    parts = []
    for item in resolution.requested_entities:
        if item.resolved:
            parts.append(f"‘{item.mention}’已识别为 {item.name}")
        elif item.status == "NOT_FOUND":
            parts.append(f"企业商品库中未找到‘{item.mention}’")
        elif item.status == "AMBIGUOUS":
            parts.append(f"‘{item.mention}’对应多个可能商品，请选择：" + "、".join(c.name for c in item.candidates))
        else:
            parts.append(f"‘{item.mention}’的匹配置信度不足，请确认是否指：" + "、".join(c.name for c in item.candidates))
    if comparison:
        parts.append("因此本次未执行商品比较，请先确认未解析的商品")
    return "；".join(parts) + "。"
