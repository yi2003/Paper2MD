#!/usr/bin/env python3
"""In-memory FIFO task queue for the OCR service.

- Single background worker thread (serial processing — matches pipeline lock).
- Tasks expire 5 min after reaching 'done' or 'failed' (TTL).
- Pending/processing tasks do NOT expire — they stay queued until processed.
"""

import queue
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from pipeline import run_ocr

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

RESULT_TTL_SEC = 300       # 5 minutes — how long done/failed results live
CLEANUP_INTERVAL_SEC = 30  # how often the cleanup thread runs

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Task:
    task_id: str
    status: str = "pending"        # pending → processing → done | failed
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    image_path: Path | None = None  # temp file on disk (cleaned up with task)
    result: dict | None = None      # {markdown, images: [...]}
    error: str | None = None


# ---------------------------------------------------------------------------
# Task manager
# ---------------------------------------------------------------------------

class TaskManager:
    """Thread-safe in-memory task store + FIFO worker."""

    def __init__(self, work_dir: Path):
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue[str] = queue.Queue()
        self._work_dir = work_dir
        self._worker_thread: threading.Thread | None = None
        self._cleanup_thread: threading.Thread | None = None
        self._running = False

    # --- public API ---

    def create_task(self, image_data: bytes, filename: str) -> str:
        """Save uploaded image, create task, enqueue for processing.

        Returns the task_id.
        """
        task_id = f"ocr-{uuid.uuid4().hex[:12]}"
        task_dir = self._work_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)

        # Save uploaded image
        suffix = Path(filename).suffix or ".jpg"
        image_path = task_dir / f"page{suffix}"
        image_path.write_bytes(image_data)

        task = Task(task_id=task_id, image_path=image_path)

        with self._lock:
            self._tasks[task_id] = task

        self._queue.put(task_id)
        print(f"  [task:{task_id}] created (queue size: {self._queue.qsize()})",
              file=sys.stderr)
        return task_id

    def get_task(self, task_id: str) -> dict | None:
        """Return task status + result as a dict, or None if not found.

        Result is only included when status is 'done'.
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None

        response: dict = {
            "task_id": task.task_id,
            "status": task.status,
        }
        if task.status == "done" and task.result:
            response["result"] = task.result
        if task.status == "failed":
            response["error"] = task.error

        return response

    # --- lifecycle ---

    def start(self):
        """Start the worker and cleanup background threads."""
        self._running = True
        self._worker_thread = threading.Thread(
            target=self._worker_loop, daemon=True, name="ocr-worker"
        )
        self._worker_thread.start()

        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True, name="ocr-cleanup"
        )
        self._cleanup_thread.start()

    def stop(self):
        """Signal threads to stop (they are daemons — will die with process)."""
        self._running = False

    # --- internals ---

    def _worker_loop(self):
        """Single-threaded FIFO worker. Blocks on queue, processes one at a time."""
        while self._running:
            try:
                task_id = self._queue.get(timeout=1)
            except queue.Empty:
                continue

            with self._lock:
                task = self._tasks.get(task_id)
            if task is None:
                continue

            # Mark processing
            with self._lock:
                task.status = "processing"
            print(f"  [task:{task_id}] processing → {task.image_path.name}",
                  file=sys.stderr)

            try:
                result = run_ocr(
                    image_path=task.image_path,
                    work_dir=self._work_dir / task_id / "_ocr_work",
                    max_dim=1536,
                )
                with self._lock:
                    task.result = result
                    task.status = "done"
                    task.completed_at = time.time()
                print(f"  [task:{task_id}] done ({len(result['markdown']):,} chars)",
                      file=sys.stderr)
            except Exception as exc:
                with self._lock:
                    task.status = "failed"
                    task.error = str(exc)
                    task.completed_at = time.time()
                print(f"  [task:{task_id}] failed: {exc}", file=sys.stderr)

    def _cleanup_loop(self):
        """Periodically remove tasks whose results have expired."""
        import shutil
        while self._running:
            time.sleep(CLEANUP_INTERVAL_SEC)
            if not self._running:
                break

            now = time.time()
            expired: list[str] = []
            with self._lock:
                for tid, task in list(self._tasks.items()):
                    if task.completed_at is None:
                        continue  # pending/processing — never expire
                    if now - task.completed_at > RESULT_TTL_SEC:
                        expired.append(tid)

            for tid in expired:
                with self._lock:
                    task = self._tasks.pop(tid, None)
                if task:
                    # Clean up task directory from disk
                    task_dir = self._work_dir / tid
                    shutil.rmtree(task_dir, ignore_errors=True)
                    print(f"  [task:{tid}] expired and cleaned up", file=sys.stderr)
