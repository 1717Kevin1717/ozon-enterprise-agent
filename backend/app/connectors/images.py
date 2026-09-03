from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ImageAsset:
    url: str
    provider: str
    source_notice: str
    verified_product_match: bool


class ImageProvider(Protocol):
    def for_category(self, category: str, index: int = 0) -> ImageAsset: ...


class PublicDemoImageProvider:
    """Public stock imagery for local demos; never claims an exact product match."""

    _images = {
        "厨房用品": "https://images.unsplash.com/photo-1556911220-bff31c812dba?auto=format&fit=crop&w=640&q=80",
        "宠物用品": "https://images.unsplash.com/photo-1552053831-71594a27632d?auto=format&fit=crop&w=640&q=80",
        "汽车用品": "https://images.unsplash.com/photo-1503376780353-7e6692767b70?auto=format&fit=crop&w=640&q=80",
        "收纳用品": "https://images.unsplash.com/photo-1586023492125-27b2c045efd7?auto=format&fit=crop&w=640&q=80",
        "电子产品": "https://images.unsplash.com/photo-1498049794561-7780e7231661?auto=format&fit=crop&w=640&q=80",
        "默认": "https://images.unsplash.com/photo-1523275335684-37898b6baf30?auto=format&fit=crop&w=640&q=80",
    }

    def for_category(self, category: str, index: int = 0) -> ImageAsset:
        root = next((name for name in self._images if name != "默认" and name in category), "默认")
        return ImageAsset(
            url=self._images[root],
            provider="unsplash_public_demo",
            source_notice="公开库存图，仅作类别演示，不代表对应 Ozon 商品实拍。",
            verified_product_match=False,
        )
