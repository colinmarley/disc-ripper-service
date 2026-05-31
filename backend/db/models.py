from datetime import datetime
from sqlalchemy import String, Integer, Float, DateTime, Text, JSON
from sqlalchemy.orm import Mapped, mapped_column
from db.database import Base


class RipJob(Base):
    __tablename__ = "rip_jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # User-supplied metadata
    disc_type: Mapped[str] = mapped_column(String)        # "dvd" | "bluray"
    media_type: Mapped[str] = mapped_column(String)       # "movie" | "show"
    title: Mapped[str] = mapped_column(String)
    year: Mapped[int] = mapped_column(Integer)
    imdb_id: Mapped[str] = mapped_column(String, nullable=True)
    season: Mapped[int] = mapped_column(Integer, nullable=True)
    mkv_title_indices: Mapped[list] = mapped_column(JSON, default=list)
    episode_map: Mapped[dict] = mapped_column(JSON, nullable=True)  # {str(title_idx): "S01E01"}

    # Job state
    status: Mapped[str] = mapped_column(String, default="queued")
    # queued | ripping | encoding | delivering | done | failed | cancelled
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str] = mapped_column(Text, nullable=True)

    # Intermediate paths
    rip_dir: Mapped[str] = mapped_column(String, nullable=True)
    output_paths: Mapped[list] = mapped_column(JSON, default=list)  # final delivered paths

    # Log (tail of subprocess output)
    log: Mapped[str] = mapped_column(Text, default="")
