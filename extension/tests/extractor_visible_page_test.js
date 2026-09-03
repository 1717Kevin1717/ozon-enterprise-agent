const fs = require('fs');
const path = require('path');
const vm = require('vm');

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

const productJson = JSON.stringify({
  '@type': 'Product',
  name: '验证鞋垫',
  brand: { name: 'MyBalance' },
  offers: { price: '148', priceBeforeDiscount: '169' },
  aggregateRating: { ratingValue: '4.7', reviewCount: '0' },
});
const node = (textContent, extra = {}) => ({ textContent, ...extra });
const visibleCard = { innerText: '可见相似鞋垫 99 ₽' };
const visibleLink = {
  innerText: '可见相似鞋垫', href: 'https://www.ozon.ru/product/visible-2710116535/',
  getBoundingClientRect: () => ({ width: 160, height: 40, left: 20, right: 180, top: 120, bottom: 160 }),
  closest: () => visibleCard,
};
const offscreenLink = {
  innerText: '屏幕外商品', href: 'https://www.ozon.ru/product/offscreen-2710116536/',
  getBoundingClientRect: () => ({ width: 160, height: 40, left: 20, right: 180, top: 900, bottom: 940 }),
  closest: () => ({ innerText: '屏幕外商品 79 ₽' }),
};
const nodes = {
  'script[type="application/ld+json"]': [node(productJson)],
  '[data-widget*="bread"] a': [node('Ozon'), node('鞋类'), node('护理和配件'), node('鞋垫'), node('MyBalance')],
  '[data-widget*="webPrice"] [data-widget*="price"]': [node('148 ₽')],
  del: [node('169 ₽')],
  'a[href*="/product/"]': [visibleLink, offscreenLink],
  '[data-widget]': [],
};
let messageListener;
const context = {
  window: { innerWidth: 1280, innerHeight: 720, getComputedStyle: () => ({ visibility: 'visible', display: 'block' }) },
  location: { href: 'https://www.ozon.ru/product/test-product-2710116534/', pathname: '/product/test-product-2710116534/' },
  document: {
    title: '验证鞋垫 — Ozon',
    body: { innerText: '验证鞋垫 148 ₽ 169 ₽' },
    querySelector: (selector) => (nodes[selector] || [])[0] || null,
    querySelectorAll: (selector) => nodes[selector] || [],
  },
  chrome: { runtime: { onMessage: { addListener: (listener) => { messageListener = listener; } } } },
};

vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'extractor.js'), 'utf8'), context);
let response;
messageListener({ type: 'OZON_CAPTURE_CURRENT_PAGE' }, null, (value) => { response = value; });

assert(response?.ok, '当前可见页面提取应成功返回结果');
assert(response.product.categoryPath === '鞋类 > 护理和配件 > 鞋垫', '类目路径不得包含品牌 MyBalance');
assert(response.product.brand === 'MyBalance', '品牌必须从 Ozon 品牌字段单独输出');
assert(response.product.brandSource === 'Ozon JSON-LD 品牌字段', '品牌必须保留字段来源');
assert(response.product.priceRub === 148, '当前展示价应优先取页面可见价格');
assert(response.product.regularPriceRub === 169, '常规价应从划线价或结构化价格中单独保留');
assert(response.product.ozonProductId === '2710116534', '应从 Ozon 商品链接提取商品 ID');
assert(response.product.monthlySales === 0 && response.product.salesSource === '公开商品页未提供', '公开商品页不得伪造近 30 天销量');
assert(response.product.visibleCompetitors.length === 1, '仅当前视口内的相似商品可作为待用户确认的竞品候选');
assert(response.product.visibleCompetitors[0].ozonProductId === '2710116535', '屏幕外相似商品不得进入当前可见竞品集合');

console.log('EXTRACTOR_VISIBLE_PAGE_TEST_OK');
