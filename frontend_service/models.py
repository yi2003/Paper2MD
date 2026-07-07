#!/usr/bin/env python3
"""SQLite database layer — async (aiosqlite)."""

import sqlite3
import time
import uuid
from pathlib import Path

import aiosqlite

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    merged_markdown TEXT
);

CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    page_index INTEGER NOT NULL,
    ocr_task_id TEXT,
    status TEXT NOT NULL DEFAULT 'awaiting_upload',
    markdown TEXT,
    label_mapping TEXT
);

CREATE TABLE IF NOT EXISTS image_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    filename TEXT NOT NULL,
    imgur_url TEXT,
    base64 TEXT,
    created_at REAL NOT NULL
);
"""

MIGRATIONS = [
    # Add label_mapping column to existing pages tables
    "ALTER TABLE pages ADD COLUMN label_mapping TEXT",
]


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

_db: aiosqlite.Connection | None = None


async def get_db() -> aiosqlite.Connection:
    """Return the shared async connection (initialized on first call)."""
    global _db
    if _db is None:
        db_path = Path(__file__).parent / "data.db"
        _db = await aiosqlite.connect(str(db_path))
        _db.row_factory = aiosqlite.Row
        await _db.executescript(SCHEMA)
        # Run migrations (ignore errors for columns that already exist)
        for stmt in MIGRATIONS:
            try:
                await _db.execute(stmt)
            except aiosqlite.OperationalError:
                pass
        await _db.commit()
    return _db


async def close_db():
    """Close the database connection (called at shutdown)."""
    global _db
    if _db:
        await _db.close()
        _db = None


# ---------------------------------------------------------------------------
# Task CRUD
# ---------------------------------------------------------------------------

async def create_task() -> str:
    """Create a frontend task (pages are added dynamically on upload). Returns task_id."""
    db = await get_db()
    task_id = f"ft-{uuid.uuid4().hex[:12]}"
    now = time.time()

    await db.execute(
        "INSERT INTO tasks (id, status, created_at) VALUES (?, 'pending', ?)",
        (task_id, now),
    )
    await db.commit()
    return task_id


async def add_page(task_id: str, page_index: int) -> None:
    """Add a page slot to an existing task."""
    db = await get_db()
    await db.execute(
        "INSERT INTO pages (task_id, page_index, status) VALUES (?, ?, 'awaiting_upload')",
        (task_id, page_index),
    )
    await db.commit()


async def register_page_ocr(task_id: str, page_index: int, ocr_task_id: str):
    """Record the OCR task_id for a page and mark it pending."""
    db = await get_db()
    await db.execute(
        "UPDATE pages SET ocr_task_id = ?, status = 'pending' WHERE task_id = ? AND page_index = ?",
        (ocr_task_id, task_id, page_index),
    )
    await db.execute(
        "UPDATE tasks SET status = 'processing' WHERE id = ? AND status = 'pending'",
        (task_id,),
    )
    await db.commit()


async def update_page_result(page_index: int, task_id: str, status: str, markdown: str | None = None):
    """Update a page's status and optionally its markdown."""
    db = await get_db()
    if markdown is not None:
        await db.execute(
            "UPDATE pages SET status = ?, markdown = ? WHERE task_id = ? AND page_index = ?",
            (status, markdown, task_id, page_index),
        )
    else:
        await db.execute(
            "UPDATE pages SET status = ? WHERE task_id = ? AND page_index = ?",
            (status, task_id, page_index),
        )
    await db.commit()


async def update_task_status(task_id: str, status: str, merged_markdown: str | None = None):
    """Update task status and optionally the merged markdown."""
    db = await get_db()
    if merged_markdown is not None:
        await db.execute(
            "UPDATE tasks SET status = ?, merged_markdown = ? WHERE id = ?",
            (status, merged_markdown, task_id),
        )
    else:
        await db.execute(
            "UPDATE tasks SET status = ? WHERE id = ?",
            (status, task_id),
        )
    await db.commit()


async def update_task_markdown(task_id: str, merged_markdown: str):
    """Update only the merged_markdown (auto-save)."""
    db = await get_db()
    await db.execute(
        "UPDATE tasks SET merged_markdown = ? WHERE id = ?",
        (merged_markdown, task_id),
    )
    await db.commit()


async def get_task(task_id: str) -> dict | None:
    """Get a task with all its pages."""
    db = await get_db()
    task_row = await db.execute_fetchall(
        "SELECT * FROM tasks WHERE id = ?", (task_id,)
    )
    if not task_row:
        return None
    task = dict(task_row[0])

    page_rows = await db.execute_fetchall(
        "SELECT * FROM pages WHERE task_id = ? ORDER BY page_index",
        (task_id,),
    )
    task["pages"] = [dict(p) for p in page_rows]
    return task


async def get_all_tasks() -> list[dict]:
    """Get all tasks (without pages, for list view)."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, status, created_at FROM tasks ORDER BY created_at DESC"
    )
    return [dict(r) for r in rows]


async def get_active_tasks() -> list[dict]:
    """Get tasks that are still in progress (need polling)."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id FROM tasks WHERE status IN ('pending', 'processing')"
    )
    return [dict(r) for r in rows]


async def get_pending_pages(task_id: str) -> list[dict]:
    """Get pages that still need polling for a given task."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM pages WHERE task_id = ? AND status IN ('pending', 'processing')",
        (task_id,),
    )
    return [dict(r) for r in rows]


# --- Per-page mapping ---

async def get_page(task_id: str, page_index: int) -> dict | None:
    """Get a single page by task_id and page_index."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM pages WHERE task_id = ? AND page_index = ?",
        (task_id, page_index),
    )
    if not rows:
        return None
    return dict(rows[0])


async def save_page_mapping(task_id: str, page_index: int, mapping_json: str):
    """Persist the label mapping JSON for a single page."""
    db = await get_db()
    await db.execute(
        "UPDATE pages SET label_mapping = ? WHERE task_id = ? AND page_index = ?",
        (mapping_json, task_id, page_index),
    )
    await db.commit()


async def get_all_page_markdowns(task_id: str) -> list[dict]:
    """Get all completed pages with their markdown and label_mapping."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT page_index, markdown, label_mapping FROM pages "
        "WHERE task_id = ? AND status = 'done' ORDER BY page_index",
        (task_id,),
    )
    return [dict(r) for r in rows]


# --- Image mappings (Imgur URL + base64) ---

async def save_image_mapping(task_id: str, filename: str, imgur_url: str, base64_data: str):
    """Save an Imgur URL ↔ base64 mapping for an uploaded image."""
    db = await get_db()
    await db.execute(
        "INSERT INTO image_mappings (task_id, filename, imgur_url, base64, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (task_id, filename, imgur_url, base64_data, time.time()),
    )
    await db.commit()


async def get_image_mappings(task_id: str) -> list[dict]:
    """Get all image mappings for a task."""
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT id, task_id, filename, imgur_url, created_at FROM image_mappings "
        "WHERE task_id = ? ORDER BY filename",
        (task_id,),
    )
    return [dict(r) for r in rows]


def get_image_url_sync(task_id: str, filename: str) -> str | None:
    """Sync lookup: return cached Imgur URL for a task+filename, or None."""
    db_path = Path(__file__).parent / "data.db"
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT imgur_url FROM image_mappings "
            "WHERE task_id = ? AND filename = ? ORDER BY id DESC LIMIT 1",
            (task_id, filename),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()
