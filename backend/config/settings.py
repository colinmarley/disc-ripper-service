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

    # DVD encode settings
    dvd_encoder: str = "nvenc_h265"
    dvd_quality: int = 21

    # Blu-ray width threshold — anything wider than this is treated as HD and remuxed
    bluray_width_threshold: int = 1280

    class Config:
        env_file = ".env"


settings = Settings()
