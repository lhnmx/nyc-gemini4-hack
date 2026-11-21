import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nyc_gemini4_hack.config import Settings
from nyc_gemini4_hack.frontend import mount_frontend
from nyc_gemini4_hack.job_store import open_job_store
from nyc_gemini4_hack.models import ProjectState
from nyc_gemini4_hack.server import create_app

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

    def test_cart_rebuild_acknowledges_ownership_and_preserves_saved_cart(self):
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
            self.assertFalse(health["capabilities"]["commerce"])
            owned = ["ruler", "paper"]
            response = client.post("/api/projects/origami/cart/rebuild", json={"owned_material_ids": owned})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {
                "project_id": "origami", "owned_material_ids": owned,
                "message": "Shopify MCP cart rebuild will be wired in Slice 3.",
            })
            self.assertEqual(client.get("/api/projects/origami").json()["cart"], cart)
            self.assertEqual(client.post("/api/projects/missing/cart/rebuild", json={}).status_code, 404)
