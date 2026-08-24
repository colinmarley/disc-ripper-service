from typing import Optional
from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8083

    # Paths
    rips_root: str = Field(default="/data/media/rips", validation_alias="RIPS_ROOT")
    ingest_root: str = Field(default="/ark/media/jellyfin/ingest", validation_alias="INGEST_ROOT")
    db_path: str = Field(default="/data/ripper/ripper.db", validation_alias="DB_PATH")

    # Tool paths
    makemkvcon_path: str = Field(default="makemkvcon", validation_alias="MAKEMKVCON_PATH")
    handbrake_path: str = Field(default="HandBrakeCLI", validation_alias="HANDBRAKE_PATH")
    ffmpeg_path: str = Field(default="ffmpeg", validation_alias="FFMPEG_PATH")
    ffprobe_path: str = Field(default="ffprobe", validation_alias="FFPROBE_PATH")

    # Disc device
    disc_device: str = Field(default="disc:0", validation_alias="DISC_DEVICE")

    # Log file storage
    logs_root: str = Field(default="/data/ripper/logs", validation_alias="LOGS_ROOT")

    # Ollama AI analysis
    ollama_host: str = Field(default="http://192.168.0.227:11434", validation_alias="OLLAMA_HOST")
    ollama_analysis_model: str = Field(default="qwen2.5:32b", validation_alias="OLLAMA_ANALYSIS_MODEL")

    # my-media-manager's Postgres-backed catalog API — canonical source of truth
    # for physical disc records (see backend/services/catalog_client.py).
    media_manager_api_url: str = Field(
        default="http://192.168.0.227:8082", validation_alias="MEDIA_MANAGER_API_URL"
    )

    # TMDB — used for movie/show search during rip configuration (services/tmdb_service.py).
    # Kept server-side (unlike the frontend's NEXT_PUBLIC_TMDB_API_KEY, which is
    # client-exposed) so the key never reaches the browser.
    tmdb_api_key: str = Field(default="", validation_alias="TMDB_API_KEY")
    tmdb_base_url: str = Field(default="https://api.themoviedb.org/3", validation_alias="TMDB_BASE_URL")

    class Config:
        env_file = ".env"


settings = Settings()
