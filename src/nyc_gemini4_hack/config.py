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
    gemini_decisions_api_version: str = Field(default="v1alpha")

    # Media Generation (Veo / Imagen / TTS)
    veo_model: str = Field(default="veo-3.0-generate-preview")
    imagen_model: str = Field(default="imagen-3.0-generate-002")
    tts_voice: str = Field(default="Aoede")

    # YouTube Data API v3
    youtube_api_key: str = Field(default="")

    # Shopify Commerce (shop.dev / Storefront MCP)
    shopify_mcp_url: str = Field(default="")
    shopify_storefront_token: str = Field(default="")
    shopify_shop_domain: str = Field(default="")

    # Server & Storage
    app_host: str = Field(default="0.0.0.0")
    app_port: int = Field(default=8000)
    media_output_dir: Path = Field(default=Path("./outputs"))

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
            "decisions_api_enabled": self.gemini_decisions_enabled,
            "models": {
                "argon": self.gemini_model_argon,
                "carbon": self.gemini_model_carbon,
                "neon": self.gemini_model_neon,
                "hydrogen": self.gemini_model_hydrogen,
                "decisions": self.gemini_decisions_model,
                "veo": self.veo_model,
                "imagen": self.imagen_model,
            },
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
