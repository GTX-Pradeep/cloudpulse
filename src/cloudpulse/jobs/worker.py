"""Worker process: claims jobs and runs them, renewing its lease while it works."""

from __future__ import annotations

import threading
import time
import traceback

from cloudpulse import pipeline
from cloudpulse.config import Settings
from cloudpulse.jobs import queue


class _Heartbeat:
    def __init__(self, meta_dsn: str, job_id: int, worker: str, every: float = 30.0):
        self.args = (meta_dsn, job_id, worker)
        self.every = every
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self.every):
            if not queue.heartbeat(*self.args):
                return

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._t.join(timeout=5)


def handle(settings: Settings, job: dict, log=print) -> None:
    p = job["payload"]
    lab_state = pipeline.LabState(**p["lab_state"])
    if job["kind"] == "investigate":
        for cid in pipeline.investigate(settings, p["incident_id"], lab_state, log=log):
            queue.enqueue(settings.meta_dsn, "experiment",
                          {"candidate_id": cid, "lab_state": p["lab_state"]})
    elif job["kind"] == "experiment":
        pipeline.run_candidate_experiment(settings, p["candidate_id"], lab_state, log=log)
    else:  # pragma: no cover - guarded by a CHECK constraint
        raise ValueError(job["kind"])


def run_once(settings: Settings, worker: str | None = None, log=print) -> bool:
    """Claim and run one job. Returns False when the queue is empty."""
    worker = worker or queue.worker_id()
    job = queue.claim(settings.meta_dsn, worker)
    if job is None:
        return False
    log(f"[worker {worker}] job {job['id']} {job['kind']} attempt {job['attempts']}")
    try:
        with _Heartbeat(settings.meta_dsn, job["id"], worker):
            handle(settings, job, log=log)
        queue.complete(settings.meta_dsn, job["id"], worker)
    except Exception:
        err = traceback.format_exc()
        log(f"[worker {worker}] job {job['id']} failed:\n{err}")
        queue.fail(settings.meta_dsn, job["id"], worker, err)
    return True


def drain(settings: Settings, log=print) -> int:
    n = 0
    while run_once(settings, log=log):
        n += 1
    queue.sweep(settings.meta_dsn)
    return n


def run_forever(settings: Settings, poll_seconds: float = 2.0, log=print) -> None:  # pragma: no cover
    while True:
        if not run_once(settings, log=log):
            queue.sweep(settings.meta_dsn)
            time.sleep(poll_seconds)
