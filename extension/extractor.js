(() => {
  if (window.__ozonSelectionExtractorLoaded) return;
  window.__ozonSelectionExtractorLoaded = true;

  const text = (value) => String(value || "").replace(/\s+/g, " ").trim();
  const getMeta = (selector) => document.querySelector(selector)?.content || "";
  const toNumber = (value) => {
    const cleaned = String(value || "").replace(/\s/g, "").replace(/[^\d,.-]/g, "").replace(",", ".");
    const parsed = Number(cleaned);
    return Number.isFinite(parsed) ? parsed : 0;
  };
  const firstNumber = (source) => {
    const match = text(source).match(/(\d[\d\s.,]*)\s*(?:₽|руб\.?|RUB)/i);
    return match ? toNumber(match[1]) : 0;
  };
  const productObjects = () => {
    const objects = [];
    document.querySelectorAll('script[type="application/ld+json"]').forEach((node) => {
      try {
        const parsed = JSON.parse(node.textContent || "{}");
        const queue = Array.isArray(parsed) ? [...parsed] : [parsed];
        while (queue.length) {
          const item = queue.shift();
          if (!item || typeof item !== "object") continue;
          if (Array.isArray(item["@graph"])) queue.push(...item["@graph"]);
          if (String(item["@type"] || "").toLowerCase().includes("product")) objects.push(item);
        }
      } catch (_) {
        // JSON-LD 不完整时降级到用户当前可见页面的 DOM。
      }
    });
    return objects;
  };
  const firstVisibleText = (selectors) => {
    for (const selector of selectors) {
      const node = document.querySelector(selector);
      const value = text(node?.textContent);
      if (value) return value;
    }
    return "";
  };
  const allVisibleText = (selectors) => {
    for (const selector of selectors) {
      const values = [...document.querySelectorAll(selector)].map((node) => text(node.textContent)).filter(Boolean);
      if (values.length) return values;
    }
    return [];
  };
  const structuredCategory = (product) => {
    const value = product.categoryPath || product.category || product.itemCategory || "";
    if (Array.isArray(value)) return value.map(text).filter(Boolean).join(" > ");
    if (typeof value === "object") return text(value.name || value.path || "");
    return text(value);
  };
  const productIdFromPage = (product) => {
    const fromUrl = location.pathname.match(/-(\d+)(?:\/|$)/)?.[1] || "";
    return text(fromUrl || product.productID || product.sku || product.mpn || "");
  };
  const breadcrumbPath = (brand) => {
    const candidates = allVisibleText([
      '[data-widget*="bread"] a',
      '[data-widget*="Bread"] a',
      'nav[aria-label*="хлеб"] a',
      'nav[aria-label*="breadcrumb"] a',
      'a[href*="/category/"]',
    ]);
    const ignored = /^(главная|ozon|каталог|主页|目录)$/i;
    const normalizedBrand = text(brand).toLowerCase();
    const parts = candidates.filter((part) => !ignored.test(part)).filter((part) => part.toLowerCase() !== normalizedBrand);
    return parts.length >= 2 ? [...new Set(parts)].join(" > ") : "";
  };

  const isVisible = (node) => {
    if (!node) return false;
    const rect = node.getBoundingClientRect();
    const style = window.getComputedStyle(node);
    const viewportWidth = window.innerWidth || document.documentElement.clientWidth || 0;
    const viewportHeight = window.innerHeight || document.documentElement.clientHeight || 0;
    return rect.width > 20 && rect.height > 20 &&
      rect.right > 0 && rect.bottom > 0 && rect.left < viewportWidth && rect.top < viewportHeight &&
      style.visibility !== "hidden" && style.display !== "none";
  };
  const productIdFromHref = (href) => String(href || "").match(/-(\d+)(?:[/?#]|$)/)?.[1] || "";
  const visibleCompetitors = (currentProductId) => {
    const result = [];
    const seen = new Set();
    const push = (entry) => {
      const key = `${entry.kind}:${entry.ozonProductId || ""}:${entry.priceRub || ""}:${entry.pageText || ""}`;
      if (!entry.priceRub || seen.has(key) || result.length >= 12) return;
      seen.add(key); result.push(entry);
    };
    [...document.querySelectorAll('a[href*="/product/"]')].filter(isVisible).forEach((link) => {
      const href = link.href || "";
      const productId = productIdFromHref(href);
      if (!productId || productId === currentProductId) return;
      const card = link.closest('[data-widget]') || link.parentElement?.parentElement || link;
      const cardText = text(card?.innerText || link.innerText);
      const price = firstNumber(cardText);
      if (!price) return;
      push({
        kind: "similar_product", ozonProductId: productId, title: text(link.innerText) || "当前可见相似商品",
        priceRub: price, url: href, pageText: cardText.slice(0, 240), source: "Ozon 当前可见相似商品卡片", capturedAt: new Date().toISOString(),
      });
    });
    [...document.querySelectorAll('[data-widget]')].filter(isVisible).forEach((node) => {
      const cardText = text(node.innerText);
      if (!/(других продавц|其他卖家|other sellers)/i.test(cardText)) return;
      const price = firstNumber(cardText);
      if (!price) return;
      push({
        kind: "other_seller_offer", ozonProductId: "", title: "当前页其他卖家价格提示", priceRub: price,
        url: location.href, pageText: cardText.slice(0, 240), source: "Ozon 当前可见其他卖家提示", capturedAt: new Date().toISOString(),
      });
    });
    return result;
  };

  function capture() {
    const pageText = text(document.body?.innerText || "").slice(0, 240000);
    const product = productObjects()[0] || {};
    const offers = Array.isArray(product.offers) ? product.offers[0] : (product.offers || {});
    const aggregate = product.aggregateRating || {};
    const brand = text(typeof product.brand === "object" ? product.brand.name : product.brand);
    const title = text(
      product.name || getMeta('meta[property="og:title"]') ||
      firstVisibleText(["h1", '[data-widget*="webProductHeading"]', '[data-widget*="productTitle"]']) || document.title
    );
    const image = text(product.image || getMeta('meta[property="og:image"]') || document.querySelector('img[src*="ozon"]')?.src || "");
    const visibleCurrent = firstNumber(firstVisibleText([
      '[data-widget*="webPrice"] [data-widget*="price"]',
      '[data-widget*="webPrice"]',
      '[data-widget*="price"]',
      '[data-widget*="Price"]',
    ]));
    const structuredCurrent = toNumber(offers.price || offers.lowPrice || "");
    const pageFallbackPrice = firstNumber(pageText);
    const priceRub = visibleCurrent || structuredCurrent || pageFallbackPrice;
    const visibleRegular = firstNumber(firstVisibleText([
      'del', 's',
      '[data-widget*="oldPrice"]', '[data-widget*="OldPrice"]', '[data-widget*="price"] del',
    ]));
    const structuredRegular = toNumber(offers.highPrice || offers.priceBeforeDiscount || product.priceBeforeDiscount || "");
    const regularCandidate = visibleRegular || structuredRegular;
    const regularPriceRub = regularCandidate > priceRub ? regularCandidate : 0;
    const visibleCategory = breadcrumbPath(brand);
    const jsonCategory = structuredCategory(product);
    const categoryPath = visibleCategory || jsonCategory || "类目待确认";
    const structuredRating = toNumber(aggregate.ratingValue || "");
    const ratingNode = firstVisibleText([
      '[data-widget*="webReviewProductScore"]', '[data-widget*="reviews"] [aria-label*="рейтинг"]',
      '[data-widget*="rating"]', '[aria-label*="Рейтинг"]',
    ]);
    const ratingMatch = text(ratingNode).match(/([1-5](?:[,.]\d)?)/);
    const rating = structuredRating || (ratingMatch ? toNumber(ratingMatch[1]) : 0);
    const structuredReviews = toNumber(aggregate.reviewCount || "");
    const reviewNode = firstVisibleText(['[data-widget*="reviews"]', '[data-widget*="review"]']);
    const reviewCount = structuredReviews || firstNumber(reviewNode) || 0;

    const ozonProductId = productIdFromPage(product);
    return {
      url: location.href,
      title,
      ozonProductId,
      brand,
      brandSource: brand ? "Ozon JSON-LD 品牌字段" : "待用户确认",
      category: categoryPath,
      categoryPath,
      categorySource: visibleCategory ? "Ozon 页面可见面包屑" : (jsonCategory ? "Ozon JSON-LD 类目" : "待用户确认"),
      image,
      priceRub,
      currentPriceRub: priceRub,
      regularPriceRub,
      priceSource: visibleCurrent ? "Ozon 页面可见当前展示价" : (structuredCurrent ? "Ozon JSON-LD 当前价" : (pageFallbackPrice ? "Ozon 页面文本降级提取" : "未识别")),
      rating,
      reviewCount,
      monthlySales: 0,
      salesSource: "公开商品页未提供",
      visibleCompetitors: visibleCompetitors(ozonProductId),
      source: {
        title: product.name ? "Ozon JSON-LD" : "Ozon 页面可见标题",
        brand: brand ? "Ozon JSON-LD 品牌字段" : "待确认",
        category: visibleCategory ? "Ozon 页面可见面包屑" : (jsonCategory ? "Ozon JSON-LD 类目" : "待确认"),
        price: visibleCurrent ? "Ozon 页面可见当前展示价" : (structuredCurrent ? "Ozon JSON-LD 当前价" : "未识别"),
        regularPrice: regularPriceRub ? (visibleRegular ? "Ozon 页面可见划线价" : "Ozon JSON-LD 常规价") : "商品页未展示或未识别",
        rating: structuredRating ? "Ozon JSON-LD" : (rating ? "Ozon 页面可见文本" : "未识别"),
        sales: "公开商品页未提供；需卖家统计、已授权 API 或人工核验证据",
      },
    };
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type !== "OZON_CAPTURE_CURRENT_PAGE" && message?.type !== "OZON_CAPTURE_VISIBLE_COMPETITORS") return;
    try {
      const product = capture();
      sendResponse(message.type === "OZON_CAPTURE_VISIBLE_COMPETITORS" ? { ok: true, competitors: product.visibleCompetitors || [] } : { ok: true, product });
    } catch (error) {
      sendResponse({ ok: false, error: String(error?.message || error) });
    }
    return true;
  });
})();
