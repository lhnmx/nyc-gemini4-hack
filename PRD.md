# CraftPOV — Product Requirements Document (PRD)

**Event:** Google DeepMind NYC Gemini 4 Hackathon  
**Tracks:**
- **Problem Statement Two — Element 119** *(Multimodal In → Multimodal Out, Gemini 4 + Decisions API, live unscripted inputs)*
- **Bonus Track — Build with Commerce (Shopify / `shop.dev`)**

---

## 1. Executive Summary

Learning a physical skill (sculpting a clay figure, soldering a macro pad, leatherworking, origami) from YouTube is painful: angles are blocked, instructions are buried in 20+ minutes of chatter or silent non-English footage, and you often discover missing supplies mid-project.

**CraftPOV** is an autonomous multimodal build companion that:
1. **Watches & listens** to real YouTube tutorials (including silent/ASMR or non-English videos) using **Gemini 4 Carbon**.
2. **Reasons & grounds** the physical build into a continuity-locked **Physical State Graph** using **Gemini 4 Argon + Hydrogen** and the **Decisions API**.
3. **Inventories your desk & builds a live cart** via **Gemini 4 Neon + Shopify (`shop.dev`) Storefront MCP**.
4. **Generates first-person (POV) step-by-step video clips with voice narration** so you can follow along hands-free.
5. **Checks your real work-in-progress** via live webcam frames and gives spoken corrective feedback.

---

## 2. Target Users & Core Demo Flow

### Primary Persona
A maker/beginner at home with a physical goal (*"sculpt a polymer clay frog"*), a messy desk, and messy hands who wants clear, over-the-hands guidance without scrubbing 25-minute YouTube timelines.

### Hero User Journey (Live, Unscripted Demo)
1. **Intake**: User enters *any* physical craft goal (and optionally snaps/uploads a photo of their desk).
2. **Live Video Research (`0–15s`)**: Agent discovers top YouTube tutorials, filters non-craft filler via **Decisions API**, and watches raw video/audio to extract techniques, timing, and visible tools.
3. **Plan & Desk Diff (`15–25s`)**: UI streams in the structured step-by-step `BuildPlan` and Bill of Materials (BOM), automatically marking items spotted on the user's desk (`OWNED_EXACT` / `OWNED_SUBSTITUTE` / `BUY`).
4. **Shopify MCP Commerce (`20–30s`, parallel)**: Commerce Agent queries the connected Shopify (`shop.dev`) Storefront MCP for missing supplies, selects variants with rationale, and surfaces a one-click hosted **Checkout** button.
5. **POV Video + Voice Steps (`30s+`, progressive SSE)**: Loopable first-person POV video clips with synced voiceover unlock step-by-step as they render, maintaining consistent workbench, lighting, hands, and object geometry.
6. **Live Workbench Check-In**: User holds their real clay figure up to the webcam; **Decisions API + Hydrogen + Neon** evaluate geometry against the target step `end_state` (`PASS` / `ADJUST` / `REDO`) and speak concise feedback.

---

## 3. Functional Requirements

### P0 — Must-Have MVP (Core Hackathon Demo)

| ID | Feature | Description | Acceptance Criteria |
| :--- | :--- | :--- | :--- |
| **FR-1** | **Live Goal Intake** | Accept any arbitrary physical project prompt + optional desk photo upload. | Works on any unscripted craft query (not hardcoded to one example). |
| **FR-2** | **Raw YouTube Multimodal Ingestion** | Search YouTube Data API v3, filter segments via **Decisions API**, and analyze raw video/audio with **Gemini 4 Carbon**. | Extracts techniques & visible tools even from a silent/non-English tutorial with no transcript. |
| **FR-3** | **Physical Build Plan & State Graph** | Synthesize multi-video notes into `BuildPlan` (`Argon`) with `PhysicalStateTransition` + `VisualContinuityBible` (`Hydrogen`). | Produces 5–8 atomic steps with valid physical ordering, BOM, narration scripts, and POV prompts. |
| **FR-4** | **Desk-to-Cart Shopify MCP Agent** | Diff optional desk photo against BOM (`Decisions API` + `Neon`), search Shopify MCP (`search_shop_catalog`), and build a live cart (`update_cart`, `get_cart`). | Returns real product matches, prices, rationale, and a clickable hosted Shopify `checkout_url`; gracefully lists `unmatched_materials`. |
| **FR-5** | **Continuity-Aware POV Video + Voice** | Generate first-person POV keyframe → Veo POV video clip → TTS voiceover → FFmpeg muxed loopable MP4 per step. | Steps stream to the UI progressively via SSE; each clip loops cleanly with synced voice audio. |
| **FR-6** | **Interactive Web UI** | Single-page UI showing live pipeline progress, BOM checklist + Shopify cart drawer, and loopable POV step player. | User can toggle owned items to rebuild the cart and step through POV videos seamlessly. |

### P1 — High-Impact Differentiators (Judging Wow Factor)

| ID | Feature | Description | Acceptance Criteria |
| :--- | :--- | :--- | :--- |
| **FR-7** | **Live Webcam Workbench Check-In** | Capture a webcam frame at any step; compare real piece vs. target `end_state` via **Hydrogen + Decisions API + Neon**. | Returns `PASS` / `ADJUST` / `REDO` verdict + spoken corrective tip in `< 4s`. |
| **FR-8** | **Stress-Test & Breaking-Point Harness** | Run real hard inputs (silent/non-English video, 45-min messy livestream, cluttered desk photo) and log latency/accuracy/breaks. | Populates the breaking-point table in `ARCHITECTURE.md` with real empirical results for judges. |

### P2 — Stretch Goals

| ID | Feature | Description |
| :--- | :--- | :--- |
| **FR-9** | **Hands-Free Voice Navigation** | Browser speech commands (*"Next step"*, *"Replay"*, *"Check my work"*). |
| **FR-10** | **Full Stitched Export** | One-click download of the complete stitched POV tutorial MP4 with captions. |

---

## 4. Non-Functional Requirements & Track Guardrails

- **No Scripted/Mocked Inputs (Element 119 Rule)**: Every demo run must execute live against real YouTube videos, real Gemini 4 + Decisions API calls, real Shopify MCP endpoints, and real generated media.
- **Progressive Perceived Speed**: First meaningful UI update (`plan_ready` + `cart_ready`) must arrive within **< 30s** via SSE while POV videos render asynchronously in the background.
- **Resilience**: Partial failures (e.g., 1 of 3 YouTube videos unavailable, or 1 BOM item not in Shopify catalog) must degrade gracefully without crashing the pipeline.
- **Model Routing Configurability**: All Gemini 4 model slugs (`Argon`, `Carbon`, `Neon`, `Hydrogen`, Veo) must be driven by `.env` so the team can hot-swap IDs during the hackathon.

---

## 5. Judging Rubric Checklist

- [ ] **Element 119 — Raw Media Necessity**: Demo proves value that text cannot replicate (silent/non-English video parsing, desk photo visual diffing, POV video + voice output, webcam geometry critique).
- [ ] **Element 119 — Decisions API**: Visible, inspectable `Decisions API` verdicts in logs/UI (segment filtering, tool substitution, step pass/retry).
- [ ] **Element 119 — Pushing Limits & Breaking Points**: Live evidence of hardest tested inputs, where it broke, and the architectural fix.
- [ ] **Shopify (`shop.dev`) Bonus Track**: End-to-end product discovery → autonomous shopping agent → live Shopify cart & checkout URL.

---

## 6. Implementation Plan (Vertical Slices)

Build in 4 fast vertical slices so you always have a working end-to-end demo:

1. **Slice 1 — Core Foundation & Config**: `config.py`, `models.py`, Gemini 4 client wrapper (with Decisions API helper), and dependencies in `pyproject.toml`.
2. **Slice 2 — YouTube Research + Planner (`Carbon` + `Argon` + `Hydrogen`)**: Live YouTube search → video analysis → structured `BuildPlan` JSON from CLI/API.
3. **Slice 3 — Shopify (`shop.dev`) MCP Commerce (`Neon`)**: Desk-photo diffing + MCP client (`search_shop_catalog` → `update_cart` → `get_cart`) returning a real checkout link.
4. **Slice 4 — POV Media Pipeline + Web UI + Live Check-In**: Keyframe → Veo POV clip → TTS → FFmpeg muxing + FastAPI SSE backend + clean Web UI + webcam check-in endpoint.
