"""Read-only product discovery through Shopify's UCP Catalog MCP."""
from __future__ import annotations

import asyncio
import json
import re
from typing import Annotated
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import HttpUrl

from nyc_gemini4_hack.config import Settings

GLOBAL_CATALOG = "https://catalog.shopify.com/api/ucp/mcp"
# Shopify requires a public application/json profile. This is our checked-in
# ucp-agent-profile.json, pinned to the upstream revision that published it.
PUBLIC_PROFILE = (
    "https://cdn.jsdelivr.net/gh/lhnmx/nyc-gemini4-hack@"
    "b6ec64927c3feb6c0670ff0bf5715e33b590b208/ucp-agent-profile.json"
)
RESULT_LIMIT = 3


def https_url(value: object) -> str | None:
    if not isinstance(value, str) or not value.startswith("https://"):
        return None
    if any(ord(char) < 32 or char.isspace() or char == "\\" for char in value):
        return None
    try:
        url = HttpUrl(value)
        if url.host and url.username is None and url.password is None:
            return value
    except ValueError:
        pass
    return None


def catalog_config(settings: Settings) -> tuple[str, str] | None:
    endpoint = settings.shopify_mcp_url.strip()
    domain = settings.shopify_shop_domain.strip().rstrip("/")
    if endpoint:
        if not https_url(endpoint):
            return None
        url = urlsplit(endpoint)
        # Shopify moved the original Storefront MCP catalog to its UCP endpoint.
        if url.path.rstrip("/") == "/api/mcp":
            endpoint = url._replace(path="/api/ucp/mcp").geturl()
    elif domain:
        base = domain if "://" in domain else f"https://{domain}"
        if not https_url(base) or any(urlsplit(base)[2:]):
            return None
        endpoint = f"{base}/api/ucp/mcp"
    else:
        endpoint = settings.shopify_global_mcp_url.strip()
        if not https_url(endpoint):
            return None
    profile = https_url(settings.shopify_profile_url or PUBLIC_PROFILE)
    return (endpoint, profile) if profile else None


def catalog_price(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    amount, currency = value.get("amount"), value.get("currency")
    if type(amount) is int and 0 <= amount <= 2**53 - 1 and isinstance(currency, str) and re.fullmatch("[A-Z]{3}", currency):
        return {"amount": amount, "currency": currency}
    return None


def catalog_image(*media_groups: object) -> str | None:
    for media in media_groups:
        if not isinstance(media, list):
            continue
        for asset in media:
            if isinstance(asset, dict) and asset.get("type") == "image":
                if url := https_url(asset.get("url")):
                    return url
    return None


def product_offer(product: dict) -> dict | None:
    title = product.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    page = https_url(product.get("url"))
    variants = product.get("variants")
    if variants is not None and not isinstance(variants, list):
        return None
    for variant in variants or [{}]:
        if not isinstance(variant, dict):
            continue
        availability = variant.get("availability")
        available = availability.get("available") if isinstance(availability, dict) else None
        if available is False:
            continue
        url = https_url(variant.get("url")) or page
        checkout = https_url(variant.get("checkout_url")) if available is True else None
        if not (url or checkout):
            continue
        option = variant.get("title")
        if not isinstance(option, str) or option.casefold() in {title.casefold(), "default title"}:
            option = None
        seller = variant.get("seller")
        name = seller.get("name") if isinstance(seller, dict) else None
        return {
            "title": title, "variant": option, "url": url, "checkout_url": checkout,
            "price": catalog_price(variant.get("price")),
            "seller": (name.strip() or None) if isinstance(name, str) else None,
            "image_url": catalog_image(variant.get("media"), product.get("media")),
        }
    return None


async def search_catalog(endpoint: str, profile: str, query: str, client: httpx.AsyncClient) -> list[dict]:
    arguments = {"meta": {"ucp-agent": {"profile": profile}},
                 "catalog": {"query": query, "pagination": {"limit": RESULT_LIMIT}}}
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "search_catalog", "arguments": arguments},
    }
    async with client.stream("POST", endpoint, json=request,
                             headers={"Accept": "application/json, text/event-stream"}) as response:
        response.raise_for_status()
        envelope = await catalog_reply(response, request["id"])
    result = envelope.get("result") if isinstance(envelope, dict) else None
    if not isinstance(result, dict) or result.get("isError"):
        raise ValueError("Catalog call failed")
    payload = result.get("structuredContent")
    if payload is None:
        for block in result.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                payload = json.loads(block["text"])
                break
    products = payload.get("products") if isinstance(payload, dict) else None
    if not isinstance(products, list):
        raise ValueError("Missing catalog products")
    offers = {}
    for product in products:
        if isinstance(product, dict) and (offer := product_offer(product)):
            offers.setdefault(offer["checkout_url"] or offer["url"], offer)
        if len(offers) == RESULT_LIMIT:
            break
    return list(offers.values())


async def catalog_reply(response: httpx.Response, request_id: int) -> dict:
    media_type = response.headers.get("content-type", "").partition(";")[0].strip().lower()
    if media_type != "text/event-stream":
        await response.aread()
        return response.json()

    # MCP may send notifications or keepalives before the response to this call.
    data = []
    async for line in response.aiter_lines():
        if line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
        elif not line and data:
            message = "\n".join(data)
            data.clear()
            if message:
                envelope = json.loads(message)
                if (isinstance(envelope, dict) and envelope.get("id") == request_id
                        and ("result" in envelope or "error" in envelope)):
                    return envelope
    raise ValueError("Missing catalog response")


def shopping_router(settings: Settings) -> APIRouter:
    router = APIRouter()

    @router.get("/api/shopping/search")
    async def search(response: Response, q: Annotated[str, Query(min_length=1, max_length=300)]) -> dict:
        query = q.strip()
        if not query:
            raise HTTPException(422, "Enter a material to search for.")
        config = catalog_config(settings)
        if config is None:
            raise HTTPException(503, "The Shopify catalog needs configuration.")
        try:
            # Bound the whole lookup, including a stream that only sends keepalives.
            async with asyncio.timeout(8.0), httpx.AsyncClient(timeout=None) as client:
                products = await search_catalog(*config, query, client)
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError):
            raise HTTPException(502, "The Shopify catalog is unavailable. Try again.") from None
        response.headers["Cache-Control"] = "no-store"
        return {"products": products}

    return router
