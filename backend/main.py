import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from api.disc_operations import router as disc_router
from api.job_operations import router as job_router
from api.tmdb_operations import router as tmdb_router
from config.settings import settings
from db.database import Base, engine
from services.job_manager import job_manager
from homelab_logging import setup_logging, get_logger, CorrelationMiddleware
from homelab_logging.config import LoggingConfig

setup_logging(LoggingConfig(project="disc-ripper-service", service="backend"))
logger = get_logger(__name__)


def _migrate_db():
    """Add new columns to existing DB tables without alembic."""
    from sqlalchemy import text
    new_columns = [
        ("encode_quality", "INTEGER"),
        ("encode_encoder", "VARCHAR"),
        ("log_path", "TEXT"),
        ("catalog_disc_id", "TEXT"),
        ("title_content_types", "TEXT"),
    ]
    with engine.connect() as conn:
        existing = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(rip_jobs)")).fetchall()
        }
        for col_name, col_type in new_columns:
            if col_name not in existing:
                conn.execute(text(f"ALTER TABLE rip_jobs ADD COLUMN {col_name} {col_type}"))
                conn.commit()
                logger.info("migrate_add_column", table="rip_jobs", column=col_name)

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS job_analysis (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL REFERENCES rip_jobs(id),
                created_at DATETIME,
                error_type TEXT,
                error_summary TEXT,
                suggested_fix TEXT,
                claude_prompt TEXT,
                full_analysis TEXT,
                model_used TEXT,
                log_path TEXT
            )
        """))
        conn.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    _migrate_db()
    recovered = job_manager.recover_stale_jobs()
    if recovered:
        logger.warning("stale_jobs_recovered", count=recovered)
    worker = asyncio.create_task(job_manager.run_worker())
    yield
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Disc Ripper Service", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Binds correlation_id/session_id to contextvars for the request lifetime so every
# log line emitted while handling this request carries the same searchable ID.
app.add_middleware(CorrelationMiddleware)

app.include_router(disc_router)
app.include_router(job_router)
app.include_router(tmdb_router)


@app.get("/", response_class=HTMLResponse)
async def ui():
    return (Path(__file__).parent / "static" / "index.html").read_text()


@app.get("/health")
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=False)
