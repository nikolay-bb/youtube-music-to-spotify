"""Starting, watching, cancelling and exporting a transfer job."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from ..models import Job, JobProgress, JobSummary, TransferRequest
from ..transfer import apply_matches, results_to_csv, run_job, store

router = APIRouter(prefix="/api/transfer", tags=["transfer"])


@router.post("", response_model=Job)
async def start(request: TransferRequest) -> Job:
    """Begin a transfer and return straight away with the job id.

    The work runs as a background task on the same event loop. The UI then polls
    ``GET /api/transfer/{job_id}`` to follow it.
    """
    if not request.playlist_ids and not request.include_liked:
        raise HTTPException(status_code=400, detail="Select at least one playlist.")

    job = store.create(dry_run=request.dry_run, request=request)
    task = asyncio.create_task(run_job(job.id, request))
    store.register_task(job.id, task)
    return job


@router.get("", response_model=list[JobSummary])
async def recent(limit: int = 20) -> list[JobSummary]:
    """Past transfers, newest first, so a restart does not hide finished work.

    Summaries only. A finished job file is about a megabyte of results, and the
    home screen needs none of it.
    """
    return store.list_summaries(limit)


@router.get("/{job_id}", response_model=Job)
async def get(job_id: str) -> Job:
    """Current progress and every result so far."""
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job.")
    return job


@router.get("/{job_id}/status", response_model=JobProgress)
async def status(job_id: str) -> JobProgress:
    """The heartbeat for the transfer screen: everything but the results.

    The screen polls this every second or two, so it must stay small. A
    finished job with results is about a megabyte; this is a few kilobytes.
    """
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job.")
    return JobProgress(**job.model_dump(exclude={"results"}))


@router.post("/{job_id}/resume", response_model=Job)
async def resume(job_id: str) -> Job:
    """Carry on a job that stopped, keeping every result it already had.

    Spotify's Development Mode quota is easy to exhaust on a large library. When
    that happens the job stops with its results intact, and this picks up from
    the next unprocessed song rather than starting again.
    """
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job.")
    if job.status == "running":
        raise HTTPException(status_code=409, detail="That job is still running.")
    if job.request is None:
        raise HTTPException(
            status_code=400,
            detail="This job was created before resuming was supported. Start a new one.",
        )

    # A stop from an earlier run must not kill this fresh attempt, but one
    # pressed in the moment before the task starts must be honoured.
    store.clear_cancelled(job_id)
    task = asyncio.create_task(run_job(job.id, job.request, resume=True))
    store.register_task(job.id, task)
    return job


@router.post("/{job_id}/apply", response_model=Job)
async def apply(job_id: str) -> Job:
    """Write a dry run's matches to Spotify, without searching for them again.

    Use this after a dry run you are happy with. It costs a handful of requests
    instead of one search per song, which matters on a Development Mode quota.
    """
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job.")
    if job.status == "running":
        raise HTTPException(status_code=409, detail="That job is still running.")
    if job.counters.matched == 0:
        raise HTTPException(status_code=400, detail="This job matched nothing to add.")

    # A stop from an earlier run must not kill this fresh attempt, but one
    # pressed in the moment before the task starts must be honoured.
    store.clear_cancelled(job_id)
    task = asyncio.create_task(apply_matches(job_id))
    store.register_task(job_id, task)
    return job


@router.post("/{job_id}/cancel")
async def cancel(job_id: str) -> dict[str, bool]:
    """Ask a running job to stop after the song it is on."""
    if not store.cancel(job_id):
        raise HTTPException(status_code=404, detail="No such job, or it already finished.")
    return {"ok": True}


@router.get("/{job_id}/report.csv", response_class=PlainTextResponse)
async def report(job_id: str, only_problems: bool = False) -> PlainTextResponse:
    """Download the results as a spreadsheet.

    ``?only_problems=true`` gives just the rows that need you: low confidence,
    not found, and errors.
    """
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="No such job.")

    suffix = "problems" if only_problems else "full"
    return PlainTextResponse(
        results_to_csv(job, only_problems=only_problems),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="transfer-{job_id}-{suffix}.csv"'},
    )
