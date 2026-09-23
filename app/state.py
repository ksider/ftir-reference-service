from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ServiceState:
    """Small persistent state store for installation jobs and their logs."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "service-state.json"
        self._lock = threading.RLock()
        data_dir.mkdir(parents=True, exist_ok=True)
        self._state: dict[str, Any] = self._read()
        self._recover_interrupted_job()

    def _recover_interrupted_job(self) -> None:
        """A worker thread cannot survive a process restart, but downloads can resume."""
        active = self._state.get("activeJob")
        if not active:
            return
        for job in self._state.get("jobs", []):
            if job.get("id") == active.get("id"):
                job["status"] = "interrupted"
                job["error"] = "Service restarted; retry the job to resume from saved partial files."
                job["finishedAt"] = utc_now()
                break
        self._state["activeJob"] = None
        self._state["status"] = "failed"
        self._state["logs"] = (self._state.get("logs", []) + [{
            "at": utc_now(),
            "jobId": active.get("id"),
            "level": "WARNING",
            "message": "The service restarted while a job was running. It can safely be retried.",
        }])[-500:]
        self._save()

    def _read(self) -> dict[str, Any]:
        default = {
            "status": "uninitialized",
            "updatedAt": utc_now(),
            "dataset": None,
            "activeJob": None,
            "jobs": [],
            "logs": [],
        }
        if not self.path.exists():
            return default
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            return {**default, **saved}
        except (OSError, json.JSONDecodeError):
            return default

    def _save(self) -> None:
        self._state["updatedAt"] = utc_now()
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._state))

    def set_status(self, status: str, dataset: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._state["status"] = status
            if dataset is not None:
                self._state["dataset"] = dataset
            self._save()

    def start_job(self, kind: str, initial_status: str = "downloading") -> dict[str, Any]:
        with self._lock:
            active = self._state.get("activeJob")
            if active:
                raise RuntimeError("Another installation job is already running")
            job = {
                "id": uuid.uuid4().hex,
                "kind": kind,
                "status": "running",
                "startedAt": utc_now(),
                "finishedAt": None,
                "error": None,
            }
            self._state["activeJob"] = job
            self._state["jobs"] = ([job] + self._state.get("jobs", []))[:20]
            self._state["status"] = initial_status
            self._save()
            return dict(job)

    def log(self, job_id: str, message: str, level: str = "INFO") -> None:
        entry = {"at": utc_now(), "jobId": job_id, "level": level, "message": message}
        with self._lock:
            self._state["logs"] = (self._state.get("logs", []) + [entry])[-500:]
            self._save()

    def finish_job(self, job_id: str, *, error: str | None = None, dataset: dict[str, Any] | None = None) -> None:
        with self._lock:
            for job in self._state.get("jobs", []):
                if job.get("id") == job_id:
                    job["status"] = "failed" if error else "completed"
                    job["error"] = error
                    job["finishedAt"] = utc_now()
                    break
            self._state["activeJob"] = None
            self._state["status"] = "failed" if error else "ready"
            if dataset is not None:
                self._state["dataset"] = dataset
            self._save()
