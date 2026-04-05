"""
Nexus AI — Image Handler (Improvement #17)
User sends product image → Qwen2-VL identifies it → browser agents search
Amazon, Flipkart, Myntra → cross-verified price comparison returned.
"""
from __future__ import annotations

import base64
import asyncio
from pathlib import Path
from typing import Optional

import structlog

from config.settings import get_settings

log = structlog.get_logger(__name__)


PRODUCT_SEARCH_SOURCES = [
    {"name": "amazon.in",   "url": "https://amazon.in/s?k={query}"},
    {"name": "flipkart.com","url": "https://flipkart.com/search?q={query}"},
    {"name": "myntra.com",  "url": "https://myntra.com/{query}"},
    {"name": "meesho.com",  "url": "https://meesho.com/search?q={query}"},
    {"name": "snapdeal.com","url": "https://snapdeal.com/search?keyword={query}"},
    {"name": "croma.com",   "url": "https://croma.com/searchB?q={query}"},
]


class ImageHandler:
    """
    Handles image input for product identification and price lookup.
    Uses Qwen2-VL (free, local) for vision tasks.
    """

    def __init__(self) -> None:
        self._llm = None

    async def identify_product(
        self,
        image_bytes: bytes,
        image_type: str = "jpeg",
    ) -> dict:
        """
        Use Qwen2-VL to identify product in image.
        Returns: {product_name, brand, category, search_query, confidence}
        """
        image_b64 = base64.b64encode(image_bytes).decode()

        # Qwen2-VL via HuggingFace API
        system_prompt = """You are a product identification expert.
Analyze the image and identify the product.
Return ONLY a JSON object:
{
  "product_name": "exact product name",
  "brand": "brand name or empty string",
  "category": "electronics/clothing/food/book/other",
  "search_query": "best search query to find price online",
  "confidence": 0.0-1.0
}
Be specific. "iPhone 15 Pro 256GB Natural Titanium" not just "phone"."""

        try:
            from src.agents.llm_client import llm_client
            import json

            response = await llm_client.chat(
                model="Qwen/Qwen2-VL-7B-Instruct",
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/{image_type};base64,{image_b64}"
                            }
                        },
                        {"type": "text", "text": "Identify this product."},
                    ],
                }],
                system=system_prompt,
                temperature=0.0,
                max_tokens=256,
                json_mode=True,
            )
            data = json.loads(response.content)
            log.info("product_identified",
                     product=data.get("product_name", "")[:40],
                     confidence=data.get("confidence", 0))
            return data
        except Exception as exc:
            log.warning("product_identification_failed", error=str(exc))
            return {
                "product_name": "unknown",
                "brand": "",
                "category": "other",
                "search_query": "",
                "confidence": 0.0,
                "error": str(exc),
            }

    async def find_prices(
        self,
        image_bytes: bytes,
        image_type: str = "jpeg",
    ) -> dict:
        """
        Full pipeline: identify product → search prices → return comparison.
        """
        # Step 1: Identify product
        product = await self.identify_product(image_bytes, image_type)
        if not product.get("search_query") or product.get("confidence", 0) < 0.5:
            return {
                "success": False,
                "error": "Could not identify product with sufficient confidence",
                "product": product,
            }

        log.info("price_search_started",
                 product=product["product_name"][:40],
                 query=product["search_query"][:40])

        # Step 2: Return search targets (browser agents handle actual scraping)
        search_targets = [
            {
                "source": s["name"],
                "url": s["url"].format(query=product["search_query"].replace(" ", "+")),
            }
            for s in PRODUCT_SEARCH_SOURCES
        ]

        return {
            "success": True,
            "product": product,
            "search_query": product["search_query"],
            "search_targets": search_targets,
            "message": f"Searching {len(search_targets)} sources for '{product['product_name']}'",
        }

    async def handle_telegram_photo(
        self,
        photo_bytes: bytes,
        user_id: str,
    ) -> dict:
        """
        Handle a photo sent via Telegram.
        Returns search targets for the pipeline to scrape.
        """
        log.info("telegram_photo_received", user_id=user_id[:8]+"…")
        result = await self.find_prices(photo_bytes)
        if result["success"]:
            product_name = result["product"]["product_name"]
            return {
                "type": "product_search",
                "query": f"Find best price for {product_name}",
                "product_name": product_name,
                "search_targets": result["search_targets"],
                "ready_for_pipeline": True,
            }
        return {"success": False, "error": result.get("error", "Unknown error")}


image_handler = ImageHandler()
