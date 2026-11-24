"""
Job lifecycle manager.

Each job goes through: queued → ripping → delivering → done | failed | cancelled

Jobs run sequentially in a background asyncio task. A single asyncio.Event
is used to signal when a new job is enqueued so the worker loop wakes up
without polling.
"""

import asyncio
import os
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from config.settings import settings
from db.database import SessionLocal
from db.models import DiscScan, RipJob, RipTitle
from services import catalog_client, letterbox_service, makemkv_service, probe_service
from services import manifest as manifest_service
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


# Extras category slug -> filename suffix token. Must match a token
# recognized by my-media-manager's services.extras_taxonomy.EXTRA_SUFFIX_TO_FOLDER
# regex (which matches these tokens at the END of a filename stem) — this
# suffix is the only channel available to communicate a title's content type
# across the filesystem handoff, since delivery has no HTTP push to
# my-media-manager, just a shared bind-mounted ingest folder.
CONTENT_TYPE_SUFFIX: dict[str, str] = {
    "behind_the_scenes": "behindthescenes",
    "deleted_scene": "deletedscene",
    "interview": "interview",
    "featurette": "featurette",
    "trailer": "trailer",
    "scene": "scene",
    "sample": "sample",
    "short": "short",
    "clip": "clip",
    "blooper": "blooper",
    "other": "other",
}


def _build_dest_name(
    media_type: str,
    title: str,
    year: int,
    i: int,
    total: int,
    title_idx: str,
    episode_map: dict,
    season: int,
    content_type: Optional[str] = None,
    content_type_counts: Optional[dict[str, int]] = None,
) -> str:
    """Return the destination filename for the i-th output file (0-indexed).

    `content_type`, when set to a known extras category (main-feature titles
    should pass None), routes the file into a taxonomy-suffixed filename
    instead of the default movie/episode naming — e.g. a trailer becomes
    "Title (Year)-trailer.mkv" rather than "Title (Year) - Version 2.mkv".
    `content_type_counts` (mutated in place) disambiguates multiple files of
    the same content type on one disc.
    """
    suffix = CONTENT_TYPE_SUFFIX.get(content_type) if content_type else None
    if suffix:
        counts = content_type_counts if content_type_counts is not None else {}
        counts[content_type] = counts.get(content_type, 0) + 1
        n = counts[content_type]
        number_part = f" {n}" if n > 1 else ""
        return f"{title} ({year}){number_part}-{suffix}.mkv"

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


# A job started without a scan_id (older UI) is attached to the most recent
# scan if that scan is newer than this.
INFERRED_SCAN_MAX_AGE = timedelta(hours=6)

# How far the ripped file's duration may differ from the scanned title's
# before it is called out in the manifest.
DURATION_TOLERANCE = 0.02

_MAX_TITLE_MESSAGES = 50


def _names_in(folder: Path) -> set[str]:
    """Lower-cased names of what is in ``folder`` now, from its listing.

    The listing, not ``Path.exists``: on the CIFS share the ingest folder lives
    on, a file another machine moved away keeps answering "exists" from the
    client's cache for hours, while the listing no longer has it. Every extra
    of a later disc was then delivered as "... - Copy 2-clip.mkv".
    """
    try:
        return {entry.lower() for entry in os.listdir(folder)}
    except OSError:
        return set()


def _deliver_file(src: Path, dest: Path) -> None:
    """Move ``src`` to ``dest`` under a fresh temporary name, then rename it.

    On the CIFS share, a name another machine moved away can stay in the
    client's cache: it answers "exists" and opening it for writing fails with
    EINVAL, while a rename onto it works and clears the stale entry. Writing
    to a name no one has used, then renaming, also means the watcher never
    sees a half-copied ``.mkv``.
    """
    tmp = dest.with_name(f".{dest.stem}.{uuid4().hex[:8]}.partial")
    shutil.move(str(src), str(tmp))
    try:
        os.replace(tmp, dest)
    except OSError:
        # Put the file back so a retry still has it.
        shutil.move(str(tmp), str(src))
        raise


def _unique_dest_name(dest_dir: Path, name: str, taken: set[str]) -> str:
    """Return `name`, or a ' - Copy N' variant if it is already used.

    Delivery must never overwrite: a second disc of the same film, or a
    re-rip, lands in the same folder under the same computed name. The
    extras suffix has to stay at the very end of the stem, so the marker
    goes in front of it.
    """
    on_disk = _names_in(dest_dir)

    def used(candidate: str) -> bool:
        return candidate.lower() in taken or candidate.lower() in on_disk

    if not used(name):
        return name
    stem = name[:-4] if name.lower().endswith(".mkv") else name
    tail = ""
    for suffix in CONTENT_TYPE_SUFFIX.values():
        if stem.endswith(f"-{suffix}"):
            stem, tail = stem[: -(len(suffix) + 1)], f"-{suffix}"
            break
    n = 2
    while True:
        candidate = f"{stem} - Copy {n}{tail}.mkv"
        if not used(candidate):
            return candidate
        n += 1


class RipLineParser:
    """Turns makemkvcon robot-mode output for one title into log lines,
    progress and per-title facts (read errors, drive label, messages)."""

    _NOISE = (
        "MSG:1005,",  # "MakeMKV vX.Y.Z started"
        "MSG:3007,",  # "Using direct disc access mode"
        "MSG:3025,",  # "Title too short, skipped"
        "PRGT:",      # total-progress label (redundant)
    )
    _SUMMARY = ("MSG:5011,", "MSG:5014,", "MSG:5005,", "MSG:5036,")

    def __init__(self):
        self.fraction: Optional[float] = None
        # Failures to read the disc (damage, dirt). These matter.
        self.read_errors = 0
        # Structure notes MakeMKV works around. Routine on protected DVDs.
        self.source_warnings = 0
        self.drive_label: Optional[str] = None
        self.messages: list[str] = []
        # "Title #N was added (...)" lines: the disc structure as MakeMKV
        # sees it on this run, used to check it is the scanned disc.
        self.added_titles: list[str] = []
        self._last_prgc = ""
        self._warn_files: dict[str, int] = {}

    def warning_summary(self) -> list[str]:
        """One line per source file MakeMKV had to work around."""
        return [
            f"{count} structure warning(s) in {fname}, worked around by MakeMKV"
            for fname, count in self._warn_files.items()
        ]

    def _remember(self, text: str) -> None:
        if text and len(self.messages) < _MAX_TITLE_MESSAGES:
            self.messages.append(text)

    def feed(self, line: str) -> list[str]:
        """Consume one output line; return the lines to write to the job log."""
        if not line:
            return []

        if line.startswith("DRV:"):
            drv = makemkv_service.parse_drv_line(line)
            if drv and drv["label"]:
                self.drive_label = drv["label"]
            return []

        if line.startswith("MSG:3028,"):
            text = _extract_msg_text(line)
            if text:
                self.added_titles.append(text)
            return []

        if line.startswith(self._NOISE):
            return []

        if line.startswith("PRGV:"):
            parts = line[5:].split(",")
            if len(parts) == 3:
                try:
                    current, max_val = int(parts[0]), int(parts[2])
                    if max_val > 0:
                        self.fraction = current / max_val
                except ValueError:
                    pass
            return []

        if line.startswith("PRGC:"):
            msg = _extract_msg_text(line)
            if msg and msg != self._last_prgc:
                self._last_prgc = msg
                return [f"[rip] {msg}"]
            return []

        # MSG:4004 "The source file X is corrupt or invalid at offset N,
        # attempting to work around" is a note about the disc's structure.
        # Discs with structural copy protection produce the same dozens of
        # them on every pass of a title that rips perfectly, so they are
        # counted apart from read errors and summarised once per file.
        if line.startswith("MSG:4004,"):
            self.source_warnings += 1
            m = re.search(r"'([^']+)'", line)
            fname = m.group(1) if m else "unknown"
            self._warn_files[fname] = self._warn_files.get(fname, 0) + 1
            if self._warn_files[fname] == 1:
                return [f"[warn] {fname} has invalid structure — MakeMKV is working around it"]
            return []

        # A real failure to read the disc: MSG:2003 "Error '<scsi error>'
        # occurred while reading '<file>' at offset '<n>'".
        if line.startswith("MSG:2003,") or (line.startswith("MSG:") and "occurred while reading" in line):
            self.read_errors += 1
            text = _extract_msg_text(line)
            if self.read_errors <= 3:
                self._remember(text)
                return [f"[warn] Read error: {text}"]
            if self.read_errors == 4:
                return ["[warn] More read errors (suppressing further warnings; the count is kept)"]
            return []

        if line.startswith(self._SUMMARY):
            text = _extract_msg_text(line)
            if text:
                self._remember(text)
                return [f"[rip] {text}"]
            return []

        if line.startswith("MSG:"):
            self._remember(_extract_msg_text(line))
        return [line]


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
        declarations = data.get("titles") or data.get("title_declarations") or None
        episode_map = data.get("episode_map")
        title_content_types = data.get("title_content_types")
        if declarations:
            derived_eps, derived_types = manifest_service.derive_legacy_maps(declarations)
            if not episode_map:
                episode_map = derived_eps or None
            if not title_content_types:
                title_content_types = derived_types or None

        job = RipJob(
            id=str(uuid4()),
            disc_type=data["disc_type"],
            media_type=data["media_type"],
            title=data["title"],
            year=data["year"],
            imdb_id=data.get("imdb_id"),
            tmdb_id=data.get("tmdb_id"),
            season=data.get("season"),
            mkv_title_indices=data.get("mkv_title_indices", [0]),
            episode_map=episode_map,
            catalog_disc_id=data.get("catalog_disc_id"),
            title_content_types=title_content_types,
            title_declarations=declarations,
        )
        with SessionLocal() as db:
            # Tie the job to the scan it was configured from. An older UI
            # sends no scan_id, so fall back to the most recent scan; the rip
            # loop drops an inferred link if the drive reports another disc.
            scan = db.get(DiscScan, data["scan_id"]) if data.get("scan_id") else None
            link = "declared" if scan is not None else None
            if scan is None:
                scan = (
                    db.query(DiscScan)
                    .filter(DiscScan.created_at >= datetime.utcnow() - INFERRED_SCAN_MAX_AGE)
                    .order_by(DiscScan.created_at.desc())
                    .first()
                )
                link = "inferred" if scan is not None else None
            if scan is not None:
                job.scan_id, job.scan_link, job.fingerprint = scan.id, link, scan.fingerprint
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
                .filter(RipJob.status.in_(("ripping", "delivering")))
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
            if job and job.status in ("queued", "ripping", "delivering"):
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
            self._write_terminal_manifest(job_id, "cancelled")
        except Exception as exc:
            self._set_status(job_id, "failed", error=str(exc))
            await log(f"[ERROR] {exc}")
            self._write_terminal_manifest(job_id, "failed")
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
        self._update_field(job_id, rip_dir=rip_dir, rip_started_at=datetime.utcnow())

        await log(f"[rip] Output dir: {rip_dir}")

        scan = self._get_scan(job.scan_id) if settings.verify_disc_matches_scan else None
        expected_label = (scan.drive_label or "").strip() if scan is not None else ""
        # MakeMKV re-reads the disc structure for every rip and reports each
        # title it finds ("Title #2 was added (23 cell(s), 0:42:22)"). The
        # scan recorded the same lines, so one that the scan never saw means
        # a different disc is in the drive.
        expected_added = {
            m.get("text") for m in ((scan.parsed or {}).get("messages") or [])
            if m.get("code") == 3028
        } if scan is not None else set()
        scan_link = job.scan_link
        duration_scan = self._get_scan(job.scan_id)
        scan_durations = {
            t.get("index"): t.get("duration_seconds", 0)
            for t in ((duration_scan.parsed or {}).get("titles") or [])
        } if duration_scan is not None else {}
        # The job's title indices are the scan's: rip with the minimum title
        # length the scan was made with, or MakeMKV numbers the titles differently.
        minlength = makemkv_service.minlength_args(
            makemkv_service.scan_min_length(duration_scan.parsed) if duration_scan is not None else None
        )
        if minlength:
            await log(f"[rip] Scan included short titles: ripping with {minlength[0]}")

        total = len(job.mkv_title_indices)
        ripped_count = 0
        failed: list[str] = []

        # Rip phase occupies 0-60% of overall job progress.
        for title_num, idx in enumerate(job.mkv_title_indices, 1):
            await log(f"[rip] ── Title {title_num}/{total} (disc index {idx}) ──")
            # Snapshot before ripping so the files this title produced can be
            # identified afterwards and recorded against this title index.
            before_rip = set(Path(rip_dir).glob("*.mkv"))
            started_at = datetime.utcnow()
            proc = await asyncio.create_subprocess_exec(
                settings.makemkvcon_path,
                "--noscan", "-r", *minlength,
                "mkv", settings.disc_device,
                str(idx),
                rip_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            self._active_proc = proc

            parser = RipLineParser()
            wrong_disc: Optional[str] = None

            async for raw in proc.stdout:
                line = raw.decode(errors="replace").rstrip()
                for out in parser.feed(line):
                    await log(out)
                if parser.fraction is not None:
                    self._set_progress(job_id, ((title_num - 1) + parser.fraction) / total * 60)

                # The disc in the drive must be the one this job was set up from.
                mismatch: Optional[str] = None
                if wrong_disc is None:
                    if (
                        expected_label and parser.drive_label
                        and parser.drive_label.strip().lower() != expected_label.lower()
                    ):
                        mismatch = f"drive reports '{parser.drive_label}', the scan was of '{expected_label}'"
                    elif expected_added:
                        unknown = next((t for t in parser.added_titles if t not in expected_added), None)
                        if unknown:
                            mismatch = f"the scan has no title matching \"{unknown}\""
                if mismatch:
                    if scan_link == "declared":
                        wrong_disc = mismatch
                        try:
                            proc.terminate()
                        except Exception:
                            pass
                    else:
                        # The scan was only assumed to belong to this job.
                        await log(
                            f"[warn] The disc in the drive is not the last scanned disc ({mismatch}) "
                            "— rip continues without scan data"
                        )
                        self._update_field(job_id, scan_id=None, scan_link=None, fingerprint=None)
                        expected_label, expected_added, scan_durations = "", set(), {}

            await proc.wait()
            self._active_proc = None
            finished_at = datetime.utcnow()

            if self._job_status(job_id) == "cancelled":
                raise asyncio.CancelledError()
            if wrong_disc is not None:
                raise RuntimeError(
                    "The disc in the drive is not the disc this job was configured from "
                    f"({wrong_disc}). Scan the disc again and start a new job."
                )

            new_files = sorted(set(Path(rip_dir).glob("*.mkv")) - before_rip)
            error: Optional[str] = None
            if proc.returncode != 0:
                error = f"makemkvcon exited {proc.returncode}"
            elif not new_files:
                error = "makemkvcon produced no file"

            if error:
                # A non-zero exit leaves partial files behind; never deliver them.
                for partial in new_files:
                    try:
                        partial.unlink()
                    except OSError:
                        pass
                self._add_rip_title(
                    job_id=job_id, title_index=idx, file_seq=0, status="failed", error=error,
                    rip_started_at=started_at, rip_finished_at=finished_at,
                    read_errors=parser.read_errors, messages=parser.messages,
                )
                failed.append(f"{idx} ({error})")
                await log(f"[rip] Title {title_num}/{total} (disc index {idx}) FAILED: {error}")
                continue

            if len(new_files) > 1:
                await log(f"[rip] Title {idx} produced {len(new_files)} files — all are recorded against it")

            for seq, produced in enumerate(new_files):
                # rip_NNNN[_SS].mkv keeps a stable, collision-free name in the
                # rip dir; the title index lives on the RipTitle row.
                suffix = f"_{seq:02d}" if seq else ""
                canonical = Path(rip_dir) / f"rip_{title_num:04d}{suffix}.mkv"
                produced.rename(canonical)
                await log(f"[rip] {produced.name} → {canonical.name}")

                info = await asyncio.to_thread(probe_service.inspect_file, str(canonical))
                messages = list(parser.messages) + parser.warning_summary()
                crop_note = await self._crop_letterbox(canonical, info.get("probe"), log)
                if crop_note:
                    messages.append(crop_note)
                    # Size, hash and streams are those of the file that is delivered.
                    info = await asyncio.to_thread(probe_service.inspect_file, str(canonical))
                probe = info.get("probe") or {}
                scanned = scan_durations.get(idx) or 0
                measured = probe.get("duration_seconds") or 0
                if scanned and measured and len(new_files) == 1:
                    if abs(measured - scanned) / scanned > DURATION_TOLERANCE:
                        note = (
                            f"Duration mismatch: scan said {scanned}s, ripped file is {measured:.0f}s"
                        )
                        messages.append(note)
                        await log(f"[warn] {note}")

                self._add_rip_title(
                    job_id=job_id, title_index=idx, file_seq=seq, status="ripped",
                    makemkv_filename=produced.name, rip_path=str(canonical),
                    size_bytes=info.get("size_bytes"), sha256=info.get("sha256"),
                    probe=info.get("probe"),
                    rip_started_at=started_at, rip_finished_at=finished_at,
                    read_errors=parser.read_errors, messages=messages,
                )
                ripped_count += 1

            await log(f"[rip] Title {title_num}/{total} done")

        self._update_field(job_id, rip_finished_at=datetime.utcnow())
        if ripped_count == 0:
            raise RuntimeError(f"No titles were ripped. Failed: {', '.join(failed) or 'none attempted'}")
        if failed:
            self._update_field(
                job_id, error=f"{len(failed)} of {total} title(s) failed: {', '.join(failed)}"
            )

    def _plan_destinations(self, job: RipJob, rows: list[RipTitle], dest_dir: Path) -> dict[int, Path]:
        """Destination path for every ripped file, keyed by RipTitle id.

        Names come from what each row's own title index was declared to be —
        never from the file's position in a directory listing.
        """
        declarations = manifest_service.declarations_for(job)
        episode_map = job.episode_map or {}

        def extras_type(row: RipTitle) -> Optional[str]:
            content_type = declarations.get(row.title_index, {}).get("content_type")
            return content_type if content_type in CONTENT_TYPE_SUFFIX else None

        main_rows = [r for r in rows if extras_type(r) is None]
        content_type_counts: dict[str, int] = {}
        taken: set[str] = set()
        planned: dict[int, Path] = {}
        for row in rows:
            content_type = extras_type(row)
            position = main_rows.index(row) if content_type is None else 0
            name = _build_dest_name(
                job.media_type, job.title, job.year,
                position, len(main_rows), str(row.title_index), episode_map, job.season or 1,
                content_type=content_type,
                content_type_counts=content_type_counts,
            )
            name = _unique_dest_name(dest_dir, name, taken)
            taken.add(name.lower())
            planned[row.id] = dest_dir / name
        return planned

    async def _crop_letterbox(self, path: Path, probe: Optional[dict], log) -> Optional[str]:
        """Crop bars burned into a letterboxed 4:3 DVD title, in place. Returns the
        note for the title's messages when it was cropped, else None."""
        if not settings.crop_letterboxed_dvds:
            return None
        crop = await asyncio.to_thread(letterbox_service.detect, str(path), probe)
        if crop is None:
            return None
        video = next((s for s in (probe or {}).get("streams") or [] if s.get("codec_type") == "video"), {})
        width, height = int(video.get("width") or 0), int(video.get("height") or 0)
        interlaced = letterbox_service.is_interlaced(probe)
        await log(f"[crop] {path.name}: bars burned into the picture; re-encoding to "
                  f"{crop.width}x{crop.height}{' (deinterlaced)' if interlaced else ''}")
        refused = await asyncio.to_thread(
            letterbox_service.crop_in_place, str(path), crop, interlaced,
            (probe or {}).get("duration_seconds") or 0,
        )
        if refused:
            await log(f"[warn] {path.name}: kept as ripped, the crop failed ({refused})")
            return f"Letterbox crop failed, kept as ripped: {refused}"
        await log(f"[crop] {path.name} done")
        return letterbox_service.describe(crop, width, height, interlaced)

    async def _run_deliver(self, job_id: str, log):
        job = self._set_status(job_id, "delivering")
        title_dir = Path(settings.ingest_root) / _ingest_folder_name(job)
        dest_dir = title_dir
        if job.media_type == "show" and job.season is not None:
            dest_dir = dest_dir / f"Season {job.season:02d}"

        dest_dir.mkdir(parents=True, exist_ok=True)
        await log(f"[deliver] Destination: {dest_dir}")

        rows = self._rip_titles(job_id)
        if not rows:
            # No per-title records (job ripped by an older version): fall back
            # to pairing the rip dir's files with the selected titles in order.
            for i, src in enumerate(sorted(Path(job.rip_dir).glob("*.mkv"))):
                idx = job.mkv_title_indices[i] if i < len(job.mkv_title_indices) else i
                self._add_rip_title(
                    job_id=job_id, title_index=idx, file_seq=0, status="ripped",
                    rip_path=str(src), size_bytes=src.stat().st_size,
                )
            rows = self._rip_titles(job_id)

        ripped = [r for r in rows if r.status == "ripped" and r.rip_path]
        planned = self._plan_destinations(job, ripped, dest_dir)
        for row in ripped:
            self._update_rip_title(row.id, dest_path=str(planned[row.id]))

        # The manifest goes into the folder before any file does, so whatever
        # watches the folder can always find the record for a file it sees.
        catalog_disc = await catalog_client.get_disc(job.catalog_disc_id) if job.catalog_disc_id else None
        manifest_folder = self._write_manifest(job_id, "delivering", catalog_disc, str(title_dir))
        if manifest_folder:
            await log(f"[deliver] Manifest: {manifest_folder}")
        else:
            await log("[deliver] Warning: could not write the manifest into the delivery folder")

        # Tell the library which files are coming before the first one lands.
        if await self.sync_library(job_id):
            await log("[deliver] Rip record registered with my-media-manager")
        else:
            await log("[deliver] my-media-manager did not take the rip record yet — will retry after delivery")

        delivered = []
        for row in ripped:
            src_path, dest_file = Path(row.rip_path), planned[row.id]
            await log(f"[deliver] Moving {src_path.name} → {dest_file.name}")
            _deliver_file(src_path, dest_file)
            self._update_rip_title(row.id, status="delivered")
            delivered.append(str(dest_file))

        # Clean up rip dir
        try:
            shutil.rmtree(job.rip_dir)
            await log(f"[deliver] Cleaned rip dir: {job.rip_dir}")
        except Exception as e:
            await log(f"[deliver] Warning: could not clean rip dir: {e}")

        self._update_field(job_id, output_paths=delivered, delivered_at=datetime.utcnow())
        any_failed = any(r.status == "failed" for r in rows)
        self._write_manifest(job_id, "partial" if any_failed else "delivered", catalog_disc, str(title_dir))

        if await self.sync_library(job_id):
            await log(f"[deliver] Rip record stored in my-media-manager ({len(delivered)} file(s))")
        else:
            await log(
                "[deliver] Warning: my-media-manager did not take the rip record "
                f"({self._sync_error(job_id)}) — it is kept here and retried"
            )
            # A library without the rip-record endpoint still gets the disc link.
            if job.catalog_disc_id and delivered:
                linked = await catalog_client.link_delivered_files(delivered, job.catalog_disc_id)
                await log(
                    f"[deliver] Linked {len(delivered)} file(s) to catalog disc {job.catalog_disc_id}"
                    if linked else "[deliver] Warning: could not link files to catalog disc either"
                )

        self._set_status(job_id, "done")
        await log(f"[deliver] Job complete. {len(delivered)} file(s) delivered.")

    # ------------------------------------------------------------------
    # Manifest
    # ------------------------------------------------------------------

    def _write_manifest(
        self, job_id: str, status: str, catalog_disc: Optional[dict], folder: Optional[str]
    ) -> Optional[str]:
        """Build the manifest from the DB and write it. Returns the path of
        the copy in the delivery folder (None if that write failed)."""
        with SessionLocal() as db:
            job = db.get(RipJob, job_id)
            scan = db.get(DiscScan, job.scan_id) if job.scan_id else None
            rows = (
                db.query(RipTitle).filter(RipTitle.job_id == job_id).order_by(RipTitle.id).all()
            )
            if catalog_disc is None:
                # Keep the catalog snapshot from an earlier write of this manifest.
                previous = manifest_service.read_stored_manifest(job_id) or {}
                snapshot = previous.get("catalog") or {}
                catalog_disc = {
                    "title": snapshot.get("title"), "barcode": snapshot.get("barcode"),
                    "discNumber": snapshot.get("disc_number"), "edition": snapshot.get("edition"),
                    "format": snapshot.get("format"), "setId": snapshot.get("release_id"),
                }
            payload = manifest_service.build_manifest(job, scan, rows, catalog_disc, status)
            paths = manifest_service.write_manifest(payload, folder)
            job.manifest_path = paths["stored"]
            db.commit()
            return paths["folder"]

    # ------------------------------------------------------------------
    # Library sync (outbox)
    # ------------------------------------------------------------------

    async def sync_library(self, job_id: str) -> bool:
        """Send the job's stored manifest to my-media-manager.

        The outcome is recorded on the job (`library_sync_state` = synced |
        failed, with the error), so a rip made while the library was down is
        picked up by `run_sync_worker` or `POST /jobs/sync-all`.
        """
        payload = manifest_service.read_stored_manifest(job_id)
        if payload is None:
            return False
        ok, error = await catalog_client.put_rip_record(job_id, payload)
        self._update_sync(job_id, "synced" if ok else "failed", None if ok else error)
        if not ok:
            logger.warning("library_sync_failed", job_id=job_id, error=error)
        return ok

    def mark_sync_pending(self, job_id: str) -> None:
        """The stored manifest changed (e.g. a file was renamed): send it again."""
        self._update_sync(job_id, "pending", None)

    def unsynced_job_ids(self) -> list[str]:
        """Delivered jobs whose record has not reached the library."""
        with SessionLocal() as db:
            rows = (
                db.query(RipJob.id)
                .filter(
                    RipJob.status == "done",
                    RipJob.manifest_path.isnot(None),
                    RipJob.library_sync_state.in_(("pending", "failed")),
                )
                .order_by(RipJob.created_at)
                .all()
            )
            return [row[0] for row in rows]

    async def sync_all(self, include_synced: bool = False) -> dict:
        """Push every delivered job's record. With `include_synced`, also the
        ones already sent and the ones never tried (a backfill)."""
        if include_synced:
            with SessionLocal() as db:
                ids = [
                    row[0] for row in db.query(RipJob.id)
                    .filter(RipJob.status == "done", RipJob.manifest_path.isnot(None))
                    .order_by(RipJob.created_at).all()
                ]
        else:
            ids = self.unsynced_job_ids()
        synced, failed = [], []
        for job_id in ids:
            (synced if await self.sync_library(job_id) else failed).append(job_id)
        return {"synced": synced, "failed": failed}

    async def run_sync_worker(self):
        """Retry rip records the library has not taken yet."""
        while True:
            await asyncio.sleep(max(5, settings.library_sync_interval_seconds))
            try:
                for job_id in self.unsynced_job_ids():
                    if job_id != self._active_job_id:
                        await self.sync_library(job_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # the loop must survive anything
                logger.warning("library_sync_worker_error", error=str(exc))

    def _update_sync(self, job_id: str, state: str, error: Optional[str]) -> None:
        with SessionLocal() as db:
            job = db.get(RipJob, job_id)
            if job is not None:
                job.library_sync_state = state
                job.library_sync_error = error
                db.commit()

    def _sync_error(self, job_id: str) -> str:
        with SessionLocal() as db:
            job = db.get(RipJob, job_id)
            return (job.library_sync_error if job else None) or "no response"

    def _write_terminal_manifest(self, job_id: str, status: str) -> None:
        """Keep a record of failed/cancelled jobs too (ripper volume only)."""
        try:
            self._write_manifest(job_id, status, None, None)
        except Exception as exc:  # never mask the job's own failure
            logger.warning("manifest_write_failed", job_id=job_id, error=str(exc))

    def rename_delivered_file(self, job_id: str, old_path: str, new_path: str) -> None:
        """Keep the per-title record and both manifest copies in step with a
        rename of a delivered file."""
        with SessionLocal() as db:
            for row in db.query(RipTitle).filter(
                RipTitle.job_id == job_id, RipTitle.dest_path == old_path
            ):
                row.dest_path = new_path
            db.commit()
        previous = manifest_service.read_stored_manifest(job_id)
        if previous is None:
            return
        job = self.get_job(job_id)
        title_dir = Path(settings.ingest_root) / _ingest_folder_name(job)
        self._write_manifest(
            job_id, previous.get("status", "delivered"), None,
            str(title_dir) if title_dir.is_dir() else None,
        )
        # The library keys the record by path: it has to hear about the rename.
        self.mark_sync_pending(job_id)

    # ------------------------------------------------------------------
    # Per-title records
    # ------------------------------------------------------------------

    def _add_rip_title(self, **fields) -> None:
        with SessionLocal() as db:
            db.add(RipTitle(**fields))
            db.commit()

    def _update_rip_title(self, row_id: int, **fields) -> None:
        with SessionLocal() as db:
            row = db.get(RipTitle, row_id)
            for k, v in fields.items():
                setattr(row, k, v)
            db.commit()

    def _rip_titles(self, job_id: str) -> list[RipTitle]:
        with SessionLocal() as db:
            return db.query(RipTitle).filter(RipTitle.job_id == job_id).order_by(RipTitle.id).all()

    def rip_titles(self, job_id: str) -> list[RipTitle]:
        return self._rip_titles(job_id)

    def _get_scan(self, scan_id: Optional[str]) -> Optional[DiscScan]:
        if not scan_id:
            return None
        with SessionLocal() as db:
            return db.get(DiscScan, scan_id)

    def _job_status(self, job_id: str) -> Optional[str]:
        with SessionLocal() as db:
            job = db.get(RipJob, job_id)
            return job.status if job else None

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
            terminal = status in ("done", "failed", "cancelled")
            duration_seconds = (
                (job.updated_at - job.created_at).total_seconds() if terminal and job.created_at else None
            )
            if status in ("failed", "cancelled"):
                logger.warning(
                    "job_status_changed",
                    job_id=job_id, status=status, title=job.title, error=error,
                    duration_seconds=duration_seconds,
                )
            else:
                logger.info(
                    "job_status_changed",
                    job_id=job_id, status=status, title=job.title,
                    duration_seconds=duration_seconds,
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
