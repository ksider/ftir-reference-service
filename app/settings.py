from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    source_dir: Path
    cors_allow_origins: tuple[str, ...]
    cors_allow_origin_regex: str | None
    admin_token: str
    service_token: str
    zenodo_record_id: str
    vector_points: int
    pubchem_timeout_seconds: float

    @property
    def index_dir(self) -> Path:
        return self.data_dir / "index"


def load_settings() -> Settings:
    # Docker Compose supplies environment variables directly; local uvicorn
    # development reads the same values from a repository-local .env file.
    load_dotenv(override=False)
    vector_points = int(os.getenv("REFERENCE_VECTOR_POINTS", "512"))
    if vector_points < 64 or vector_points > 4096:
        raise ValueError("REFERENCE_VECTOR_POINTS must be between 64 and 4096")
    pubchem_timeout_seconds = float(os.getenv("PUBCHEM_TIMEOUT_SECONDS", "12"))
    if pubchem_timeout_seconds <= 0 or pubchem_timeout_seconds > 60:
        raise ValueError("PUBCHEM_TIMEOUT_SECONDS must be between 0 and 60")
    data_dir = Path(os.getenv("REFERENCE_DATA_DIR", "/data")).resolve()
    configured_source = os.getenv("REFERENCE_SOURCE_DIR", "").strip()
    cors_allow_origins = tuple(
        origin.strip() for origin in os.getenv("CORS_ALLOW_ORIGINS", "").split(",") if origin.strip()
    )
    cors_allow_origin_regex = os.getenv("CORS_ALLOW_ORIGIN_REGEX", "").strip() or None
    return Settings(
        data_dir=data_dir,
        source_dir=Path(configured_source).expanduser().resolve() if configured_source else data_dir / "source",
        cors_allow_origins=cors_allow_origins,
        cors_allow_origin_regex=cors_allow_origin_regex,
        admin_token=os.getenv("ADMIN_TOKEN", "").strip(),
        service_token=os.getenv("SERVICE_TOKEN", "").strip(),
        zenodo_record_id=os.getenv("ZENODO_RECORD_ID", "16417648").strip(),
        vector_points=vector_points,
        pubchem_timeout_seconds=pubchem_timeout_seconds,
    )
