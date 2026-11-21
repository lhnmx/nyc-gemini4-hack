import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nyc_gemini4_hack.config import Settings
from nyc_gemini4_hack.frontend import mount_frontend
from nyc_gemini4_hack.job_store import open_job_store
from nyc_gemini4_hack.models import MaterialItem, ProjectState
from nyc_gemini4_hack.server import create_app
from tests.test_pipeline import paper_plan

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "ucp-agent-profile.json"


class CommercePreservationTests(unittest.TestCase):
    def assert_public_profile(self, client):
        response = client.get("/ucp-agent-profile.json", follow_redirects=False)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"].split(";")[0], "application/json")
        self.assertEqual(response.content, PROFILE.read_bytes())
        head = client.head("/ucp-agent-profile.json", follow_redirects=False)
        self.assertEqual(head.status_code, 200)
        self.assertEqual(int(head.headers["content-length"]), len(response.content))

    def test_agent_profile_advertises_upstream_mcp_services_and_capabilities(self):
        ucp = json.loads(PROFILE.read_bytes())["ucp"]
        version = "2026-08-25"
        service = {"transport": "mcp", "version": version,
                   "spec": f"https://ucp.dev/{version}/specification/overview/"}
        self.assertEqual(ucp["version"], version)
        self.assertEqual(ucp["services"], {"dev.ucp.shopping": [service], "dev.ucp.common": [service]})
        self.assertEqual(ucp["capabilities"], {
            "dev.ucp.shopping." + name: [{"version": version}]
            for name in ("catalog.search", "catalog.lookup", "cart", "checkout")
        })
        self.assertEqual(ucp["payment_handlers"], {})

    def test_local_frontend_serves_public_profile(self):
        app = FastAPI()
        mount_frontend(app)
        with TestClient(app) as client:
            self.assert_public_profile(client)

    @unittest.skipUnless((ROOT / "frontend/dist/index.html").is_file(), "Build the frontend to check the hosted assets")
    def test_hosted_frontend_serves_built_profile_without_storage_credentials(self):
        import app as deployment

        with patch.dict(os.environ, {"TURSO_DATABASE_URL": "", "TURSO_AUTH_TOKEN": ""}):
            with TestClient(deployment.app) as client:
                self.assert_public_profile(client)

    def test_cart_rebuild_without_a_plan_preserves_saved_cart(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        settings = Settings(_env_file=None, gemini_api_key="", youtube_api_key="",
                            shopify_mcp_url="https://shop.example/api/mcp", sqlite_path=root / "guide.sqlite3")
        cart = {
            "cart_id": "saved-cart", "checkout_url": "https://shop.example/checkout/saved",
            "matched_items": [{
                "material_id": "paper", "material_name": "Paper", "product_title": "Origami paper",
                "variant_id": "gid://shopify/ProductVariant/123", "price": "5.00",
                "rationale": "Square sheets", "product_url": "https://shop.example/products/paper",
                "image_url": "https://shop.example/paper.jpg",
            }],
            "unmatched_materials": ["ruler"],
        }

        async def save_existing_project():
            async with open_job_store(settings.sqlite_path) as store:
                state = ProjectState.model_validate({"project_id": "origami", "goal": "Fold paper", "cart": cart})
                await store.enqueue(state)

        asyncio.run(save_existing_project())
        with TestClient(create_app(settings=settings)) as client:
            health = client.get("/api/health").json()
            self.assertTrue(health["shopify_mcp_configured"])
            self.assertTrue(health["capabilities"]["commerce"])
            owned = ["ruler", "paper"]
            response = client.post("/api/projects/origami/cart/rebuild", json={"owned_material_ids": owned})
            self.assertEqual(response.status_code, 400)
            saved_cart = client.get("/api/projects/origami").json()["cart"]
            self.assertEqual(saved_cart["cart_id"], cart["cart_id"])
            self.assertEqual(saved_cart["checkout_url"], cart["checkout_url"])
            self.assertEqual(saved_cart["unmatched_materials"], cart["unmatched_materials"])
            self.assertEqual(saved_cart["matched_items"][0], cart["matched_items"][0] | {"quantity": 1})
            self.assertEqual(client.post("/api/projects/missing/cart/rebuild", json={}).status_code, 404)

    def test_search_checkout_and_rebuild_use_app_settings_and_scope_store_auth(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        store = "https://shop.example/api/ucp/mcp"
        catalog = "https://catalog.example/api/ucp/mcp"
        profile = "https://guide.example/ucp-agent-profile.json"
        store_token = "test-store-token"
        settings = Settings(_env_file=None, gemini_api_key="", youtube_api_key="", sqlite_path=root / "jobs.sqlite3",
                            shopify_mcp_url=store, shopify_global_mcp_url=catalog, shopify_shop_domain="shop.example",
                            shopify_agent_profile_url=profile, shopify_storefront_token=store_token)
        calls = []
        cart_lines = []

        async def save_project():
            plan = paper_plan()
            plan.materials = [MaterialItem(id="paper", name="Paper", search_query="paper")]
            async with open_job_store(settings.sqlite_path) as jobs:
                await jobs.enqueue(ProjectState(project_id="paper", goal="Fold paper", plan=plan))

        asyncio.run(save_project())

        def respond(request):
            endpoint = str(request.url)
            rpc = json.loads(request.content)
            self.assertEqual((rpc["jsonrpc"], rpc["method"]), ("2.0", "tools/call"))
            params = rpc["params"]
            calls.append((endpoint, params["name"], request.headers.get("Authorization"),
                          params["arguments"]["meta"]["ucp-agent"]["profile"]))
            if params["name"] == "search_catalog":
                source = "Store" if endpoint == store else "Global"
                title = f"{source} paper"
                content = {"products": [{"id": "p1", "title": title, "variants": [
                    {"id": f"{source.lower()}-paper", "url": "https://shop.example/products/paper",
                     "price": {"amount": 350, "currency": "USD"}}
                ]}]}
            else:
                self.assertEqual((endpoint, params["name"]), (store, "create_cart"))
                lines = params["arguments"]["cart"]["line_items"]
                cart_lines.append(lines)
                amount = 350 * sum(line["quantity"] for line in lines)
                content = {"id": "cart1", "continue_url": "https://shop.example/cart/cart1",
                           "currency": "USD", "totals": [{"type": "total", "amount": amount}]}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rpc["id"], "result": {"structuredContent": content}})

        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(respond)
        with (
            patch.object(httpx, "AsyncClient", side_effect=lambda **options: real_client(transport=transport, **options)),
            TestClient(create_app(settings=settings)) as client,
        ):
            search = client.post("/api/shopify/search", json={"query": "paper", "scope": "both"})
            self.assertEqual(search.status_code, 200)
            self.assertEqual([o["product_title"] for o in search.json()["global_options"]], ["Global paper"])
            self.assertEqual([o["product_title"] for o in search.json()["store_options"]], ["Store paper"])
            cart = client.post("/api/shopify/cart", json={"items": [{"variant_id": "store-paper", "quantity": 2}]})
            self.assertEqual(cart.status_code, 200)
            self.assertEqual(cart.json()["checkout_url"], "https://shop.example/cart/cart1")
            self.assertEqual(cart.json()["subtotal"], "$7.00")
            rebuilt = client.post("/api/projects/paper/cart/rebuild", json={"owned_material_ids": []})
            self.assertEqual(rebuilt.status_code, 200)
            self.assertEqual(rebuilt.json()["subtotal"], "$3.50")
            self.assertEqual([item["variant_id"] for item in rebuilt.json()["global_options"]], ["global-paper"])
            self.assertEqual([item["variant_id"] for item in rebuilt.json()["matched_items"]], ["store-paper"])
            self.assertEqual(client.get("/api/projects/paper").json()["cart"], rebuilt.json())
        store_auth = f"Bearer {store_token}"
        self.assertCountEqual(calls, [(store, "search_catalog", store_auth, profile),
                                      (catalog, "search_catalog", None, profile),
                                      (store, "create_cart", store_auth, profile)] * 2)
        self.assertEqual(cart_lines, [[{"item": {"id": "store-paper"}, "quantity": quantity}] for quantity in (2, 1)])
