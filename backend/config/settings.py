from typing import Optional
from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8083

    # Paths
    rips_root: str = Field(default="/data/media/rips", validation_alias="RIPS_ROOT")
    ingest_root: str = Field(default="/data/ingest", validation_alias="INGEST_ROOT")
    db_path: str = Field(default="/data/ripper/ripper.db", validation_alias="DB_PATH")

    # Tool paths
    makemkvcon_path: str = Field(default="makemkvcon", validation_alias="MAKEMKVCON_PATH")
    handbrake_path: str = Field(default="HandBrakeCLI", validation_alias="HANDBRAKE_PATH")
    ffmpeg_path: str = Field(default="ffmpeg", validation_alias="FFMPEG_PATH")
    ffprobe_path: str = Field(default="ffprobe", validation_alias="FFPROBE_PATH")

    # Disc device
    disc_device: str = Field(default="disc:0", validation_alias="DISC_DEVICE")

    # Seconds to wait for `makemkvcon info` (Blu-ray scans can take minutes)
    scan_timeout_seconds: int = Field(default=300, validation_alias="SCAN_TIMEOUT_SECONDS")

    # Compute a sha256 of every ripped file before delivery (adds roughly a
    # minute per 30 GB title on NVMe).
    hash_ripped_files: bool = Field(default=True, validation_alias="HASH_RIPPED_FILES")

    # Fail a job whose scan was explicitly chosen if the drive turns out to
    # hold a different disc. Turn off if it ever rejects the right disc.
    verify_disc_matches_scan: bool = Field(default=True, validation_alias="VERIFY_DISC_MATCHES_SCAN")

    # A rip must name the catalogued disc it is ripping (catalog_disc_id).
    require_catalog_disc: bool = Field(default=True, validation_alias="REQUIRE_CATALOG_DISC")

    # How often rip records that have not reached my-media-manager are retried.
    library_sync_interval_seconds: int = Field(default=60, validation_alias="LIBRARY_SYNC_INTERVAL_SECONDS")

    # Recorded in the rip manifest so the library knows which machine ripped.
    host_id: str = Field(default="disc-ripper", validation_alias="HOST_ID")

    # Log file storage
    logs_root: str = Field(default="/data/ripper/logs", validation_alias="LOGS_ROOT")

    # LocalAI AI analysis
    localai_url: str = Field(default="http://localhost:8080", validation_alias="LOCALAI_URL")
    localai_api_key: str = Field(default="", validation_alias="LOCALAI_API_KEY")
    localai_analysis_model: str = Field(default="qwen2.5-14b", validation_alias="LOCALAI_ANALYSIS_MODEL")

    # my-media-manager's Postgres-backed catalog API — canonical source of truth
    # for physical disc records (see backend/services/catalog_client.py).
    media_manager_api_url: str = Field(
        default="http://localhost:8082", validation_alias="MEDIA_MANAGER_API_URL"
    )

    # TMDB — used for movie/show search during rip configuration (services/tmdb_service.py).
    # Kept server-side (unlike the frontend's NEXT_PUBLIC_TMDB_API_KEY, which is
    # client-exposed) so the key never reaches the browser.
    tmdb_api_key: str = Field(default="", validation_alias="TMDB_API_KEY")
    tmdb_base_url: str = Field(default="https://api.themoviedb.org/3", validation_alias="TMDB_BASE_URL")

    class Config:
        env_file = ".env"


settings = Settings()
