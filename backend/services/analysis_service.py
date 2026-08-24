"""
AI-powered failure analysis for rip jobs using Ollama.
"""

import json
import re
from datetime import datetime
from uuid import uuid4

import httpx

from config.settings import settings
from db.database import SessionLocal
from db.models import JobAnalysis, RipJob


_SYSTEM_PROMPT = (
    "You are an expert at diagnosing failures in disc ripping pipelines. "
    "You analyze logs from MakeMKV (disc ripping) and HandBrakeCLI/ffmpeg (video encoding). "
    "Respond ONLY with valid JSON matching the exact structure requested. No prose outside the JSON."
)

_USER_PROMPT_TEMPLATE = """\
A disc rip job failed. Analyze the following log and respond with a JSON object.

Job error message: {error}

Full log:
{log_content}

Respond with this exact JSON structure (all fields required):
{{
  "error_type": "<one of: disc_read_error | makemkv_corruption | encode_failure | delivery_failure | configuration_error | other>",
  "error_summary": "<1-2 sentence human-readable summary of what went wrong>",
  "suggested_fix": "<concrete steps the user should take to resolve this>",
  "claude_prompt": "<a ready-to-paste Claude Code prompt to implement a code fix, or 'No code change needed.' if the fix is operational>"
}}"""


async def analyze_failed_job(job_id: str) -> JobAnalysis:
    """
    Analyze a failed rip job using Ollama. Reads the full log file (falls back
    to the DB truncated log). Saves and returns the JobAnalysis record.
    """
    with SessionLocal() as db:
        job = db.get(RipJob, job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")
        if job.status != "failed":
            raise ValueError(f"Job {job_id} is not failed (status: {job.status})")
        job_error = job.error or ""
        job_log_path = job.log_path
        job_log_db = job.log or ""

    # Read full log from file; fall back to DB truncated log
    log_content = ""
    if job_log_path:
        try:
            with open(job_log_path, "r", encoding="utf-8", errors="replace") as f:
                log_content = f.read()
        except OSError:
            log_content = job_log_db
    else:
        log_content = job_log_db

    # Keep last ~8000 chars to avoid overwhelming the model
    if len(log_content) > 8000:
        log_content = "...[truncated]\n" + log_content[-8000:]

    user_prompt = _USER_PROMPT_TEMPLATE.format(
        error=job_error,
        log_content=log_content,
    )

    ollama_url = f"{settings.ollama_host.rstrip('/')}/api/chat"
    payload = {
        "model": settings.ollama_analysis_model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "format": "json",
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(ollama_url, json=payload)
        response.raise_for_status()

    raw_response = response.json()
    # Ollama /api/chat non-streaming: {"message": {"role": "assistant", "content": "..."}}
    content = raw_response.get("message", {}).get("content", "")

    parsed: dict = {}
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r'\{.*\}', content, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except json.JSONDecodeError:
                pass

    analysis = JobAnalysis(
        id=str(uuid4()),
        job_id=job_id,
        created_at=datetime.utcnow(),
        error_type=parsed.get("error_type", "other"),
        error_summary=parsed.get("error_summary", "Analysis could not be parsed."),
        suggested_fix=parsed.get("suggested_fix", ""),
        claude_prompt=parsed.get("claude_prompt", ""),
        full_analysis=content,
        model_used=settings.ollama_analysis_model,
        log_path=job_log_path,
    )

    with SessionLocal() as db:
        db.add(analysis)
        db.commit()
        db.refresh(analysis)
        return analysis


def get_job_analysis(job_id: str) -> JobAnalysis | None:
    """Return the most recent analysis for a job, or None."""
    with SessionLocal() as db:
        return (
            db.query(JobAnalysis)
            .filter(JobAnalysis.job_id == job_id)
            .order_by(JobAnalysis.created_at.desc())
            .first()
        )
