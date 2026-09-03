const fs = require('fs');
const path = require('path');
const vm = require('vm');

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

const elements = {
  competitorCount: { value: '' },
  visibleLowestCompetitorPrice: { value: '' },
  competitionEvidence: { value: '' },
  visibleCompetitorList: { innerHTML: '', querySelectorAll: () => [] },
  notice: { textContent: '', className: '' },
};

const source = fs.readFileSync(path.join(__dirname, '..', 'panel.js'), 'utf8');
const testHook = `
globalThis.__competitorTestApi = {
  setVisible: (items) => { visibleCompetitors = items; },
  setEvidenceBase: (value) => { competitorEvidenceBase = value; },
  sync: syncSelectedCompetitorMetrics,
  selected: selectedVisibleCompetitors,
};`;

const context = {
  console,
  globalThis: {},
  document: {
    getElementById: (id) => elements[id] || { value: '', textContent: '', className: '', querySelectorAll: () => [] },
    addEventListener: () => {},
  },
};
context.globalThis = context;
vm.runInNewContext(`${source}\n${testHook}`, context);

context.__competitorTestApi.setEvidenceBase('人工复核：同类目价格带正常');
context.__competitorTestApi.setVisible([
  { kind: 'similar_product', title: '相似商品 A', priceRub: 139, selected: true },
  { kind: 'other_seller_offer', title: '其他卖家提示', priceRub: 126, selected: true },
  { kind: 'similar_product', title: '未选择商品', priceRub: 99, selected: false },
]);
const outcome = context.__competitorTestApi.sync();

assert(outcome.selected.length === 2, '只应统计用户已勾选的当前可见竞品');
assert(elements.competitorCount.value === 2, '同质竞品数应自动回填已选择条目数');
assert(elements.visibleLowestCompetitorPrice.value === 126, '最低可见价应自动取已选择条目的最低价格');
assert(elements.competitionEvidence.value.includes('人工复核：同类目价格带正常'), '原有人工证据不得被自动回填覆盖');
assert(elements.competitionEvidence.value.includes('选择 2 条竞品/其他卖家条目'), '自动证据应说明用户选择的当前可见条目数');
assert(!elements.competitionEvidence.value.includes('未选择商品'), '未勾选条目不得进入竞品证据');

console.log('COMPETITOR_AUTOFILL_TEST_OK');
