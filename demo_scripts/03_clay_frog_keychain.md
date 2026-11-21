# Demo 3: Clay Frog Keychain

**Best for:** Hero demo for **Build with Commerce (Shopify MCP)** + **Element 119** (3D sculpting techniques, multi-item shopping cart, and live webcam check-in).

## Prompt (Copy & Paste)

- **Your project:**
  ```text
  Sculpt a polymer clay frog keychain
  ```
  *(Alternative if you specifically want silicone molds in the BOM: `Make a polymer clay frog keychain using silicone molds`)*

- **Supplies you have (optional):**
  *(Leave blank for the fullest shopping cart, or enter `baking sheet, oven` to demonstrate owned-vs-buy diffing.)*
  ```text
  baking sheet, oven
  ```

## CLI Command

```bash
uv run python -m nyc_gemini4_hack.cli "Sculpt a polymer clay frog keychain" --owned "baking sheet, oven"
```

## Expected Supplies to Buy (Multi-Item Cart)

- Polymer clay (green, white, black, pink)
- Silicone mold *(if using the mold variation)*
- Clay sculpting & dotting tools
- Acrylic roller / cutting blade
- Eye pins & keychain rings
- Gloss polymer clay glaze / varnish

## Demo Flow & Talking Points

1. **Rich Multi-Item BOM**: Unlike origami, sculpting a clay frog keychain requires multiple specialized supplies—the agent extracts both spoken and *unmentioned visible* tools from YouTube sculpting videos.
2. **Shopify Global Catalog + Live Checkout Cart**:
   - Show multi-merchant options pulled live from Shopify Global Catalog MCP (e.g., Sculpey Premo, silicone molds, sculpting tools).
   - Click the hosted **Checkout** button built on your Shopify dev store with multiple line items and a real subtotal.
3. **Physical Continuity & POV Film**: Walk through the 3D state progression (conditioning clay → shaping body/eyes → inserting eye pin → baking & glazing).
4. **Live Workbench Coach (Wow Factor)**: Hold a clay piece (or test object) up to the webcam to get a real-time `PASS` / `ADJUST` / `REDO` visual critique and spoken tip.
