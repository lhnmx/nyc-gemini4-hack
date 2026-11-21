from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ModelRole = Literal["argon", "carbon", "neon", "hydrogen"]


class Settings(BaseSettings):
    """Application settings loaded from environment variables / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Google DeepMind / Gemini 4 API
    gemini_api_key: str = Field(default="", description="Gemini API key")
    gemini_model_argon: str = Field(default="gemini-4-argon")
    gemini_model_carbon: str = Field(default="gemini-4-carbon")
    gemini_model_neon: str = Field(default="gemini-4-neon")
    gemini_model_hydrogen: str = Field(default="gemini-4-hydrogen")
    gemini_decisions_enabled: bool = Field(default=True)
    gemini_decisions_model: str = Field(default="gemini-decision-1")
    gemini_decisions_api_version: str = Field(default="v1beta")

    # Media Generation (POV Keyframes / Video / Voice)
    pov_media_mode: Literal["motion", "illustrated_frames"] = "motion"
    pov_video_model: str = Field(default="gemini-omni-1.2-flash")
    pov_image_model: str = Field(default="gemini-3.1-flash-image")
    audio_model: str = Field(default="gemini-3.8-flash-tts")
    live_coach_model: str = Field(default="gemini-3.8-live")
    live_coach_api_version: str = Field(default="v1alpha")
    veo_model: str = Field(default="veo-3.0-generate-preview")
    imagen_model: str = Field(default="imagen-3.0-generate-002")
    tts_voice: str = Field(default="Aoede")

    # YouTube Data API v3
    youtube_api_key: str = Field(default="")

    # Shopify Commerce (Global Catalog MCP + Mock Store UCP MCP)
    shopify_mcp_url: str = Field(default="")
    shopify_global_mcp_url: str = Field(default="https://catalog.shopify.com/api/ucp/mcp")
    shopify_ucp_agent_profile_url: str = Field(
        default="https://cdn.jsdelivr.net/gh/lhnmx/nyc-gemini4-hack@b6ec64927c3feb6c0670ff0bf5715e33b590b208/ucp-agent-profile.json"
    )
    shopify_storefront_token: str = Field(default="")
    shopify_client_id: str = Field(default="")
    shopify_client_secret: str = Field(default="")
    shopify_shop_domain: str = Field(default="")

    # Server & Storage
    app_host: str = Field(default="0.0.0.0")
    app_port: int = Field(default=8000)
    media_output_dir: Path = Field(default=Path("./outputs"))
    sqlite_path: Path = Field(default=Path("./data/craftpov.sqlite3"))

    def model_for(self, role: ModelRole) -> str:
        mapping: dict[ModelRole, str] = {
            "argon": self.gemini_model_argon,
            "carbon": self.gemini_model_carbon,
            "neon": self.gemini_model_neon,
            "hydrogen": self.gemini_model_hydrogen,
        }
        return mapping[role]

    def ensure_output_dir(self) -> Path:
        self.media_output_dir.mkdir(parents=True, exist_ok=True)
        return self.media_output_dir

    def readiness_summary(self) -> dict[str, object]:
        return {
            "gemini_configured": bool(self.gemini_api_key.strip()),
            "youtube_configured": bool(self.youtube_api_key.strip()),
            "shopify_mcp_configured": bool(self.shopify_mcp_url.strip()),
            "shopify_global_mcp_configured": bool(self.shopify_global_mcp_url.strip()),
            "decisions_api_enabled": self.gemini_decisions_enabled,
            "capabilities": {
                "media_mode": self.pov_media_mode,
                "commerce": bool(self.shopify_mcp_url.strip()),
                "live_coach": bool(self.gemini_api_key.strip()),
                "desk_inventory": False,
            },
            "models": {
                "argon": self.gemini_model_argon,
                "carbon": self.gemini_model_carbon,
                "neon": self.gemini_model_neon,
                "hydrogen": self.gemini_model_hydrogen,
                "decisions": self.gemini_decisions_model,
                "live_coach": self.live_coach_model,
                "video": self.pov_video_model,
                "image": self.pov_image_model,
                "audio": self.audio_model,
                "veo": self.veo_model,
                "imagen": self.imagen_model,
            },
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
