"""Local FastAPI application with an embedded SQLite rendering worker."""
from nyc_gemini4_hack.frontend import mount_frontend
from nyc_gemini4_hack.server import create_app

app = create_app()
mount_frontend(app)
