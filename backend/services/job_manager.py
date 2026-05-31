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
from services import makemkv_service, encode_service


def _safe_name(s: str) -> str:
    return re.sub(r'[^\w\s\-\.]', '', s).strip().replace(' ', '_')


def _ingest_folder_name(job: RipJob) -> str:
    base = f"{job.title} ({job.year})"
    if job.imdb_id:
        base += f" [imdbid-{job.imdb_id}]"
    return base


def _episode_filename(ep_code: str, title: str) -> str:
    """e.g. 'S01E03' + series title → 'S01E03 - Show Title.mkv'"""
    return f"{ep_code} - {_safe_name(title)}.mkv"


class JobManager:
    def __init__(self):
        self._new_job_event = asyncio.Event()
        self._active_proc: Optional[asyncio.subprocess.Process] = None
        self._active_job_id: Optional[str] = None
        self._log_subscribers: dict[str, list[asyncio.Queue]] = {}

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

        async def log(line: str):
            self._append_log(job_id, line)
            for q in list(self._log_subscribers.get(job_id, [])):
                await q.put(line)

        try:
            await self._run_rip(job_id, log)
            await self._run_encode(job_id, log)
            await self._run_deliver(job_id, log)
        except asyncio.CancelledError:
            self._set_status(job_id, "cancelled")
        except Exception as exc:
            self._set_status(job_id, "failed", error=str(exc))
            await log(f"[ERROR] {exc}")
        finally:
            self._active_job_id = None
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

        for idx in job.mkv_title_indices:
            await log(f"[rip] Starting title index {idx}")
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
            async for raw in proc.stdout:
                await log(raw.decode(errors="replace").rstrip())
            await proc.wait()
            self._active_proc = None
            if proc.returncode != 0:
                raise RuntimeError(f"makemkvcon exited {proc.returncode} for title {idx}")
            await log(f"[rip] Title {idx} done")

    async def _run_encode(self, job_id: str, log):
        job = self._set_status(job_id, "encoding")
        rip_dir = job.rip_dir
        encoded_paths = []

        mkv_files = sorted(Path(rip_dir).glob("*.mkv"))
        if not mkv_files:
            raise RuntimeError(f"No .mkv files found in {rip_dir} after ripping")

        await log(f"[encode] Found {len(mkv_files)} MKV file(s)")

        for mkv in mkv_files:
            out_name = mkv.stem + "_processed.mkv"
            out_path = str(mkv.parent / out_name)

            proc_handle: list = []

            async def log_and_track(line: str, _out=out_path):
                await log(line)

            await encode_service.process_file(
                input_path=str(mkv),
                output_path=out_path,
                disc_type=job.disc_type,
                log_callback=log,
            )
            encoded_paths.append(out_path)
            await log(f"[encode] Done: {out_path}")

        self._update_field(job_id, output_paths=encoded_paths)

    async def _run_deliver(self, job_id: str, log):
        job = self._set_status(job_id, "delivering")
        folder_name = _ingest_folder_name(job)
        dest_dir = Path(settings.ingest_root) / folder_name

        if job.media_type == "show" and job.season is not None:
            dest_dir = dest_dir / f"Season {job.season:02d}"

        dest_dir.mkdir(parents=True, exist_ok=True)
        await log(f"[deliver] Destination: {dest_dir}")

        delivered = []
        encoded_paths = job.output_paths or []
        episode_map = job.episode_map or {}

        for i, src in enumerate(encoded_paths):
            src_path = Path(src)
            if job.media_type == "movie":
                dest_name = f"{job.title} ({job.year}).mkv"
                if len(encoded_paths) > 1:
                    dest_name = f"{job.title} ({job.year}) - Version {i + 1}.mkv"
            else:
                # Map by original title index if we have an episode map
                title_idx = str(job.mkv_title_indices[i]) if i < len(job.mkv_title_indices) else str(i)
                ep_code = episode_map.get(title_idx, f"S{job.season or 1:02d}E{i + 1:02d}")
                dest_name = _episode_filename(ep_code, job.title)

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
            return job

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
            if len(lines) > 200:
                lines = lines[-200:]
            job.log = "\n".join(lines)
            job.updated_at = datetime.utcnow()
            db.commit()


# Singleton
job_manager = JobManager()
