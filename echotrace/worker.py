"""Production Redis dispatcher and orchestrator worker entry point."""

from __future__ import annotations

import os
import logging
import time
from threading import Event, Thread
from typing import Any

from echotrace.runtime.environment import load_local_environment


logger = logging.getLogger("echotrace.worker")


class WorkerService:
    """Run dispatcher and role consumers beside a local API when requested."""

    def __init__(self, runtime: Any, *, owner: str | None = None) -> None:
        self.runtime = runtime
        self.owner = owner or os.getenv("ECHOTRACE_DISPATCHER_ID", os.getenv("ECHOTRACE_WORKER_ID", "orchestrator-1"))
        self.stop_event = Event()
        self.thread = Thread(target=self._run, name="echotrace-worker-service", daemon=True)

    def start(self) -> None:
        if not self.thread.is_alive():
            self.thread.start()

    def close(self, *, timeout: float = 10.0) -> None:
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=max(0.1, timeout))

    def is_alive(self) -> bool:
        return self.thread.is_alive()

    def _run(self) -> None:
        def consume(worker, *, reclaim_idle_ms: int = 30_000) -> None:
            while not self.stop_event.is_set():
                try:
                    worker.process_once(count=1, block_ms=500)
                    worker.reclaim_once(min_idle_ms=reclaim_idle_ms, count=4)
                except Exception:
                    logger.exception("role worker poll failed: %s", getattr(worker, "consumer", "unknown"))
                    self.stop_event.wait(1)

        workers = [self.runtime.orchestrator_worker, *self.runtime.node_workers]
        if self.runtime.report_worker is not None:
            workers.append(self.runtime.report_worker)
        threads = [
            Thread(target=consume, args=(worker,), name=f"worker-{worker.consumer}", daemon=True)
            for worker in workers
        ]
        for thread in threads:
            thread.start()
        last_recovery = 0.0
        try:
            while not self.stop_event.is_set():
                self.runtime.dispatch_once(owner=self.owner, limit=50)
                if time.monotonic() - last_recovery >= 10:
                    self.runtime.task_service.recover_expired_tasks(limit=100)
                    self.runtime.task_service.redrive_pending_tasks(limit=100)
                    last_recovery = time.monotonic()
                self.stop_event.wait(0.05)
        except Exception:
            logger.exception("outbox dispatcher loop failed")
            self.stop_event.set()
        finally:
            self.stop_event.set()
            for thread in threads:
                thread.join(timeout=2)


def main() -> None:
    load_local_environment()
    from app.investigation_composition import build_investigation_runtime

    runtime = build_investigation_runtime()
    service = WorkerService(runtime)
    service.start()
    try:
        while service.is_alive():
            service.thread.join(timeout=1)
    except KeyboardInterrupt:
        pass
    finally:
        service.close()
        runtime.close()


if __name__ == "__main__":
    main()


__all__ = ["WorkerService", "main"]

