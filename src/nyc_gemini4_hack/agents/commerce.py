from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from nyc_gemini4_hack.config import Settings, get_settings
from nyc_gemini4_hack.models import (
    CartLineItem,
    GlobalProductOption,
    MaterialItem,
    MaterialStatus,
    ShopifyCartLineRequest,
    ShoppingCartProposal,
)

logger = logging.getLogger(__name__)

_ZERO_DECIMAL_CURRENCIES = {"JPY", "KRW", "VND", "CLP", "ISK", "UGX"}
_STOPWORDS = {
    "a",
    "an",
    "the",
    "and",
    "or",
    "for",
    "with",
    "of",
    "to",
    "in",
    "on",
    "set",
    "pack",
    "kit",
    "sheet",
    "sheets",
    "piece",
    "pieces",
    "small",
    "medium",
    "large",
    "colored",
    "colorful",
    "basic",
    "standard",
}


def format_minor_price(amount_minor: int | float | None, currency: str = "USD") -> str:
    """Convert UCP ISO-4217 minor units (e.g. 1099 -> '$10.99', 0 -> '$0.00')."""
    if amount_minor is None:
        return "$0.00"
    cur = (currency or "USD").upper()
    symbol = "$" if cur == "USD" else f"{cur} "
    if cur in _ZERO_DECIMAL_CURRENCIES:
        return f"{symbol}{int(amount_minor)}"
    return f"{symbol}{float(amount_minor) / 100:.2f}"


def _extract_merchant(url: str | None, fallback: str = "") -> str:
    if not url:
        return fallback
    host = urlparse(url).netloc.lower()
    return host.removeprefix("www.") or fallback


def _extract_first_image(product: dict[str, Any], variant: dict[str, Any]) -> str | None:
    for container in (variant, product):
        media = container.get("media") or []
        if isinstance(media, list):
            for item in media:
                if isinstance(item, dict) and item.get("url"):
                    return str(item["url"])
        if container.get("image_url"):
            return str(container["image_url"])
    return None


def _extract_price(product: dict[str, Any], variant: dict[str, Any]) -> tuple[int, str]:
    price_obj = variant.get("price")
    if isinstance(price_obj, dict):
        return int(price_obj.get("amount", 0) or 0), str(price_obj.get("currency") or "USD")
    if isinstance(price_obj, (int, float)):
        return int(price_obj), "USD"
    pr = product.get("price_range") or {}
    min_p = pr.get("min") if isinstance(pr, dict) else None
    if isinstance(min_p, dict):
        return int(min_p.get("amount", 0) or 0), str(min_p.get("currency") or "USD")
    return 0, "USD"


def _parse_mcp_structured_content(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract UCP structured payload from a JSON-RPC 2.0 MCP tools/call response."""
    if "error" in payload and payload["error"]:
        err = payload["error"]
        raise RuntimeError(f"Shopify MCP error {err.get('code')}: {err.get('message')} ({err.get('data')})")

    result = payload.get("result") or {}
    if result.get("isError"):
        raise RuntimeError(f"Shopify MCP tool error: {result.get('content')}")

    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured

    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
            try:
                parsed = json.loads(block["text"])
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                continue
    return {}


class ShopifyUCPClient:
    """Reusable async JSON-RPC 2.0 client for any Shopify UCP MCP endpoint (/api/ucp/mcp)."""

    def __init__(
        self,
        mcp_url: str,
        agent_profile_url: str,
        bearer_token: str = "",
        timeout_seconds: float = 15.0,
    ) -> None:
        self.mcp_url = mcp_url.strip()
        self.agent_profile_url = agent_profile_url.strip()
        self.bearer_token = bearer_token.strip()
        self.timeout_seconds = timeout_seconds
        self._rpc_id = 0

    @classmethod
    def for_global_catalog(cls, settings: Settings) -> ShopifyUCPClient:
        return cls(settings.shopify_global_mcp_url, settings.shopify_profile_url)

    @classmethod
    def for_store(cls, settings: Settings) -> ShopifyUCPClient:
        return cls(settings.shopify_mcp_url, settings.shopify_profile_url, bearer_token=settings.shopify_storefront_token)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        return headers

    def _meta(self) -> dict[str, Any]:
        return {"ucp-agent": {"profile": self.agent_profile_url}}

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any]:
        self._rpc_id += 1
        body = {
            "jsonrpc": "2.0",
            "id": self._rpc_id,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": {
                    "meta": self._meta(),
                    **arguments,
                },
            },
        }
        if client is not None:
            resp = await client.post(self.mcp_url, headers=self._headers(), json=body)
            resp.raise_for_status()
            return _parse_mcp_structured_content(resp.json())

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as temp_client:
            resp = await temp_client.post(self.mcp_url, headers=self._headers(), json=body)
            resp.raise_for_status()
            return _parse_mcp_structured_content(resp.json())

    async def search_catalog(
        self,
        query: str,
        limit: int = 5,
        client: httpx.AsyncClient | None = None,
    ) -> list[dict[str, Any]]:
        data = await self.call_tool(
            "search_catalog",
            {
                "catalog": {
                    "query": query,
                    "pagination": {"limit": limit},
                }
            },
            client=client,
        )
        products = data.get("products") or []
        return products if isinstance(products, list) else []

    async def create_cart(
        self,
        line_items: list[dict[str, Any]],
        client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any]:
        return await self.call_tool(
            "create_cart",
            {"cart": {"line_items": line_items}},
            client=client,
        )

    async def update_cart(
        self,
        cart_id: str,
        line_items: list[dict[str, Any]],
        client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any]:
        return await self.call_tool(
            "update_cart",
            {"id": cart_id, "cart": {"line_items": line_items}},
            client=client,
        )

    async def get_cart(
        self,
        cart_id: str,
        client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any]:
        return await self.call_tool("get_cart", {"id": cart_id}, client=client)


def _normalize_products_to_options(
    products: list[dict[str, Any]],
    material_id: str = "",
    material_name: str = "",
    default_merchant: str = "",
    max_options: int = 5,
) -> list[GlobalProductOption]:
    options: list[GlobalProductOption] = []
    for prod in products:
        variants = prod.get("variants") or []
        if not isinstance(variants, list) or not variants:
            continue
        # Prefer first available variant
        chosen = next(
            (v for v in variants if (v.get("availability") or {}).get("available", True)),
            variants[0],
        )
        amount_minor, currency = _extract_price(prod, chosen)
        product_url = chosen.get("url") or prod.get("url")
        if not product_url and default_merchant and prod.get("handle"):
            product_url = f"https://{default_merchant}/products/{prod['handle']}"
        merchant = _extract_merchant(product_url, fallback=default_merchant)
        variant_title = chosen.get("title") or ""
        prod_title = str(prod.get("title") or variant_title or "Untitled Product")
        if variant_title and variant_title.lower() not in ("default title", prod_title.lower()):
            display_title = f"{prod_title} — {variant_title}"
        else:
            display_title = prod_title

        options.append(
            GlobalProductOption(
                material_id=material_id,
                material_name=material_name,
                product_id=str(prod.get("id") or ""),
                variant_id=str(chosen.get("id") or ""),
                product_title=display_title,
                merchant=merchant,
                price=format_minor_price(amount_minor, currency),
                price_minor=amount_minor,
                currency=currency,
                product_url=str(product_url) if product_url else None,
                image_url=_extract_first_image(prod, chosen),
                available=bool((chosen.get("availability") or {}).get("available", True)),
            )
        )
        if len(options) >= max_options:
            break
    return options


def _fallback_queries(query: str) -> list[str]:
    """Generate progressively broader search queries for a small mock store catalog."""
    cleaned = query.strip()
    candidates: list[str] = [cleaned] if cleaned else []
    words = [w for w in re.findall(r"[a-zA-Z0-9]+", cleaned.lower()) if w not in _STOPWORDS and len(w) > 2]
    if len(words) >= 2:
        last_two = " ".join(words[-2:])
        if last_two not in candidates:
            candidates.append(last_two)
    for w in reversed(words):
        if w not in candidates:
            candidates.append(w)
    return candidates[:4]


async def _search_store_with_fallback(
    store_client: ShopifyUCPClient,
    query: str,
    http_client: httpx.AsyncClient,
    limit: int = 3,
) -> list[dict[str, Any]]:
    for q in _fallback_queries(query):
        products = await store_client.search_catalog(q, limit=limit, client=http_client)
        if products:
            return products
    return []


def _extract_cart_totals(cart_data: dict[str, Any]) -> tuple[str, str]:
    currency = str(cart_data.get("currency") or "USD")
    totals = cart_data.get("totals") or []
    for entry in totals:
        if isinstance(entry, dict) and entry.get("type") in ("total", "subtotal"):
            return format_minor_price(entry.get("amount", 0), currency), currency
    return format_minor_price(0, currency), currency


async def search_shopify_products(
    query: str,
    limit: int = 5,
    scope: str = "both",
    *,
    settings: Settings | None = None,
) -> dict[str, list[GlobalProductOption]]:
    """
    Standalone reusable search across Shopify Global Catalog MCP and/or the mock dev store.
    Frontend-agnostic: returns normalized GlobalProductOption lists under 'global' and 'store'.
    """
    settings = settings or get_settings()
    result: dict[str, list[GlobalProductOption]] = {"global": [], "store": []}

    async with httpx.AsyncClient(timeout=15.0) as http_client:
        tasks: list[tuple[str, Any]] = []
        if scope in ("global", "both") and settings.shopify_global_mcp_url.strip():
            global_client = ShopifyUCPClient.for_global_catalog(settings)
            tasks.append(("global", global_client.search_catalog(query, limit=limit, client=http_client)))

        if scope in ("store", "both") and settings.shopify_mcp_url.strip():
            store_client = ShopifyUCPClient.for_store(settings)
            tasks.append(("store", _search_store_with_fallback(store_client, query, http_client, limit=limit)))

        if not tasks:
            return result

        responses = await asyncio.gather(*(t[1] for t in tasks), return_exceptions=True)
        for (key, _), resp in zip(tasks, responses, strict=True):
            if isinstance(resp, Exception):
                logger.warning("Shopify %s search failed for '%s': %s", key, query, resp)
                continue
            default_merchant = settings.shopify_shop_domain if key == "store" else ""
            result[key] = _normalize_products_to_options(
                resp,
                default_merchant=default_merchant,
                max_options=limit,
            )

    return result


async def create_mock_store_cart(
    items: list[ShopifyCartLineRequest],
    *,
    settings: Settings | None = None,
) -> ShoppingCartProposal:
    """
    Standalone reusable cart builder that always checks out on the configured mock dev store.
    """
    settings = settings or get_settings()
    if not settings.shopify_mcp_url.strip():
        raise ValueError("SHOPIFY_MCP_URL is not configured.")

    store_client = ShopifyUCPClient.for_store(settings)
    merged: dict[str, int] = {}
    for item in items:
        merged[item.variant_id] = merged.get(item.variant_id, 0) + item.quantity

    line_items = [{"item": {"id": vid}, "quantity": qty} for vid, qty in merged.items()]
    cart_data = await store_client.create_cart(line_items)
    subtotal, currency = _extract_cart_totals(cart_data)

    matched: list[CartLineItem] = []
    for li in cart_data.get("line_items") or []:
        item_info = li.get("item") or {}
        vid = str(item_info.get("id") or "")
        title = str(item_info.get("title") or "Store Item")
        price_minor = item_info.get("price", 0)
        matched.append(
            CartLineItem(
                material_id=vid,
                material_name=title,
                product_title=title,
                variant_id=vid,
                price=format_minor_price(price_minor, currency),
                quantity=int(li.get("quantity") or 1),
                image_url=item_info.get("image_url"),
            )
        )

    return ShoppingCartProposal(
        cart_id=cart_data.get("id"),
        checkout_url=cart_data.get("continue_url") or cart_data.get("checkout_url"),
        store_domain=settings.shopify_shop_domain or _extract_merchant(settings.shopify_mcp_url),
        subtotal=subtotal,
        currency=currency,
        matched_items=matched,
    )


async def build_shopping_cart_proposal(
    materials: list[MaterialItem],
    owned_material_ids: set[str] | None = None,
    global_limit_per_item: int = 3,
    *,
    settings: Settings | None = None,
) -> ShoppingCartProposal:
    """
    Two-tier Shopify MCP commerce flow:
      1. Showcase multi-merchant options via Shopify Global Catalog MCP (`catalog.shopify.com/api/ucp/mcp`).
      2. Match & build a live checkout cart exclusively on the configured mock dev store (`SHOPIFY_MCP_URL`).
    """
    settings = settings or get_settings()
    owned_ids = owned_material_ids or set()
    needed = [
        m for m in materials
        if m.status == MaterialStatus.BUY and m.id not in owned_ids
    ]

    store_domain = settings.shopify_shop_domain or _extract_merchant(settings.shopify_mcp_url)
    if not needed:
        return ShoppingCartProposal(store_domain=store_domain)

    global_client = ShopifyUCPClient.for_global_catalog(settings) if settings.shopify_global_mcp_url.strip() else None
    store_client = ShopifyUCPClient.for_store(settings) if settings.shopify_mcp_url.strip() else None

    global_options: list[GlobalProductOption] = []
    matched_items: list[CartLineItem] = []
    unmatched_materials: list[str] = []

    async with httpx.AsyncClient(timeout=15.0) as http_client:
        async def _query_global(mat: MaterialItem) -> list[GlobalProductOption]:
            if global_client is None:
                return []
            try:
                prods = await global_client.search_catalog(
                    mat.search_query or mat.name,
                    limit=global_limit_per_item,
                    client=http_client,
                )
                return _normalize_products_to_options(
                    prods,
                    material_id=mat.id,
                    material_name=mat.name,
                    max_options=global_limit_per_item,
                )
            except Exception as exc:
                logger.warning("Global Catalog MCP search failed for '%s': %s", mat.name, exc)
                return []

        async def _query_store(mat: MaterialItem) -> tuple[MaterialItem, GlobalProductOption | None]:
            if store_client is None:
                return mat, None
            try:
                prods = await _search_store_with_fallback(
                    store_client,
                    mat.search_query or mat.name,
                    http_client=http_client,
                    limit=3,
                )
                opts = _normalize_products_to_options(
                    prods,
                    material_id=mat.id,
                    material_name=mat.name,
                    default_merchant=store_domain,
                    max_options=1,
                )
                return mat, (opts[0] if opts else None)
            except Exception as exc:
                logger.warning("Mock Store MCP search failed for '%s': %s", mat.name, exc)
                return mat, None

        global_results, store_results = await asyncio.gather(
            asyncio.gather(*(_query_global(m) for m in needed)),
            asyncio.gather(*(_query_store(m) for m in needed)),
        )

        for opts in global_results:
            global_options.extend(opts)

        # Deduplicate variants for mock-store cart creation
        variant_quantities: dict[str, int] = {}
        for mat, opt in store_results:
            if opt is None or not opt.variant_id:
                unmatched_materials.append(mat.name)
                continue
            variant_quantities[opt.variant_id] = variant_quantities.get(opt.variant_id, 0) + 1
            matched_items.append(
                CartLineItem(
                    material_id=mat.id,
                    material_name=mat.name,
                    product_title=opt.product_title,
                    variant_id=opt.variant_id,
                    price=opt.price,
                    quantity=1,
                    rationale=f"Matched on mock checkout store ({store_domain})",
                    product_url=opt.product_url,
                    image_url=opt.image_url,
                )
            )

        cart_id: str | None = None
        checkout_url: str | None = None
        subtotal = "$0.00"
        currency = "USD"

        if store_client is not None and variant_quantities:
            try:
                line_items = [
                    {"item": {"id": vid}, "quantity": qty}
                    for vid, qty in variant_quantities.items()
                ]
                cart_data = await store_client.create_cart(line_items, client=http_client)
                cart_id = cart_data.get("id")
                checkout_url = cart_data.get("continue_url") or cart_data.get("checkout_url")
                subtotal, currency = _extract_cart_totals(cart_data)
            except Exception as exc:
                logger.warning("Failed to create mock store cart: %s", exc)

    return ShoppingCartProposal(
        cart_id=cart_id,
        checkout_url=checkout_url,
        store_domain=store_domain,
        subtotal=subtotal,
        currency=currency,
        matched_items=matched_items,
        global_options=global_options,
        unmatched_materials=unmatched_materials,
    )


async def place_dev_store_order(
    items: list[ShopifyCartLineRequest],
    note: str = "CraftPOV dev store order",
) -> dict[str, Any]:
    """
    Create and complete a draft order on the configured Shopify dev store
    using client_credentials OAuth + GraphQL Admin API.
    """
    settings = get_settings()
    shop_domain = (settings.shopify_shop_domain or _extract_merchant(settings.shopify_mcp_url)).strip()
    if not shop_domain:
        raise ValueError("SHOPIFY_SHOP_DOMAIN is not configured.")
    if not settings.shopify_client_id.strip() or not settings.shopify_client_secret.strip():
        raise ValueError("SHOPIFY_CLIENT_ID and SHOPIFY_CLIENT_SECRET are required.")

    merged: dict[str, int] = {}
    for item in items:
        merged[item.variant_id] = merged.get(item.variant_id, 0) + item.quantity
    line_items = [{"variantId": vid, "quantity": qty} for vid, qty in merged.items()]

    async with httpx.AsyncClient(timeout=20.0) as client:
        token_resp = await client.post(
            f"https://{shop_domain}/admin/oauth/access_token",
            data={
                "client_id": settings.shopify_client_id.strip(),
                "client_secret": settings.shopify_client_secret.strip(),
                "grant_type": "client_credentials",
            },
        )
        token_resp.raise_for_status()
        access_token = token_resp.json().get("access_token") or ""
        if not access_token:
            raise RuntimeError("Shopify OAuth did not return an access_token.")

        gql_url = f"https://{shop_domain}/admin/api/2025-10/graphql.json"
        headers = {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": access_token,
        }

        create_query = """
        mutation draftOrderCreate($input: DraftOrderInput!) {
          draftOrderCreate(input: $input) {
            draftOrder { id name totalPrice }
            userErrors { field message }
          }
        }
        """
        r1 = await client.post(
            gql_url,
            headers=headers,
            json={
                "query": create_query,
                "variables": {"input": {"note": note, "lineItems": line_items}},
            },
        )
        r1.raise_for_status()
        d1 = r1.json()
        if d1.get("errors"):
            raise RuntimeError(f"Shopify draftOrderCreate GraphQL error: {d1['errors']}")
        payload1 = (d1.get("data") or {}).get("draftOrderCreate") or {}
        if payload1.get("userErrors"):
            raise RuntimeError(f"Shopify draftOrderCreate userErrors: {payload1['userErrors']}")
        draft = payload1.get("draftOrder")
        if not draft or not draft.get("id"):
            raise RuntimeError("Shopify draftOrderCreate returned no draftOrder.")

        complete_query = """
        mutation draftOrderComplete($id: ID!) {
          draftOrderComplete(id: $id) {
            draftOrder {
              id
              order {
                id
                name
                totalPriceSet { shopMoney { amount currencyCode } }
              }
            }
            userErrors { field message }
          }
        }
        """
        r2 = await client.post(
            gql_url,
            headers=headers,
            json={"query": complete_query, "variables": {"id": draft["id"]}},
        )
        r2.raise_for_status()
        d2 = r2.json()
        if d2.get("errors"):
            raise RuntimeError(f"Shopify draftOrderComplete GraphQL error: {d2['errors']}")
        payload2 = (d2.get("data") or {}).get("draftOrderComplete") or {}
        if payload2.get("userErrors"):
            raise RuntimeError(f"Shopify draftOrderComplete userErrors: {payload2['userErrors']}")
        order = ((payload2.get("draftOrder") or {}).get("order")) or {}
        return {
            "draft_order_id": draft["id"],
            "order_id": order.get("id"),
            "order_name": order.get("name"),
            "total": ((order.get("totalPriceSet") or {}).get("shopMoney")) or {},
            "store_domain": shop_domain,
        }
