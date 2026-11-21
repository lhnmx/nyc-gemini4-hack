"""Vercel FastAPI function and the compiled Vite frontend, promoted to the CDN."""
from nyc_gemini4_hack.server import create_app

app = create_app(hosted=True)
app.frontend("/", directory="frontend/dist", fallback=None, check_dir=False)
