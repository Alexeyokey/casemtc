from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    db_path: str = field(default_factory=lambda: os.getenv("CASEMTC_DB_PATH", "data/casemtc.sqlite3"))
    leads_dir: str = field(default_factory=lambda: os.getenv("CASEMTC_LEADS_DIR", "data/leads"))
    engine_spec: str | None = field(default_factory=lambda: os.getenv("CONSULTATION_ENGINE"))
    debug_api: bool = field(default_factory=lambda: _bool("CASEMTC_DEBUG_API", True))
    archive_token: str | None = field(default_factory=lambda: os.getenv("ARCHIVE_TOKEN") or None)
    cors_origins: list[str] = field(default_factory=lambda: [
        o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:5173,http://localhost:3000").split(",")
        if o.strip()])
