(() => {
  const clamp = (value, low = 0, high = 100) => Math.min(Math.max(value, low), high);
  const round = (value, digits = 2) => {
    const multiplier = 10 ** digits;
    return Math.round((Number(value) + Number.EPSILON) * multiplier) / multiplier;
  };
  const num = (value, fallback = 0) => {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  };
  const safeText = (value) => String(value || "").trim();
  const median = (values) => {
    const sorted = values.filter((value) => Number.isFinite(value) && value > 0).sort((a, b) => a - b);
    if (!sorted.length) return 0;
    const mid = Math.floor(sorted.length / 2);
    return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
  };
  const percentile = (values, fraction) => {
    const sorted = values.filter((value) => Number.isFinite(value) && value >= 0).sort((a, b) => a - b);
    if (!sorted.length) return 0;
    const index = (sorted.length - 1) * fraction;
    const low = Math.floor(index);
    const high = Math.ceil(index);
    return low === high ? sorted[low] : sorted[low] + (sorted[high] - sorted[low]) * (index - low);
  };
  const rankPercent = (value, values, ascending = true) => {
    const valid = values.filter((item) => Number.isFinite(item));
    if (valid.length <= 1 || !Number.isFinite(value)) return 50;
    const sorted = [...valid].sort((a, b) => ascending ? a - b : b - a);
    const first = sorted.findIndex((item) => item === value);
    const last = sorted.length - 1 - [...sorted].reverse().findIndex((item) => item === value);
    return ((first + last + 2) / (2 * sorted.length)) * 100;
  };
  const uuid = () => `ozon-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const normalizedUrl = (value) => safeText(value).replace(/[?#].*$/, "").replace(/\/+$/, "");
  const masterKeyFor = (input) => {
    const productId = safeText(input.ozonProductId || input.productId);
    if (productId) return `ozon-product:${productId}`;
    const url = normalizedUrl(input.url);
    return url ? `ozon-url:${url}` : "";
  };

  const normalize = (input, settings = {}) => ({
    id: safeText(input.id) || uuid(),
    masterKey: safeText(input.masterKey) || masterKeyFor(input),
    url: safeText(input.url),
    title: safeText(input.title) || "未命名商品",
    ozonProductId: safeText(input.ozonProductId || input.productId),
    brand: safeText(input.brand),
    brandSource: safeText(input.brandSource) || "待确认",
    category: safeText(input.categoryPath || input.category) || "类目待确认",
    categoryPath: safeText(input.categoryPath || input.category) || "类目待确认",
    categorySource: safeText(input.categorySource) || "待确认",
    image: safeText(input.imageUrl || input.image),
    imageUrl: safeText(input.imageUrl || input.image),
    priceRub: num(input.priceRub, 0),
    currentPriceRub: num(input.currentPriceRub || input.priceRub, 0),
    regularPriceRub: num(input.regularPriceRub, 0),
    priceSource: safeText(input.priceSource) || "待确认",
    monthlySales: num(input.monthlySales, 0),
    salesSource: safeText(input.salesSource) || "未提供",
    salesEvidence: safeText(input.salesEvidence),
    salesGrowthRate: num(input.salesGrowthRate, 0),
    listingStatus: safeText(input.listingStatus) || "候选",
    rating: num(input.rating, 0),
    reviewCount: num(input.reviewCount, 0),
    estimatedCostRub: num(input.estimatedCostRub, 0),
    shippingCostRub: num(input.shippingCostRub, 0),
    adCostRub: num(input.adCostRub, 0),
    commissionRate: num(input.commissionRate, num(settings.defaultCommission, 0.18)),
    competitorCount: num(input.competitorCount, 0),
    competitionEvidence: safeText(input.competitionEvidence),
    visibleCompetitors: Array.isArray(input.visibleCompetitors) ? input.visibleCompetitors.filter((item) => item && item.selected && num(item.priceRub) > 0).map((item) => ({ kind: safeText(item.kind), title: safeText(item.title), ozonProductId: safeText(item.ozonProductId), priceRub: num(item.priceRub), source: safeText(item.source), url: safeText(item.url), pageText: safeText(item.pageText), capturedAt: item.capturedAt || "" })) : [],
    visibleLowestCompetitorPrice: num(input.visibleLowestCompetitorPrice, 0),
    complianceStatus: safeText(input.complianceStatus) || "待核验",
    complianceEvidence: safeText(input.complianceEvidence),
    tags: Array.isArray(input.tags) ? input.tags.map(safeText).filter(Boolean) : safeText(input.tags).split(/[,，]/).map(safeText).filter(Boolean),
    relationshipType: safeText(input.relationshipType) || "无",
    relatedProductId: safeText(input.relatedProductId),
    relationshipEvidence: safeText(input.relationshipEvidence),
    createdAt: input.createdAt || new Date().toISOString(),
    updatedAt: new Date().toISOString(),
  });

  const statusFor = (score, margin, complianceStatus, evidenceReady) => {
    if (complianceStatus === "未通过") return "合规阻断";
    if (!evidenceReady) return "待补数据";
    if (margin < 0) return "谨慎进入";
    if (score >= 75) return "优先进入";
    if (score >= 60) return "建议验证";
    if (score >= 40) return "谨慎进入";
    return "暂不建议";
  };

  const calculateOne = (raw, allRaw = [], settings = {}) => {
    const targetMargin = num(settings.targetMargin, 0.30);
    const record = normalize(raw, settings);
    const sameCategory = allRaw.map((item) => normalize(item, settings)).filter((item) => item.category === record.category);
    const pool = sameCategory.length ? sameCategory : [record];
    const prices = pool.map((item) => item.priceRub).filter((value) => value > 0);
    const visibleCompetitorPrices = record.visibleCompetitors.map((item) => item.priceRub).filter((value) => value > 0);
    const explicitVisibleLowest = record.visibleLowestCompetitorPrice > 0 ? record.visibleLowestCompetitorPrice : (visibleCompetitorPrices.length ? Math.min(...visibleCompetitorPrices) : 0);
    const priceReferenceValues = [...prices, ...visibleCompetitorPrices];
    const salesValues = pool.map((item) => item.monthlySales);
    const reviewValues = pool.map((item) => item.reviewCount);
    const competitorValues = pool.map((item) => item.competitorCount).filter((value) => value > 0);
    const marketMedian = median(priceReferenceValues);
    const marketQ40 = percentile(priceReferenceValues, 0.40);
    const marketQ75 = percentile(priceReferenceValues, 0.75);
    const allCost = record.estimatedCostRub + record.shippingCostRub + record.adCostRub;
    const commission = clamp(record.commissionRate, 0, 0.9);
    const unitProfit = record.priceRub > 0 ? record.priceRub * (1 - commission) - allCost : 0;
    const netMargin = record.priceRub > 0 ? unitProfit / record.priceRub : 0;
    const breakEvenPrice = commission < 1 ? allCost / (1 - commission) : 0;
    const marginDenominator = 1 - commission - targetMargin;
    const targetFloor = marginDenominator > 0 ? allCost / marginDenominator : 0;
    const suggestedBase = Math.max(targetFloor, marketQ40 || 0, breakEvenPrice || 0);
    const recommendedPrice = suggestedBase > 0 ? Math.round(suggestedBase / 10) * 10 : 0;

    const salesPercentile = rankPercent(record.monthlySales, salesValues, true);
    const reviewsPercentile = rankPercent(record.reviewCount, reviewValues, true);
    const competitorsForRank = competitorValues.length ? competitorValues : [record.competitorCount];
    const competitionPercentile = rankPercent(record.competitorCount, competitorsForRank, true);
    const demandScore = 0.75 * salesPercentile + 0.25 * reviewsPercentile;
    let priceScore = marketMedian > 0 ? clamp(100 - Math.abs(record.priceRub / marketMedian - 0.96) * 125) : 50;
    if (prices.length > 3 && record.priceRub < percentile(prices, 0.25)) priceScore *= 0.85;
    if (explicitVisibleLowest > 0 && record.priceRub > explicitVisibleLowest * 1.15) priceScore *= 0.85;
    const marginScore = clamp((netMargin / targetMargin) * 100);
    const competitionScore = 100 - competitionPercentile;
    const ratingScore = record.rating > 0 ? clamp(((record.rating - 3.5) / 1.5) * 100) : 50;
    const reputationScore = 0.7 * ratingScore + 0.3 * reviewsPercentile;
    const salesPerCompetitor = record.competitorCount > 0 ? record.monthlySales / record.competitorCount : record.monthlySales;
    const marketSpaceScore = pool.length <= 1 ? 50 : 0.6 * rankPercent(record.monthlySales, salesValues, true) + 0.4 * rankPercent(salesPerCompetitor, pool.map((item) => item.competitorCount > 0 ? item.monthlySales / item.competitorCount : item.monthlySales), true);
    const financialScore = 0.5 * priceScore + 0.5 * marginScore;
    const complianceScore = record.complianceStatus === "通过" ? 100 : 0;
    const salesEvidenceReady = record.monthlySales > 0 && record.salesSource !== "未提供" && Boolean(record.salesEvidence);
    const competitionEvidenceReady = (record.competitorCount > 0 || record.visibleCompetitors.length > 0) && Boolean(record.competitionEvidence || record.visibleCompetitors.length);
    const evidenceReady = salesEvidenceReady && competitionEvidenceReady && record.estimatedCostRub > 0 && record.complianceStatus === "通过";
    const selectionScore = round(
      0.30 * demandScore + 0.30 * financialScore + 0.25 * complianceScore + 0.15 * competitionScore,
      1,
    );

    const risks = [];
    if (record.priceRub <= 0) risks.push("未识别到有效售价，请手工录入后重新计算。");
    if (record.estimatedCostRub <= 0) risks.push("尚未填写采购或到仓成本，利润结论不可用于进货决策。");
    if (record.priceRub > 0 && netMargin < 0) risks.push("当前售价低于保本线，单件预计亏损。");
    else if (record.priceRub > 0 && netMargin < 0.15) risks.push("净利率低于 15%，不足以抵御折扣、退货和广告波动。");
    if (marketQ75 > 0 && recommendedPrice > marketQ75) risks.push("达到目标净利率所需售价高于当前候选池的 75 分位价格，存在价格竞争风险。");
    if (explicitVisibleLowest > 0 && record.priceRub > explicitVisibleLowest * 1.15) risks.push(`当前售价比已选当前可见最低竞品价 ${Math.round(explicitVisibleLowest)} RUB 高出超过 15%；需核对差异化、规格与履约口径。`);
    if (pool.length > 3 && competitionPercentile >= 75) risks.push("竞品数量在当前候选池中偏高，需验证差异化和投放效率。");
    if (!salesEvidenceReady) risks.push("销量缺少已授权来源或核验证据：不得将该数字视为 Ozon 平台事实。");
    else if (pool.length > 3 && salesPercentile <= 25) risks.push("销量在当前候选池中偏低，建议先小批量验证需求。");
    if (record.rating > 0 && record.rating < 4.2) risks.push("评分低于 4.2，建议复核质量、详情页描述和售后问题。");
    if (record.complianceStatus === "未通过") risks.push("合规状态为未通过：该候选不得进入已通过流程。");
    else if (record.complianceStatus !== "通过") risks.push("合规尚未通过：需补充禁限售、认证、标签、知识产权和运输限制核验。");
    if (!competitionEvidenceReady) risks.push("未填写竞争证据说明：竞品数量不得被视为已核验的市场结论。");
    if (!risks.length) risks.push("未触发关键量化风险，仍需复核库存、合规和数据口径。");

    const strengths = [];
    if (pool.length > 1 && salesPercentile >= 75) strengths.push("销量位于当前候选池前四分位，需求信号较强。");
    if (record.priceRub > 0 && netMargin >= targetMargin) strengths.push(`净利率达到 ${Math.round(targetMargin * 100)}% 目标线。`);
    if (priceScore >= 75) strengths.push("价格接近候选池的竞争性价格带。");
    if (pool.length > 3 && competitionScore >= 75) strengths.push("竞品数量相对可控。");
    if (reputationScore >= 75) strengths.push("评分与评论信号较好。");
    if (!strengths.length) strengths.push("暂无明显单项优势，建议补充销量和竞品数据后再比较。\n");

    return {
      ...record,
      commissionRate: commission,
      marketMedian: round(marketMedian),
      marketQ40: round(marketQ40),
      marketQ75: round(marketQ75),
      visibleLowestCompetitorPrice: round(explicitVisibleLowest),
      visibleCompetitorCount: record.visibleCompetitors.length,
      unitProfit: round(unitProfit),
      netMargin: round(netMargin, 4),
      breakEvenPrice: round(breakEvenPrice),
      targetFloor: round(targetFloor),
      recommendedPrice: round(recommendedPrice),
      salesPercentile: round(salesPercentile, 1),
      demandScore: round(demandScore, 1),
      priceScore: round(priceScore, 1),
      marginScore: round(marginScore, 1),
      financialScore: round(financialScore, 1),
      complianceScore: round(complianceScore, 1),
      competitionScore: round(competitionScore, 1),
      reputationScore: round(reputationScore, 1),
      marketSpaceScore: round(marketSpaceScore, 1),
      selectionScore,
      decision: statusFor(selectionScore, netMargin, record.complianceStatus, evidenceReady),
      strengths,
      risks,
      completeness: {
        price: record.priceRub > 0,
        cost: record.estimatedCostRub > 0,
        sales: salesEvidenceReady,
        competitors: competitionEvidenceReady,
        compliance: record.complianceStatus === "通过",
      },
    };
  };

  const analyzePool = (records = [], settings = {}) => {
    const normalized = records.map((record) => normalize(record, settings));
    return normalized
      .map((record) => calculateOne(record, normalized, settings))
      .sort((a, b) => b.selectionScore - a.selectionScore || b.monthlySales - a.monthlySales)
      .map((record, index) => ({ ...record, rank: index + 1 }));
  };

  const toCsv = (records = []) => {
    const columns = [
      "rank", "title", "category", "url", "priceRub", "monthlySales", "rating", "reviewCount",
      "estimatedCostRub", "shippingCostRub", "adCostRub", "commissionRate", "competitorCount", "visibleLowestCompetitorPrice", "visibleCompetitorCount", "competitionEvidence", "complianceStatus", "complianceEvidence",
      "demandScore", "financialScore", "complianceScore", "competitionScore", "unitProfit", "netMargin", "recommendedPrice", "selectionScore", "decision", "risks",
    ];
    const escape = (value) => `"${String(value ?? "").replaceAll('"', '""')}"`;
    const lines = [columns.join(",")].concat(records.map((record) => columns.map((column) => {
      const value = Array.isArray(record[column]) ? record[column].join("；") : record[column];
      return escape(value);
    }).join(",")));
    return "\ufeff" + lines.join("\n");
  };

  globalThis.OzonEngine = { normalize, calculateOne, analyzePool, toCsv, median, rankPercent, masterKeyFor, normalizedUrl };
})();
