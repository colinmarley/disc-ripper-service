from datetime import datetime
from typing import Optional
from sqlalchemy import String, Integer, BigInteger, Float, DateTime, Text, JSON, ForeignKey
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
    # {str(title_idx): "trailer"} — extras taxonomy category slug per title;
    # absent/None title_idx means "main feature" (see _build_dest_name).
    title_content_types: Mapped[dict] = mapped_column(JSON, nullable=True)

    # Links this rip to a physical disc record in my-media-manager's Postgres
    # catalog (the canonical disc entity — see services/catalog_client.py).
    # Nullable: a rip can happen without a pre-logged or linked disc.
    catalog_disc_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

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
    log_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ── Rip-time metadata (kept so the library can categorise without guessing) ──
    tmdb_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # The DiscScan this job was configured from. scan_link says how we know:
    # "declared" (the UI sent scan_id) or "inferred" (most recent scan).
    scan_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    scan_link: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    fingerprint: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Structured per-title declarations: [{title_index, content_type,
    # is_main_feature, season, episode, episode_end, tmdb_episode_id,
    # episode_name, custom_name}]
    title_declarations: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    rip_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    rip_finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Durable copy of the delivered manifest (on the ripper's own volume).
    manifest_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Push of the rip record to my-media-manager: None | pending | synced | failed
    library_sync_state: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    library_sync_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class DiscScan(Base):
    """One `makemkvcon info` run: the raw output plus everything parsed from it."""
    __tablename__ = "disc_scans"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    raw_output: Mapped[str] = mapped_column(Text, default="")
    parsed: Mapped[dict] = mapped_column(JSON, default=dict)
    fingerprint: Mapped[Optional[str]] = mapped_column(String, nullable=True, index=True)
    fingerprint_algo: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    disc_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    disc_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    volume_label: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    drive_model: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    drive_label: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    device: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    makemkv_version: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    title_count: Mapped[int] = mapped_column(Integer, default=0)


class RipTitle(Base):
    """One output file of a rip job (or one title that produced none).

    Files are tied to the disc title they came from here, never by position
    in a directory listing.
    """
    __tablename__ = "rip_titles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String, ForeignKey("rip_jobs.id"), nullable=False, index=True)
    title_index: Mapped[int] = mapped_column(Integer)
    file_seq: Mapped[int] = mapped_column(Integer, default=0)
    # ripped | failed | delivered
    status: Mapped[str] = mapped_column(String, default="ripped")
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    makemkv_filename: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    rip_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    dest_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    rip_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    rip_finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    read_errors: Mapped[int] = mapped_column(Integer, default=0)
    messages: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    probe: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)


class JobAnalysis(Base):
    __tablename__ = "job_analysis"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(String, ForeignKey("rip_jobs.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    error_type: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error_summary: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    suggested_fix: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    claude_prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    full_analysis: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    model_used: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    log_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
