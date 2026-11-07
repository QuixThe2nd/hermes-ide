"""Best-effort cron job subjects for user-facing progress labels."""

from __future__ import annotations


def resolve_cron_job_name(job_id: str) -> str | None:
    """Return only the stored job's non-empty name, or None when it cannot be resolved."""
    try:
        from cron.jobs import get_job

        job = get_job(job_id)
        name = job.get("name") if isinstance(job, dict) else None
        return name.strip() if isinstance(name, str) and name.strip() else None
    except Exception:
        return None
