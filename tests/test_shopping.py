import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from nyc_gemini4_hack.config import Settings
from nyc_gemini4_hack.server import create_app
from nyc_gemini4_hack.shopping import GLOBAL_CATALOG, PUBLIC_PROFILE, catalog_config, search_catalog


def settings(**overrides):
    values = dict(shopify_mcp_url="", shopify_shop_domain="", shopify_agent_profile_url="",
                  gemini_api_key="", youtube_api_key="")
    return Settings(_env_file=None, **(values | overrides))


class ShoppingTests(unittest.IsolatedAsyncioTestCase):
    def test_default_catalog_and_optional_merchant_configuration(self):
        self.assertEqual(catalog_config(settings()), (GLOBAL_CATALOG, PUBLIC_PROFILE))
        endpoint = "https://supplies.example/api/ucp/mcp"
        profile = "https://guide.example/ucp-agent-profile.json"
        global_endpoint = "https://global.example/api/ucp/mcp"
        upstream = settings(shopify_global_mcp_url=global_endpoint, shopify_ucp_agent_profile_url=profile)
        self.assertEqual(catalog_config(upstream), (global_endpoint, profile))
        self.assertIsNone(catalog_config(settings(shopify_global_mcp_url="")))
        self.assertIsNone(catalog_config(settings(shopify_global_mcp_url="http://global.example/api/ucp/mcp")))
        for config in ({"shopify_shop_domain": "supplies.example"},
                       {"shopify_mcp_url": "https://supplies.example/api/mcp"},
                       {"shopify_mcp_url": endpoint}):
            self.assertEqual(catalog_config(settings(**config, shopify_agent_profile_url=profile)), (endpoint, profile))
        self.assertIsNone(catalog_config(settings(shopify_agent_profile_url="invalid")))
        self.assertIsNone(catalog_config(settings(shopify_shop_domain="supplies.example/path")))

    async def test_only_searches_and_returns_the_original_merchant_urls_for_available_variants(self):
        store_url = "https://supplies.example/products/cards?variant=002&shclid=a%2Fb"
        checkout = "https://checkout.example/cart/002:1?shclid=a%2Fb"
        variants = [
            {"title": "Blue", "availability": {"available": False}, "url": store_url, "checkout_url": checkout},
            {"title": "White", "availability": {"available": True}, "url": store_url, "checkout_url": checkout,
             "seller": {"name": " Stationery Co. "},
             "media": [{"type": "image", "url": "https://cdn.example/cards.jpg"}],
             "price": {"amount": 629, "currency": "USD"}},
        ]
        def reply(request):
            self.assertEqual(request.url, GLOBAL_CATALOG)
            self.assertEqual(request.method, "POST")
            self.assertNotIn("Authorization", request.headers)
            body = json.loads(request.content)
            self.assertEqual(body["params"], {"name": "search_catalog", "arguments": {
                "meta": {"ucp-agent": {"profile": PUBLIC_PROFILE}},
                "catalog": {"query": "card stock", "pagination": {"limit": 3}},
            }})
            products = [
                {"title": "Unsafe", "url": "http://insecure.example/products/cards"},
                {"title": "Invalid link", "url": "https://supplies.example:abc/"},
                {"title": "Sold out", "url": store_url, "variants": [variants[0]]},
                {"title": "Card stock", "variants": variants},
                {"title": "Duplicate listing", "variants": variants},
                {"title": "Plain cards", "url": "https://supplies.example/products/plain", "media": [
                    {"type": "image", "url": "javascript:invalid"}, {"type": "image", "url": "https://cdn.example/plain.jpg"},
                ], "variants": [
                    {"checkout_url": checkout, "title": "Default Title", "price": {"currency": "XYZ", "amount": "six"}},
                ]},
            ]
            return httpx.Response(200, json={"result": {"structuredContent": {"products": products}}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            offers = await search_catalog(GLOBAL_CATALOG, PUBLIC_PROFILE, "card stock", client)
        self.assertEqual(offers, [
            {"title": "Card stock", "variant": "White", "url": store_url, "checkout_url": checkout,
             "price": {"amount": 629, "currency": "USD"}, "seller": "Stationery Co.", "image_url": "https://cdn.example/cards.jpg"},
            {"title": "Plain cards", "variant": None, "url": "https://supplies.example/products/plain",
             "checkout_url": None, "price": None, "seller": None, "image_url": "https://cdn.example/plain.jpg"},
        ])

    async def test_streamed_catalog_result_arrives_before_the_connection_closes(self):
        url = "https://supplies.example/products/paper"
        payload = {"products": [{"title": "Paper", "url": url}]}
        result = {"content": [{"type": "text", "text": json.dumps(payload)}]}

        class CatalogEvents(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b": keepalive\r\n\r\n"
                yield b'data: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
                yield b'data: {"jsonrpc":"2.0","id":9,"result":{}}\n\n'
                yield b'event: message\ndata: {"jsonrpc":"2.0","id":1,\n'
                yield ('data: "result":' + json.dumps(result) + '}\n\n').encode()
                raise AssertionError("The matching response should complete the lookup immediately")

        def reply(request):
            return httpx.Response(200, headers={"Content-Type": "text/event-stream; charset=utf-8"},
                                  stream=CatalogEvents())

        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            offers = await search_catalog(GLOBAL_CATALOG, PUBLIC_PROFILE, "paper", client)
        self.assertEqual([offer["url"] for offer in offers], [url])

    def test_search_is_independent_of_guide_storage_and_provider_credentials(self):
        with tempfile.TemporaryDirectory() as root:
            config = settings(sqlite_path=Path(root) / "guide.sqlite3")
            with TestClient(create_app(settings=config)) as client:
                health = client.get("/api/health").json()
                self.assertTrue(health["capabilities"]["material_catalog"])
                self.assertFalse(health["generation_ready"])
                with patch("nyc_gemini4_hack.shopping.search_catalog", new_callable=AsyncMock) as search:
                    search.return_value = []
                    response = client.get("/api/shopping/search", params={"q": "Paper"})
                    self.assertEqual(response.json(), {"products": []})
                    self.assertEqual(response.headers["cache-control"], "no-store")
                    for timeout in (httpx.ReadTimeout("network timeout"), TimeoutError("lookup deadline")):
                        search.side_effect = timeout
                        self.assertEqual(client.get("/api/shopping/search", params={"q": "Paper"}).status_code, 502)
                    self.assertEqual(client.get("/api/shopping/search", params={"q": " "}).status_code, 422)
                    self.assertEqual(search.await_count, 3)
