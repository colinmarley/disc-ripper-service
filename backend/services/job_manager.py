"""
Job lifecycle manager.

Each job goes through: queued → ripping → encoding → delivering → done | failed | cancelled

Jobs run sequentially in a background asyncio task. A single asyncio.Event
is used to signal when a new job is enqueued so the worker loop wakes up
without polling.
"""

import asyncio
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from config.settings import settings
from db.database import SessionLocal
from db.models import RipJob
from services import makemkv_service
from homelab_logging import setup_logging, get_logger
from homelab_logging.config import LoggingConfig

# Idempotent: this module may be imported directly (e.g. by tests) before
# main.py has had a chance to call setup_logging() itself.
setup_logging(LoggingConfig(project="disc-ripper-service", service="backend"))
logger = get_logger(__name__)


def _safe_name(s: str) -> str:
    return re.sub(r'[^\w\s\-\.]', '', s).strip().replace(' ', '_')


def _safe_title(s: str) -> str:
    """Sanitize a title for use in a filename, keeping spaces (for readability)."""
    return re.sub(r'[/\\:*?"<>|\x00-\x1f]', '', s).strip()


def _ingest_folder_name(job: RipJob) -> str:
    base = f"{job.title} ({job.year})"
    if job.imdb_id:
        base += f" [imdbid-{job.imdb_id}]"
    return base


def _episode_filename(ep_value: str, show_title: str) -> str:
    """e.g. 'S01E03 - Pilot' + 'Burn Notice' → 'Burn Notice S01E03 - Pilot.mkv'
    ep_value may be just a code ('S01E03') or code + name ('S01E03 - Pilot').
    """
    return f"{_safe_title(show_title)} {ep_value}.mkv"


def _build_dest_name(
    media_type: str,
    title: str,
    year: int,
    i: int,
    total: int,
    title_idx: str,
    episode_map: dict,
    season: int,
) -> str:
    """Return the destination filename for the i-th output file (0-indexed)."""
    if media_type == "movie":
        dest_name = f"{title} ({year}).mkv"
        if total > 1:
            dest_name = f"{title} ({year}) - Version {i + 1}.mkv"
    else:
        ep_code = episode_map.get(title_idx, f"S{season or 1:02d}E{i + 1:02d}")
        dest_name = _episode_filename(ep_code, title)
    return dest_name


def _extract_msg_text(line: str) -> str:
    """Return the human-readable string from a MSG:/PRGC: line (first quoted field)."""
    m = re.search(r'"([^"]*)"', line)
    return m.group(1) if m else ""


class JobManager:
    def __init__(self):
        self._new_job_event = asyncio.Event()
        self._active_proc: Optional[asyncio.subprocess.Process] = None
        self._active_job_id: Optional[str] = None
        self._log_subscribers: dict[str, list[asyncio.Queue]] = {}
        self._last_written_progress: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create_job(self, data: dict) -> RipJob:
        job = RipJob(
            id=str(uuid4()),
            disc_type=data["disc_type"],
            media_type=data["media_type"],
            title=data["title"],
            year=data["year"],
            imdb_id=data.get("imdb_id"),
            season=data.get("season"),
            mkv_title_indices=data.get("mkv_title_indices", [0]),
            episode_map=data.get("episode_map"),
            encode_quality=data.get("dvd_quality"),
            encode_encoder=data.get("dvd_encoder"),
            catalog_disc_id=data.get("catalog_disc_id"),
        )
        with SessionLocal() as db:
            db.add(job)
            db.commit()
            db.refresh(job)
        self._new_job_event.set()
        return job

    def get_job(self, job_id: str) -> Optional[RipJob]:
        with SessionLocal() as db:
            return db.get(RipJob, job_id)

    def list_jobs(self, status: Optional[str] = None) -> list[RipJob]:
        with SessionLocal() as db:
            q = db.query(RipJob).order_by(RipJob.created_at.desc())
            if status:
                q = q.filter(RipJob.status == status)
            return q.all()

    def recover_stale_jobs(self) -> int:
        """Fail any jobs left in active states from a previous run."""
        with SessionLocal() as db:
            stale = (
                db.query(RipJob)
                .filter(RipJob.status.in_(("ripping", "encoding", "delivering")))
                .all()
            )
            for job in stale:
                job.status = "failed"
                job.error = "Service restarted while job was in progress"
                job.updated_at = datetime.utcnow()
            db.commit()
            return len(stale)

    def cancel_job(self, job_id: str) -> bool:
        if self._active_job_id == job_id and self._active_proc:
            try:
                self._active_proc.terminate()
            except Exception:
                pass
        with SessionLocal() as db:
            job = db.get(RipJob, job_id)
            if job and job.status in ("queued", "ripping", "encoding", "delivering"):
                job.status = "cancelled"
                db.commit()
                return True
        return False

    def subscribe_log(self, job_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._log_subscribers.setdefault(job_id, []).append(q)
        return q

    def unsubscribe_log(self, job_id: str, q: asyncio.Queue) -> None:
        subs = self._log_subscribers.get(job_id, [])
        if q in subs:
            subs.remove(q)

    # ------------------------------------------------------------------
    # Worker loop (runs as a background asyncio task)
    # ------------------------------------------------------------------

    async def run_worker(self):
        while True:
            await self._new_job_event.wait()
            self._new_job_event.clear()
            while True:
                job = self._next_queued()
                if not job:
                    break
                await self._process_job(job.id)

    def _next_queued(self) -> Optional[RipJob]:
        with SessionLocal() as db:
            return (
                db.query(RipJob)
                .filter(RipJob.status == "queued")
                .order_by(RipJob.created_at)
                .first()
            )

    # ------------------------------------------------------------------
    # Job processing
    # ------------------------------------------------------------------

    async def _process_job(self, job_id: str):
        self._active_job_id = job_id

        # Open full log file for this job
        log_dir = Path(settings.logs_root)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file_path = str(log_dir / f"{job_id}.log")
        log_fh = open(log_file_path, "a", encoding="utf-8")
        self._update_field(job_id, log_path=log_file_path)

        async def log(line: str):
            self._append_log(job_id, line)
            try:
                log_fh.write(line + "\n")
                log_fh.flush()
            except Exception:
                pass
            for q in list(self._log_subscribers.get(job_id, [])):
                await q.put(line)

        try:
            await self._run_rip(job_id, log)
            await self._run_deliver(job_id, log)
        except asyncio.CancelledError:
            self._set_status(job_id, "cancelled")
        except Exception as exc:
            self._set_status(job_id, "failed", error=str(exc))
            await log(f"[ERROR] {exc}")
        finally:
            self._active_job_id = None
            try:
                log_fh.close()
            except Exception:
                pass
            # Signal EOF to all log subscribers
            for q in list(self._log_subscribers.get(job_id, [])):
                await q.put(None)

    async def _run_rip(self, job_id: str, log):
        job = self._set_status(job_id, "ripping")
        rip_dir = os.path.join(
            settings.rips_root,
            job.media_type + "s",
            _safe_name(job.title),
            job_id[:8],
        )
        Path(rip_dir).mkdir(parents=True, exist_ok=True)
        self._update_field(job_id, rip_dir=rip_dir)

        await log(f"[rip] Output dir: {rip_dir}")

        total = len(job.mkv_title_indices)
        # Rip phase occupies 0-60% of overall job progress.
        for title_num, idx in enumerate(job.mkv_title_indices, 1):
            await log(f"[rip] ── Title {title_num}/{total} (disc index {idx}) ──")
            # Snapshot before ripping so we can identify the new file afterwards
            # and rename it to a canonical sequential name. This guarantees that
            # sorted(glob("*.mkv")) in the encode phase matches the rip order,
            # which is required for episode_map lookups to align correctly.
            before_rip = set(Path(rip_dir).glob("*.mkv"))
            proc = await asyncio.create_subprocess_exec(
                settings.makemkvcon_path,
                "--noscan", "-r",
                "mkv", settings.disc_device,
                str(idx),
                rip_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            self._active_proc = proc

            last_prgc = ""
            warn_files: dict[str, int] = {}  # file → count of MSG:4004 hits

            async for raw in proc.stdout:
                line = raw.decode(errors="replace").rstrip()
                if not line:
                    continue

                # ── Skip pure noise ──────────────────────────────────────
                if line.startswith((
                    "DRV:",       # drive enumeration (16 blank entries)
                    "MSG:1005,",  # "MakeMKV vX.Y.Z started"
                    "MSG:3007,",  # "Using direct disc access mode"
                    "MSG:3025,",  # "Title too short, skipped"
                    "MSG:3028,",  # "Title #N was added"
                    "PRGT:",      # total-progress label (redundant)
                )):
                    continue

                # ── Progress ─────────────────────────────────────────────
                if line.startswith("PRGV:"):
                    parts = line[5:].split(",")
                    if len(parts) == 3:
                        try:
                            current, _, max_val = int(parts[0]), parts[1], int(parts[2])
                            if max_val > 0:
                                title_frac = current / max_val
                                overall = ((title_num - 1) + title_frac) / total * 60
                                self._set_progress(job_id, overall)
                        except ValueError:
                            pass
                    continue

                # ── Current-operation label (log when it changes) ────────
                if line.startswith("PRGC:"):
                    msg = _extract_msg_text(line)
                    if msg and msg != last_prgc:
                        last_prgc = msg
                        await log(f"[rip] {msg}")
                    continue

                # ── Deduplicate MSG:4004 corruption warnings ─────────────
                if line.startswith("MSG:4004,"):
                    m = re.search(r"'([^']+)'", line)
                    fname = m.group(1) if m else "unknown"
                    warn_files[fname] = warn_files.get(fname, 0) + 1
                    if warn_files[fname] == 1:
                        await log(f"[warn] Corrupt sector in {fname} — attempting workaround")
                    elif warn_files[fname] == 5:
                        await log(f"[warn] Multiple corrupt sectors in {fname} (suppressing further warnings)")
                    continue

                # ── Human-readable summary for completion messages ────────
                if line.startswith(("MSG:5011,", "MSG:5014,", "MSG:5005,", "MSG:5036,")):
                    text = _extract_msg_text(line)
                    if text:
                        await log(f"[rip] {text}")
                    continue

                # ── Everything else passes through ───────────────────────
                await log(line)

            await proc.wait()
            self._active_proc = None
            if proc.returncode != 0:
                raise RuntimeError(f"makemkvcon exited {proc.returncode} for title {idx}")

            # Rename the newly produced file to rip_NNNN.mkv so encode/deliver
            # can rely on alphabetical sort order matching the rip order.
            after_rip = set(Path(rip_dir).glob("*.mkv"))
            new_files = after_rip - before_rip
            if len(new_files) == 1:
                produced = new_files.pop()
                canonical = Path(rip_dir) / f"rip_{title_num:04d}.mkv"
                produced.rename(canonical)
                await log(f"[rip] {produced.name} → {canonical.name}")
            else:
                await log(f"[rip] Warning: expected 1 new file for title {idx}, found {len(new_files)} — episode order may be incorrect")

            await log(f"[rip] Title {title_num}/{total} done")

    async def _run_deliver(self, job_id: str, log):
        job = self._set_status(job_id, "delivering")
        folder_name = _ingest_folder_name(job)
        dest_dir = Path(settings.ingest_root) / folder_name

        if job.media_type == "show" and job.season is not None:
            dest_dir = dest_dir / f"Season {job.season:02d}"

        dest_dir.mkdir(parents=True, exist_ok=True)
        await log(f"[deliver] Destination: {dest_dir}")

        delivered = []
        encoded_paths = job.output_paths or sorted(str(p) for p in Path(job.rip_dir).glob("*.mkv"))
        episode_map = job.episode_map or {}

        for i, src in enumerate(encoded_paths):
            src_path = Path(src)
            title_idx = str(job.mkv_title_indices[i]) if i < len(job.mkv_title_indices) else str(i)
            dest_name = _build_dest_name(
                job.media_type, job.title, job.year,
                i, len(encoded_paths), title_idx, episode_map, job.season or 1,
            )

            dest_file = dest_dir / dest_name
            await log(f"[deliver] Moving {src_path.name} → {dest_file.name}")
            shutil.move(str(src_path), str(dest_file))
            delivered.append(str(dest_file))

        # Clean up rip dir
        try:
            shutil.rmtree(job.rip_dir)
            await log(f"[deliver] Cleaned rip dir: {job.rip_dir}")
        except Exception as e:
            await log(f"[deliver] Warning: could not clean rip dir: {e}")

        self._update_field(job_id, output_paths=delivered)
        self._set_status(job_id, "done")
        await log(f"[deliver] Job complete. {len(delivered)} file(s) delivered.")

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    def _set_status(self, job_id: str, status: str, error: Optional[str] = None) -> RipJob:
        with SessionLocal() as db:
            job = db.get(RipJob, job_id)
            job.status = status
            job.updated_at = datetime.utcnow()
            if error is not None:
                job.error = error
            db.commit()
            db.refresh(job)
            if status in ("failed", "cancelled"):
                logger.warning(
                    "job_status_changed",
                    job_id=job_id, status=status, title=job.title, error=error,
                )
            else:
                logger.info(
                    "job_status_changed",
                    job_id=job_id, status=status, title=job.title,
                )
            return job

    def _set_progress(self, job_id: str, value: float) -> None:
        """Write progress to DB only when it moves by ≥2 points (avoids per-PRGV-line writes)."""
        last = self._last_written_progress.get(job_id, -99)
        if abs(value - last) < 2.0:
            return
        self._last_written_progress[job_id] = value
        with SessionLocal() as db:
            job = db.get(RipJob, job_id)
            if job:
                job.progress = round(value, 1)
                job.updated_at = datetime.utcnow()
                db.commit()

    def _update_field(self, job_id: str, **kwargs):
        with SessionLocal() as db:
            job = db.get(RipJob, job_id)
            for k, v in kwargs.items():
                setattr(job, k, v)
            job.updated_at = datetime.utcnow()
            db.commit()

    def _append_log(self, job_id: str, line: str):
        with SessionLocal() as db:
            job = db.get(RipJob, job_id)
            existing = job.log or ""
            # Keep last 200 lines to avoid unbounded DB growth
            lines = existing.splitlines()
            lines.append(line)
            if len(lines) > 500:
                lines = lines[-500:]
            job.log = "\n".join(lines)
            job.updated_at = datetime.utcnow()
            db.commit()


# Singleton
job_manager = JobManager()
