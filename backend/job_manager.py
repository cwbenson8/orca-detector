"""
Job Manager — in-memory store for prediction jobs with async log streaming.

For production use, swap the _store dict for Redis or a lightweight SQLite DB.
"""

import asyncio
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional


class JobStatus:
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class JobManager:
    def __init__(self):
        # job_id -> job dict
        self._store: dict[str, dict] = {}
        # job_id -> asyncio.Queue of log lines (None = sentinel for done)
        self._log_queues: dict[str, asyncio.Queue] = {}

    # ── CRUD ──────────────────────────────────────────────────────────────────

    def create(self, job_id: str, gcs_path: str) -> dict:
        job = {
            "job_id": job_id,
            "gcs_path": gcs_path,
            "status": JobStatus.QUEUED,
            "created_at": _now(),
            "updated_at": _now(),
            "logs": [],
            "annotations": None,
            "annotation_file": None,
            "error": None,
        }
        self._store[job_id] = job
        self._log_queues[job_id] = asyncio.Queue()
        return job

    def get(self, job_id: str) -> Optional[dict]:
        return self._store.get(job_id)

    def list_all(self) -> list[dict]:
        return sorted(
            self._store.values(),
            key=lambda j: j["created_at"],
            reverse=True,
        )

    # ── Status & logging ──────────────────────────────────────────────────────

    def set_status(self, job_id: str, status: str):
        if job_id in self._store:
            self._store[job_id]["status"] = status
            self._store[job_id]["updated_at"] = _now()

    def log(self, job_id: str, line: str):
        """Append a log line and push it to any active WebSocket subscriber."""
        if job_id not in self._store:
            return
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        entry = f"[{ts}] {line}"
        self._store[job_id]["logs"].append(entry)
        if job_id in self._log_queues:
            try:
                self._log_queues[job_id].put_nowait(entry)
            except asyncio.QueueFull:
                pass

    def finish(self, job_id: str, annotations: list, annotation_file: str, wav_file: str = ""):
        if job_id not in self._store:
            return
        self._store[job_id].update({
            "status": JobStatus.DONE,
            "annotations": annotations,
            "annotation_file": annotation_file,
            "wav_file": wav_file,
            "updated_at": _now(),
            "detection_count": len(annotations),
        })
        # Signal WebSocket consumers that the job is complete
        if job_id in self._log_queues:
            self._log_queues[job_id].put_nowait(None)  # sentinel

    # ── Async log streaming ───────────────────────────────────────────────────

    async def stream_logs(self, job_id: str) -> AsyncGenerator[str, None]:
        """
        Async generator yielding log lines as they arrive.
        Exits when the job finishes (sentinel None received) or job not found.
        """
        q = self._log_queues.get(job_id)
        if not q:
            # Job already finished — replay stored logs
            job = self.get(job_id)
            if job:
                for line in job.get("logs", []):
                    yield line
            return

        while True:
            try:
                line = await asyncio.wait_for(q.get(), timeout=120.0)
            except asyncio.TimeoutError:
                break
            if line is None:  # sentinel: job done
                break
            yield line


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
