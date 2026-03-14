"""
Core configuration via pydantic-settings.
All secrets loaded from .env file.
"""
from typing import Literal
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # --- API Keys ---
    gemini_api_key: str

    # --- Model Selection ---
    model_dialogue: str = "gemini-2.5-flash"
    model_vision: str = "gemini-2.5-flash"
    model_mx: str = "gemini-2.5-pro"
    model_longitudinal: str = "gemini-2.5-flash"

    # --- Database ---
    db_url: str = "sqlite+aiosqlite:///data/patients.db"

    # --- Cockpit ---
    cockpit_mode: Literal["cli", "web"] = "cli"
    web_port: int = 8080

    # --- Orchestrator Guardrails ---
    completeness_threshold: float = 0.80
    max_agent_timeout_seconds: int = 45
    max_concurrent_gemini_calls: int = 5
    max_agent_retries: int = 3

    # --- Clinical Red Flag Thresholds ---
    red_flag_hb_gdl: float = 7.0          # Hemoglobin g/dL
    red_flag_waz_zscore: float = -3.0     # Weight-for-age z-score
    red_flag_haz_zscore: float = -3.0     # Height-for-age z-score
    red_flag_hb_drop_gdl: float = 2.0     # Drop in Hb g/dL within 4 weeks
    red_flag_ferritin_ugL: float = 10.0   # Ferritin µg/L

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")
