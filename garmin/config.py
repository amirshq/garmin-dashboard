"""Project configuration, loaded from environment / .env."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

BASE_URL = os.getenv("GARMIN_BASE_URL", "https://personal.connectapi.garmin.com")
DB_PATH = Path(os.getenv("GARMIN_DB_PATH", PROJECT_ROOT / "data" / "garmin.db"))
FIT_DIR = Path(os.getenv("GARMIN_FIT_DIR", PROJECT_ROOT / "data" / "fit"))
TOKEN_CACHE = Path(os.getenv("GARMIN_TOKEN_CACHE", PROJECT_ROOT / "data" / ".token.json"))
LOG_DIR = PROJECT_ROOT / "logs"


def api_key() -> str:
    key = os.getenv("GARMIN_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "GARMIN_API_KEY is not set. Put it in .env (see .env.example)."
        )
    return key
