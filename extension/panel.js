const $ = (id) => document.getElementById(id);
const fieldIds = [
  "title", "ozonProductId", "brand", "brandSource", "category", "categorySource", "priceRub", "regularPriceRub", "priceSource", "monthlySales", "salesSource", "salesEvidence", "salesGrowthRate", "listingStatus", "rating", "reviewCount", "imageUrl",
  "estimatedCostRub", "shippingCostRub", "adCostRub", "commissionRate", "competitorCount", "competitionEvidence", "visibleLowestCompetitorPrice", "complianceStatus", "complianceEvidence", "tags", "relationshipType", "relatedProductId", "relationshipEvidence", "url",
];

let records = [];
let settings = { targetMargin: 0.30, defaultCommission: 0.18, backendUrl: "http://127.0.0.1:8000" };
let editingId = null;
let visibleCompetitors = [];
let competitorEvidenceBase = "";

const html = (value) => String(value ?? "").replace(/[&<>'"]/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#039;", "\"": "&quot;",
}[char]));
const num = (value) => Number.isFinite(Number(value)) ? Number(value) : 0;
const money = (value) => value === null || value === undefined ? "—" : `${Math.round(num(value)).toLocaleString("zh-CN")} RUB`;
const percentage = (value) => `${(num(value) * 100).toFixed(1)}%`;
const statusClass = (decision) => decision || "暂不建议";

function setNotice(message = "", type = "") {
  const box = $("notice");
  box.textContent = message;
  box.className = `notice ${message ? type : ""}`;
}

function formSettings() {
  return {
    targetMargin: Math.min(Math.max(num($("targetMargin").value) / 100, 0.01), 0.79),
    defaultCommission: Math.min(Math.max(num($("commissionRate").value) / 100, 0), 0.89),
  };
}

function readForm() {
  const raw = {};
  fieldIds.forEach((id) => { raw[id] = $(id).value; });
  return {
    id: editingId || undefined,
    title: raw.title,
    ozonProductId: raw.ozonProductId,
    brand: raw.brand,
    brandSource: raw.brandSource,
    category: raw.category,
    categoryPath: raw.category,
    categorySource: raw.categorySource,
    priceRub: raw.priceRub,
    currentPriceRub: raw.priceRub,
    regularPriceRub: raw.regularPriceRub,
    priceSource: raw.priceSource,
    monthlySales: raw.monthlySales,
    salesSource: raw.salesSource,
    salesEvidence: raw.salesEvidence,
    salesGrowthRate: raw.salesGrowthRate,
    listingStatus: raw.listingStatus,
    rating: raw.rating,
    reviewCount: raw.reviewCount,
    imageUrl: raw.imageUrl,
    estimatedCostRub: raw.estimatedCostRub,
    shippingCostRub: raw.shippingCostRub,
    adCostRub: raw.adCostRub,
    commissionRate: num(raw.commissionRate) / 100,
    competitorCount: raw.competitorCount,
    competitionEvidence: raw.competitionEvidence,
    visibleLowestCompetitorPrice: raw.visibleLowestCompetitorPrice,
    visibleCompetitors,
    complianceStatus: raw.complianceStatus,
    complianceEvidence: raw.complianceEvidence,
    tags: raw.tags,
    relationshipType: raw.relationshipType,
    relatedProductId: raw.relatedProductId,
    relationshipEvidence: raw.relationshipEvidence,
    url: raw.url,
  };
}

function fillForm(record = {}) {
  editingId = record.id || null;
  $("title").value = record.title || "";
  $("ozonProductId").value = record.ozonProductId || record.productId || "";
  $("brand").value = record.brand || "";
  $("brandSource").value = record.brandSource || "待确认";
  $("category").value = record.category || "";
  $("categorySource").value = record.categorySource || "待确认";
  $("priceRub").value = record.priceRub ?? "";
  $("regularPriceRub").value = record.regularPriceRub ?? "";
  $("priceSource").value = record.priceSource || "待确认";
  $("monthlySales").value = record.monthlySales ?? "";
  $("salesSource").value = record.salesSource || "未提供";
  $("salesEvidence").value = record.salesEvidence || "";
  $("salesGrowthRate").value = record.salesGrowthRate ?? "";
  $("listingStatus").value = record.listingStatus || "候选";
  $("rating").value = record.rating || "";
  $("reviewCount").value = record.reviewCount ?? "";
  $("imageUrl").value = record.imageUrl || record.image || "";
  $("estimatedCostRub").value = record.estimatedCostRub ?? "";
  $("shippingCostRub").value = record.shippingCostRub ?? 0;
  $("adCostRub").value = record.adCostRub ?? 0;
  $("commissionRate").value = record.commissionRate !== undefined ? Math.round(num(record.commissionRate) * 100) : Math.round(settings.defaultCommission * 100);
  $("competitorCount").value = record.competitorCount ?? "";
  competitorEvidenceBase = String(record.competitionEvidence || "").replace(/；?用户已从当前可见 Ozon 页面选择\s*\d+\s*条竞品\/其他卖家条目；最低可见价\s*[^；。]*RUB。?/g, "").trim();
  $("competitionEvidence").value = competitorEvidenceBase;
  $("visibleLowestCompetitorPrice").value = record.visibleLowestCompetitorPrice ?? 0;
  $("complianceStatus").value = record.complianceStatus || "待核验";
  $("complianceEvidence").value = record.complianceEvidence || "";
  $("tags").value = Array.isArray(record.tags) ? record.tags.join(",") : (record.tags || "");
  $("relationshipType").value = record.relationshipType || "无";
  $("relatedProductId").value = record.relatedProductId || "";
  $("relationshipEvidence").value = record.relationshipEvidence || "";
  $("url").value = record.url || "";
  visibleCompetitors = Array.isArray(record.visibleCompetitors) ? record.visibleCompetitors : [];
  renderVisibleCompetitors();
}

function selectedVisibleCompetitors() {
  return visibleCompetitors.filter((item) => item && item.selected && num(item.priceRub) > 0);
}

function visibleCompetitorEvidence(selected) {
  const lowest = selected.length ? Math.min(...selected.map((item) => num(item.priceRub)).filter((value) => value > 0)) : 0;
  const sentence = selected.length ? `用户已从当前可见 Ozon 页面选择 ${selected.length} 条竞品/其他卖家条目；最低可见价 ${lowest || "未识别"} RUB。` : "";
  return { lowest, sentence };
}

function syncSelectedCompetitorMetrics() {
  const selected = selectedVisibleCompetitors();
  const { lowest, sentence } = visibleCompetitorEvidence(selected);
  $("competitorCount").value = selected.length;
  $("visibleLowestCompetitorPrice").value = lowest || 0;
  $("competitionEvidence").value = sentence ? (competitorEvidenceBase ? `${competitorEvidenceBase}；${sentence}` : sentence) : competitorEvidenceBase;
  return { selected, lowest };
}

function renderVisibleCompetitors() {
  const list = $("visibleCompetitorList");
  if (!visibleCompetitors.length) {
    list.innerHTML = '<div class="comp-empty">点击“读取当前可见竞品”后，仅展示当前页面可见的相似商品和其他卖家价格提示。</div>';
    return;
  }
  list.innerHTML = visibleCompetitors.map((entry, index) => `
    <label class="competitor-item"><input type="checkbox" data-competitor-index="${index}" ${entry.selected ? "checked" : ""} />
      <span><b>${html(entry.kind === "other_seller_offer" ? "其他卖家提示" : (entry.title || "相似商品"))}</b>
      <small>${html(money(entry.priceRub))} · ${html(entry.source || "当前可见页面")} · ${html(entry.ozonProductId ? `Ozon ID: ${entry.ozonProductId}` : "无独立商品 ID")}</small></span>
    </label>`).join("");
  list.querySelectorAll("[data-competitor-index]").forEach((box) => {
    box.addEventListener("change", () => {
      const index = Number(box.dataset.competitorIndex);
      if (visibleCompetitors[index]) visibleCompetitors[index] = { ...visibleCompetitors[index], selected: box.checked };
      const { selected, lowest } = syncSelectedCompetitorMetrics();
      setNotice(selected.length ? `已自动回填：同质竞品数 ${selected.length}；当前可见最低竞品价 ${lowest} RUB。点击“写入竞品集合”后将其确认为同步证据。` : "已取消选择，竞品数量和最低可见价已清空。", selected.length ? "success" : "");
    });
  });
}

function clearForm() {
  editingId = null;
  fillForm({ commissionRate: settings.defaultCommission, shippingCostRub: 0, adCostRub: 0 });
  $("sourceInfo").textContent = "已清空表单，可以手工添加一个候选商品。";
  $("analysisCard").hidden = true;
  setNotice();
}

async function persist() {
  await chrome.storage.local.set({ records, settings });
}

function buildEnterprisePacket(raw) {
  const normalized = globalThis.OzonEngine.normalize(raw, settings);
  return {
    protocol: "zmt-ozon-product-master/v2", operation: "upsert_product_master", schemaVersion: 2,
    capturedAt: new Date().toISOString(), captureScope: "user_current_visible_page",
    snapshot: {
      ...normalized, categoryPath: raw.category, priceRub: num(raw.priceRub), currentPriceRub: num(raw.priceRub),
      regularPriceRub: num(raw.regularPriceRub), monthlySales: num(raw.monthlySales), rating: num(raw.rating),
      reviewCount: num(raw.reviewCount), competitorCount: num(raw.competitorCount),
      prices: { currentDisplayPriceRub: num(raw.priceRub), regularPriceRub: num(raw.regularPriceRub) },
      relations: raw.relationshipType && raw.relationshipType !== "无" && raw.relatedProductId ? [{ type: raw.relationshipType, targetOzonProductId: raw.relatedProductId, evidence: raw.relationshipEvidence }] : [],
      visibleCompetitors: (raw.visibleCompetitors || []).filter((item) => item && item.selected).map((item) => ({ ...item, selected: true })),
      visibleLowestCompetitorPriceRub: num(raw.visibleLowestCompetitorPrice),
    },
    master: {
      masterKey: normalized.masterKey,
      identity: { ozonProductId: normalized.ozonProductId, url: normalized.url, title: normalized.title },
      catalog: { brand: normalized.brand, brandSource: normalized.brandSource, categoryPath: normalized.categoryPath, categorySource: normalized.categorySource, tags: normalized.tags },
    },
    fieldLineage: {
      title: "ozon_current_visible_page_or_user_confirmed", ozonProductId: "ozon_current_visible_page_or_user_confirmed",
      brand: raw.brandSource || "enterprise_or_ozon_visible_source_required", categoryPath: "ozon_visible_breadcrumb_or_user_confirmed",
      currentPriceRub: "ozon_visible_current_display_price_or_user_confirmed", regularPriceRub: "ozon_visible_strikethrough_price_or_user_confirmed",
      rating: "ozon_current_visible_page_or_user_confirmed", reviewCount: "ozon_current_visible_page_or_user_confirmed",
      monthlySales: raw.salesSource === "Ozon Seller 已授权统计" || raw.salesSource === "Ozon Statistics API 已授权数据" ? "authorized_ozon_sales_data" : "enterprise_manual_verified_evidence_required",
      salesEvidence: "enterprise_or_authorized_data_required", competitorCount: "manual_verified_competition_evidence_required",
      estimatedCostRub: "enterprise_operating_data", complianceStatus: "enterprise_compliance_review",
      visibleCompetitors: "user_selected_from_ozon_current_visible_page", visibleLowestCompetitorPriceRub: "minimum_of_user_selected_current_visible_competitor_entries",
    },
  };
}

function analyzedPool() {
  return globalThis.OzonEngine.analyzePool(records, settings);
}

function currentAnalyzed(id) {
  return analyzedPool().find((record) => record.id === id);
}

function renderAnalysis(record) {
  if (!record) return;
  $("analysisCard").hidden = false;
  $("score").textContent = `${record.selectionScore.toFixed(1)}`;
  $("decision").innerHTML = `<span class="tag ${statusClass(record.decision)}">${html(record.decision)}</span>`;
  $("scoreText").textContent = `候选池排名 ${record.rank || "—"}；需求 30%、利润价格带 30%、合规 25%、竞争 15%。合规或关键证据缺失会阻断推荐。`;
  $("unitProfit").textContent = money(record.unitProfit);
  $("netMargin").textContent = percentage(record.netMargin);
  $("recommendedPrice").textContent = money(record.recommendedPrice);
  $("breakEvenPrice").textContent = money(record.breakEvenPrice);
  $("marketMedian").textContent = record.marketMedian ? money(record.marketMedian) : "候选不足";
  $("priceScore").textContent = `${record.priceScore.toFixed(1)} / 100`;
  $("demandScore").textContent = `${record.demandScore.toFixed(1)} / 100`;
  $("financialScore").textContent = `${record.financialScore.toFixed(1)} / 100`;
  $("complianceScore").textContent = `${record.complianceScore.toFixed(1)} / 100`;
  $("competitionScore").textContent = `${record.competitionScore.toFixed(1)} / 100`;
  $("strengths").innerHTML = record.strengths.map((item) => `<li>${html(item)}</li>`).join("");
  $("risks").innerHTML = record.risks.map((item) => `<li>${html(item)}</li>`).join("");
}

function renderCandidates() {
  const pool = analyzedPool();
  $("poolSummary").textContent = pool.length ? `已保存 ${pool.length} 个候选商品，按综合选品分排序。` : "尚未保存候选商品。";
  const list = $("candidateList");
  if (!pool.length) {
    list.className = "empty";
    list.textContent = "采集当前页并保存后，商品会在这里按综合选品分排序。";
    return;
  }
  list.className = "";
  list.innerHTML = pool.map((record) => `
    <div class="candidate" data-id="${html(record.id)}" title="点击载入并编辑">
      <div><div class="title">${record.rank}. ${html(record.title)}</div><small>${html(record.category)} · ${money(record.priceRub)} · 净利率 ${percentage(record.netMargin)}</small></div>
      <div class="right"><b>${record.selectionScore.toFixed(1)}</b><br><span class="tag ${statusClass(record.decision)}">${html(record.decision)}</span></div>
    </div>
  `).join("");
  list.querySelectorAll(".candidate").forEach((node) => {
    node.addEventListener("click", () => {
      const raw = records.find((item) => item.id === node.dataset.id);
      if (!raw) return;
      fillForm(raw);
      $("sourceInfo").textContent = "已从本地候选池载入。修改后点击“保存并分析”即可更新。";
      renderAnalysis(currentAnalyzed(raw.id));
      setNotice("已载入候选商品，可继续修改成本或市场字段。", "success");
    });
  });
}

async function captureCurrentPage() {
  setNotice();
  const button = $("captureBtn");
  button.disabled = true;
  button.textContent = "采集中…";
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id || !/^https:\/\/(www\.)?ozon\.ru\//i.test(tab.url || "")) {
      throw new Error("请先在当前窗口正常打开一个 Ozon 商品详情页，再点击“采集当前页”。");
    }
    let response;
    try {
      response = await chrome.tabs.sendMessage(tab.id, { type: "OZON_CAPTURE_CURRENT_PAGE" });
    } catch (_) {
      await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["extractor.js"] });
      response = await chrome.tabs.sendMessage(tab.id, { type: "OZON_CAPTURE_CURRENT_PAGE" });
    }
    if (!response?.ok) throw new Error(response?.error || "页面信息读取失败。");
    const product = response.product || {};
    const existing = readForm();
    fillForm({
      ...existing,
        title: product.title || existing.title,
        ozonProductId: product.ozonProductId || existing.ozonProductId,
        brand: product.brand || existing.brand,
        brandSource: product.brandSource || existing.brandSource,
        category: product.categoryPath || product.category || existing.category,
        categorySource: product.categorySource || existing.categorySource,
        priceRub: product.priceRub || existing.priceRub,
        regularPriceRub: product.regularPriceRub || existing.regularPriceRub,
        priceSource: product.priceSource || existing.priceSource,
        rating: product.rating || existing.rating,
        reviewCount: product.reviewCount || existing.reviewCount,
      imageUrl: product.image || existing.imageUrl,
      url: product.url || tab.url,
    });
    competitorEvidenceBase = $("competitionEvidence").value.trim();
    visibleCompetitors = Array.isArray(product.visibleCompetitors) ? product.visibleCompetitors.map((item) => ({ ...item, selected: false })) : [];
    syncSelectedCompetitorMetrics();
    renderVisibleCompetitors();
    $("sourceInfo").textContent = `标题：${product.source?.title || "页面"}；品牌：${product.source?.brand || "待确认"}；类目：${product.source?.category || "待确认"}；当前展示价：${product.source?.price || "未识别"}；常规价：${product.source?.regularPrice || "未展示"}；评分：${product.source?.rating || "未识别"}。公开商品页不自动填写销量。`;
    setNotice("已采集当前页面可见信息。请确认类目路径和价格口径；销量仅填写已授权统计或人工核验证据。", "success");
  } catch (error) {
    setNotice(error.message || "采集失败。", "error");
  } finally {
    button.disabled = false;
    button.textContent = "采集当前页";
  }
}

async function saveAndAnalyze() {
  setNotice();
  settings = { ...settings, ...formSettings() };
  const raw = readForm();
  if (!raw.title.trim()) return setNotice("请填写商品标题或先采集当前页。", "error");
  if (num(raw.priceRub) <= 0) return setNotice("请填写有效售价（RUB）后再分析。", "error");
  if (num(raw.estimatedCostRub) <= 0) return setNotice("请填写采购 / 到仓成本；否则利润结论不可靠。", "error");
  const normalized = globalThis.OzonEngine.normalize(raw, settings);
  const index = records.findIndex((item) => item.id === normalized.id || (normalized.masterKey && globalThis.OzonEngine.normalize(item, settings).masterKey === normalized.masterKey));
  if (index >= 0) records[index] = { ...records[index], ...normalized, id: records[index].id, createdAt: records[index].createdAt || normalized.createdAt };
  else records.push(normalized);
  editingId = normalized.id;
  await persist();
  renderCandidates();
  renderAnalysis(currentAnalyzed(normalized.id));
  setNotice("已保存到浏览器本地，并完成量化分析。", "success");
}

async function captureVisibleCompetitors() {
  const button = $("captureCompetitorsBtn"); button.disabled = true; button.textContent = "读取中…";
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id || !/^https:\/\/(www\.)?ozon\.ru\//i.test(tab.url || "")) throw new Error("请先正常打开当前 Ozon 商品详情页。");
    let response;
    try { response = await chrome.tabs.sendMessage(tab.id, { type: "OZON_CAPTURE_VISIBLE_COMPETITORS" }); }
    catch (_) { await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["extractor.js"] }); response = await chrome.tabs.sendMessage(tab.id, { type: "OZON_CAPTURE_VISIBLE_COMPETITORS" }); }
    if (!response?.ok) throw new Error(response?.error || "当前可见竞品读取失败。");
    competitorEvidenceBase = $("competitionEvidence").value.trim();
    visibleCompetitors = (response.competitors || []).map((item) => ({ ...item, selected: false }));
    syncSelectedCompetitorMetrics();
    renderVisibleCompetitors();
    setNotice(visibleCompetitors.length ? `已读取 ${visibleCompetitors.length} 条当前可见条目。请勾选需要纳入竞品集合的条目。` : "当前可见区域没有可确认的相似商品或其他卖家价格提示；不进行隐藏页面或批量抓取。", visibleCompetitors.length ? "success" : "error");
  } catch (error) { setNotice(error.message || "竞品读取失败。", "error"); }
  finally { button.disabled = false; button.textContent = "读取当前可见竞品"; }
}

function applyVisibleCompetitors() {
  const selected = selectedVisibleCompetitors();
  if (!selected.length) return setNotice("请先勾选至少一条当前可见竞品或其他卖家价格提示。", "error");
  const { lowest } = syncSelectedCompetitorMetrics();
  renderVisibleCompetitors();
  setNotice(`已确认写入 ${selected.length} 条竞品证据，并自动回填同质竞品数与最低可见价 ${lowest} RUB。该基准只代表当前可见且已勾选条目，不代表 Ozon 全站最低价。`, "success");
}

async function exportCsv() {
  const pool = analyzedPool();
  if (!pool.length) return setNotice("候选池为空，暂时无法导出。", "error");
  const blob = new Blob([globalThis.OzonEngine.toCsv(pool)], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  try {
    await chrome.downloads.download({ url, filename: `ozon-selection-candidates-${new Date().toISOString().slice(0, 10)}.csv`, saveAs: true });
    setNotice("已发起 CSV 下载。", "success");
  } finally {
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  }
}

async function exportEnterpriseIntake() {
  const raw = readForm();
  if (!raw.title.trim()) return setNotice("请先采集当前页或填写商品标题，再导出企业交接包。", "error");
  const packet = buildEnterprisePacket(raw);
  const url = URL.createObjectURL(new Blob([JSON.stringify(packet, null, 2)], { type: "application/json;charset=utf-8" }));
  try {
    await chrome.downloads.download({ url, filename: `zmt-ozon-intake-${new Date().toISOString().slice(0, 10)}.json`, saveAs: true });
    setNotice("已导出企业交接包。后台导入后会继续标识字段来源和待核验状态。", "success");
  } finally {
    setTimeout(() => URL.revokeObjectURL(url), 5000);
  }
}

async function syncToBackend() {
  const raw = readForm();
  if (!raw.title.trim() || num(raw.priceRub) <= 0) return setNotice("请先采集并确认商品标题和有效售价后再同步。", "error");
  const button = $("syncBtn"); button.disabled = true; button.textContent = "同步中…";
  try {
    settings.backendUrl = $("backendUrl").value.trim().replace(/\/$/, "") || "http://127.0.0.1:8000";
    await persist();
    const response = await fetch(`${settings.backendUrl}/api/v1/extension/capture`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Company-ID": "local-zmt", "X-User-ID": "local-operator", "X-Role": "operator" },
      body: JSON.stringify(buildEnterprisePacket(raw)),
    });
    const result = await response.json();
    if (!response.ok || !result.success) throw new Error(result.detail?.message || "后台拒绝了本次同步。");
    const analysis = result.data?.product?.analysis;
    const score = analysis?.total_score === null || analysis?.total_score === undefined ? "待补数" : `${analysis.total_score} / 100`;
    const completeness = analysis?.evidence_completeness?.percent ?? 0;
    const missing = Array.isArray(analysis?.missing_data) ? analysis.missing_data.length : 0;
    setNotice(`已同步到企业后台。后端四维结果：${score}；数据完整度：${completeness}%；待补关键项：${missing}；风险等级：${analysis?.risk_level || "待核验"}。`, "success");
  } catch (error) {
    setNotice(`同步失败：${error.message || "请确认企业后台已在 127.0.0.1:8000 启动。"}`, "error");
  } finally { button.disabled = false; button.textContent = "同步到企业后台"; }
}

async function clearAll() {
  if (!records.length) return;
  if (!confirm("确定清空浏览器本地保存的全部候选商品吗？此操作无法撤销。")) return;
  records = [];
  editingId = null;
  await persist();
  clearForm();
  renderCandidates();
  setNotice("已清空本地候选池。", "success");
}

async function init() {
  const saved = await chrome.storage.local.get(["records", "settings"]);
  records = Array.isArray(saved.records) ? saved.records : [];
  settings = { ...settings, ...(saved.settings || {}) };
  $("targetMargin").value = Math.round(settings.targetMargin * 100);
  $("commissionRate").value = Math.round(settings.defaultCommission * 100);
  $("backendUrl").value = settings.backendUrl || "http://127.0.0.1:8000";
  clearForm();
  renderCandidates();
  $("captureBtn").addEventListener("click", captureCurrentPage);
  $("captureCompetitorsBtn").addEventListener("click", captureVisibleCompetitors);
  $("applyCompetitorsBtn").addEventListener("click", applyVisibleCompetitors);
  $("analyzeBtn").addEventListener("click", saveAndAnalyze);
  $("newBtn").addEventListener("click", clearForm);
  $("handoffBtn").addEventListener("click", exportEnterpriseIntake);
  $("syncBtn").addEventListener("click", syncToBackend);
  $("saveBackendBtn").addEventListener("click", async () => { settings.backendUrl = $("backendUrl").value.trim().replace(/\/$/, "") || "http://127.0.0.1:8000"; await persist(); setNotice("企业后台地址已保存。", "success"); });
  $("openBackendBtn").addEventListener("click", () => { const url = $("backendUrl").value.trim() || "http://127.0.0.1:8000"; chrome.tabs.create({ url }); });
  $("exportBtn").addEventListener("click", exportCsv);
  $("clearBtn").addEventListener("click", clearAll);
}

document.addEventListener("DOMContentLoaded", init);
