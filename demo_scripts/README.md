# CraftPOV Demo Scripts

Three ready-to-run demo scenarios designed to showcase **Element 119** (multimodal video/audio ingestion, Decisions API gating, POV narrated film, live workbench check-in) and **Build with Commerce** (Shopify Global Catalog + Storefront UCP MCP checkout).

## Demo Lineup

| # | Script | Prompt | What It Showcases |
| :--- | :--- | :--- | :--- |
| **1** | [01_paper_glider.md](./01_paper_glider.md) | `Fold a paper glider` | Fast, clean baseline — minimal supplies, crisp folding continuity, quick narrated POV film. |
| **2** | [02_himmel_und_hoelle.md](./02_himmel_und_hoelle.md) | `Fold a Himmel und Hölle origami paper fortune teller` | Non-English / visual-first YouTube tutorials (German *Himmel und Hölle*), Decisions API video filtering, origami paper shopping. |
| **3** | [03_clay_frog_keychain.md](./03_clay_frog_keychain.md) | `Sculpt a polymer clay frog keychain` | Multi-item BOM & Shopify cart (clay, silicone molds, sculpting tools, glaze, keychain rings), 3D physical state transitions, and live webcam check-in. |

## Recommended Products to Add to Your Shopify Dev Store

To make sure **Demo 3 (Clay Frog Keychain)** and **Demo 2 (Himmel und Hölle)** populate a rich multi-item checkout cart on your mock store (`paper-store-dyzpqjzv.myshopify.com`), add these products with non-zero prices:

- **Origami Paper** *(already in store — set a price like `$6.99`)*
- **Polymer Clay Starter Pack (Green, White, Black, Pink)** — `$12.99`
- **Silicone Clay Mold Set** — `$9.99`
- **Clay Sculpting & Dotting Tool Set** — `$8.99`
- **Acrylic Clay Roller & Cutting Blade** — `$11.99`
- **Gloss Polymer Clay Glaze / Varnish** — `$7.99`
- **Keychain Rings & Eye Pins Hardware Kit** — `$6.49`
- **Fine-Tip Markers Set** — `$5.99`
- **Ruler & Bone Folder** — `$4.99`

## Quick CLI Runner Reference

```bash
# 1. Paper Glider
uv run python -m nyc_gemini4_hack.cli "Fold a paper glider" --owned "paper"

# 2. Himmel und Hölle
uv run python -m nyc_gemini4_hack.cli "Fold a Himmel und Hölle origami paper fortune teller"

# 3. Clay Frog Keychain (leave owned empty to trigger full shopping cart)
uv run python -m nyc_gemini4_hack.cli "Sculpt a polymer clay frog keychain"
```
