# CraftPOV — Project Status Log

## 2026-11-21 11:42 EST — Checkpoint

### ✅ Working & Verified Live
- **Project Docs & Setup**: `README.md`, `ARCHITECTURE.md`, `PRD.md`, `.env.example`, `.gitignore` (`.env` and `outputs/` ignored).
- **Gemini 4 + Native Decisions API Client** (`src/nyc_gemini4_hack/gemini_client.py`):
  - Verified live on `gemini-4-argon`, `gemini-4-carbon`, `gemini-4-hydrogen`, `gemini-4-neon`.
  - Native **Decisions API** verified live using `model="gemini-decision-1"` with `api_version="v1alpha"` (returns calibrated probabilities with `0` output tokens).
- **YouTube Research & Physical Planning Pipeline** (`agents/researcher.py`, `agents/planner.py`, `pipeline.py`, `cli.py`):
  - Searches YouTube Data API v3 → gates candidate tutorials via `gemini-decision-1` → watches raw video/audio with **Gemini 4 Carbon** → synthesizes steps & BOM with **Gemini 4 Argon** → grounds `VisualContinuityBible` and `Start → Hands → End` physical state transitions with **Gemini 4 Hydrogen**.
  - Verified live on `"sculpt a polymer clay frog"` (7 steps, 8 BOM items).
- **Voiceover + FFmpeg Composer** (`media/voice_gen.py`, `media/composer.py`):
  - Bundled `imageio-ffmpeg` working with zero external Homebrew install; outputs per-step MP4s + stitched `full_pov_tutorial.mp4`.
- **FastAPI + SSE Backend & UI Shell** (`api.py`, `web/index.html`):
  - Endpoints `/`, `/api/health`, `/api/projects`, `/api/projects/{id}/events` (SSE), `/api/projects/{id}`, `/media/*`.

---

### 🔧 Needs Work Next
1. **POV Video Generation (`media/video_gen.py`)**:
   - Currently generates 3 continuity-chained static POV JPEGs (`gemini-3.1-flash-image`) animated with FFmpeg `zoompan` crossfades (looks like slightly blurry stills with no true hand animation).
   - **Why**: The current Gemini Developer API key does not expose `veo-*` models (`generate_videos` requires **Gemini Enterprise Agent Platform** mode: `genai.Client(enterprise=True, project=..., location="us-central1")`).
   - **Next Step**: Wire real Image-to-Video (Start → End frame interpolation) via either GCP `enterprise=True` (Veo 3) or `fal.ai` (`fal-ai/veo3` / `kling-video`), and upgrade keyframes to `gemini-3-pro-image`.
2. **Shopify (`shop.dev`) Storefront MCP Commerce (`agents/commerce.py`)**:
   - Not yet implemented.
   - **Next Step**: Add desk-photo visual inventory diffing (`gemini-decision-1` + `gemini-4-neon`) and connect to Shopify Storefront MCP (`search_shop_catalog` → `update_cart` → `get_cart`) to generate a real checkout link for missing BOM items.
3. **Final Polish (Later)**:
   - **Live Workbench Coach** (`agents/coach.py`): webcam frame check-in (`PASS` / `ADJUST` / `REDO`) using `Hydrogen` + `gemini-decision-1` + `Neon`.
   - **Web UI** (`web/index.html`): Wire SSE listeners (`plan_ready`, `cart_ready`, `step_media_ready`) to render live BOM, Shopify cart, and POV video player in the browser.
