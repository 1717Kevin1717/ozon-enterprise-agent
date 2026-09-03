// 扩展离线冒烟测试；数据仅用于验证程序逻辑，不代表真实市场结论。
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const root = path.resolve(__dirname, "..");
const manifest = JSON.parse(fs.readFileSync(path.join(root, "manifest.json"), "utf8"));
assert.strictEqual(manifest.manifest_version, 3);
assert(manifest.permissions.includes("sidePanel"));
assert(manifest.permissions.includes("storage"));
assert(manifest.permissions.includes("scripting"));
assert(manifest.host_permissions.includes("https://www.ozon.ru/*"));

const code = fs.readFileSync(path.join(root, "engine.js"), "utf8");
const context = { globalThis: {} };
vm.runInNewContext(code, context, { filename: "engine.js" });
const engine = context.globalThis.OzonEngine;
assert(engine);

const records = [
  { id: "a", title: "测试候选 A", category: "测试类目", priceRub: 820, monthlySales: 520, rating: 4.8, reviewCount: 260, estimatedCostRub: 260, shippingCostRub: 60, adCostRub: 35, commissionRate: 0.18, competitorCount: 38 },
  { id: "b", title: "测试候选 B", category: "测试类目", priceRub: 950, monthlySales: 300, rating: 4.5, reviewCount: 120, estimatedCostRub: 360, shippingCostRub: 70, adCostRub: 45, commissionRate: 0.18, competitorCount: 75 },
  { id: "c", title: "测试候选 C", category: "测试类目", priceRub: 730, monthlySales: 90, rating: 4.0, reviewCount: 15, estimatedCostRub: 700, shippingCostRub: 80, adCostRub: 40, commissionRate: 0.18, competitorCount: 98 },
  { id: "d", title: "测试候选 D", category: "测试类目", priceRub: 880, monthlySales: 410, rating: 4.7, reviewCount: 180, estimatedCostRub: 300, shippingCostRub: 65, adCostRub: 38, commissionRate: 0.18, competitorCount: 44 },
];
const pool = engine.analyzePool(records, { targetMargin: 0.30, defaultCommission: 0.18 });
assert.strictEqual(pool.length, 4);
assert(pool[0].selectionScore >= pool[3].selectionScore);
assert(pool.some((item) => item.netMargin < 0));
assert(pool.some((item) => item.risks.some((risk) => risk.includes("亏损"))));
assert(pool.every((item) => item.recommendedPrice > 0));
assert(pool.every((item, index) => item.rank === index + 1));
const csv = engine.toCsv(pool);
assert(csv.startsWith("\ufeffrank,title"));
assert(csv.includes("测试候选 A"));

const panel = fs.readFileSync(path.join(root, "panel.html"), "utf8");
["captureBtn", "analyzeBtn", "exportBtn", "candidateList", "analysisCard"].forEach((id) => assert(panel.includes(`id="${id}"`)));
console.log("EXTENSION_SMOKE_TEST_PASSED");
