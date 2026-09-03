const fs = require('fs');
const vm = require('vm');

vm.runInThisContext(fs.readFileSync(require('path').join(__dirname, '..', 'engine.js'), 'utf8'));

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

const base = {
  title: '内部规则验证候选', category: '测试类目', priceRub: 1000,
  monthlySales: 120, reviewCount: 0, estimatedCostRub: 350,
  shippingCostRub: 80, adCostRub: 40, commissionRate: 0.18,
  currentPriceRub: 1000, regularPriceRub: 1250, categoryPath: '测试一级类目 > 测试二级类目',
  salesSource: 'Ozon Seller 已授权统计', salesEvidence: '已授权统计报表，商品 ID test，统计周期 30 天',
  competitorCount: 12, competitionEvidence: '人工核验价格带与差异化',
  complianceStatus: '通过', complianceEvidence: '内部合规清单已复核',
};

const ready = globalThis.OzonEngine.calculateOne(base, [base], { targetMargin: 0.30, defaultCommission: 0.18 });
assert(ready.complianceScore === 100, '合规通过必须得到合规维度评分');
assert(ready.competitionScore > 0, '有竞争证据时必须产生竞争评分');
assert(!['待补数据', '合规阻断'].includes(ready.decision), '完整合规候选不应被证据阻断');
assert(ready.currentPriceRub === 1000 && ready.regularPriceRub === 1250, '必须保留当前展示价和常规价两个价格口径');
assert(ready.categoryPath === '测试一级类目 > 测试二级类目', '必须保留 Ozon 类目路径');
assert(Array.isArray(ready.tags), '商品主档必须将业务标签标准化为数组');

const master = globalThis.OzonEngine.normalize({ ...base, ozonProductId: '2710116534', url: 'https://www.ozon.ru/product/test-2710116534/?from=search' });
assert(master.masterKey === 'ozon-product:2710116534', '商品主档必须优先使用 Ozon 商品 ID 生成稳定主键');

const blocked = globalThis.OzonEngine.calculateOne({ ...base, complianceStatus: '未通过' }, [{ ...base, complianceStatus: '未通过' }], { targetMargin: 0.30, defaultCommission: 0.18 });
assert(blocked.decision === '合规阻断', '合规未通过必须阻断进入建议');
assert(blocked.risks.some((risk) => risk.includes('合规')), '合规阻断必须留下风险说明');

const incomplete = globalThis.OzonEngine.calculateOne({ ...base, monthlySales: 0 }, [{ ...base, monthlySales: 0 }], { targetMargin: 0.30, defaultCommission: 0.18 });
assert(incomplete.decision === '待补数据', '需求证据缺失必须进入待补数据状态');

const noSalesEvidence = globalThis.OzonEngine.calculateOne({ ...base, salesEvidence: '' }, [{ ...base, salesEvidence: '' }], { targetMargin: 0.30, defaultCommission: 0.18 });
assert(noSalesEvidence.decision === '待补数据', '无销量证据的数字不得解除待补数据状态');
assert(noSalesEvidence.risks.some((risk) => risk.includes('销量缺少')), '无销量证据必须明确提示来源风险');

console.log('FOUR_DIMENSION_ENTERPRISE_TEST_OK');
