#!/usr/bin/env python3
"""OCR Service — FastAPI app.

Endpoints:
    POST /tasks          — upload a page image (multipart), create OCR task.
    GET  /tasks/{task_id} — poll task status + get result when done.
"""

import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from task_manager import RESULT_TTL_SEC, TaskManager

load_dotenv()

# ---------------------------------------------------------------------------
# Work directory
# ---------------------------------------------------------------------------

WORK_DIR = Path(os.getenv("OCR_WORK_DIR", Path(__file__).parent / "temp"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

task_manager = TaskManager(work_dir=WORK_DIR)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    task_manager.start()
    print(f"OCR service started (work dir: {WORK_DIR})", file=sys.stderr)
    yield
    task_manager.stop()


app = FastAPI(title="OCR Service", version="1.0.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/tasks")
async def create_task(file: UploadFile = File(...)):
    """Submit a page image for OCR processing.

    Returns immediately with a task_id. Processing happens in the background.
    """
    # Validate file type
    if not file.filename:
        raise HTTPException(400, "Filename is required")

    suffix = Path(file.filename).suffix.lower()
    allowed = {".jpg", ".jpeg", ".png"}
    if suffix not in allowed:
        raise HTTPException(415, f"Unsupported file type: {suffix}. Allowed: {', '.join(allowed)}")

    # Convert PNG to JPEG if needed (pipeline is JPG-only)
    image_data = await file.read()
    if suffix == ".png":
        image_data = _convert_png_to_jpeg(image_data, file.filename)

    task_id = task_manager.create_task(image_data, file.filename)
    return JSONResponse({"task_id": task_id, "status": "pending"}, status_code=201)


@app.get("/tasks/{task_id}")
async def get_task(task_id: str):
    """Poll task status and get result when done.

    Returns:
        {task_id, status}                                    — pending / processing
        {task_id, status, result: {markdown, images: [...]}} — done
        {task_id, status, error}                             — failed
        404 if task not found (expired or never existed).
    """
    info = task_manager.get_task(task_id)
    if info is None:
        raise HTTPException(404, f"Task not found: {task_id}. "
                            f"Results are retained for {RESULT_TTL_SEC // 60} min after completion.")
    return info


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _convert_png_to_jpeg(image_data: bytes, filename: str) -> bytes:
    """Convert PNG to JPEG (in-memory). Pipeline only accepts JPG."""
    from io import BytesIO
    from PIL import Image

    img = Image.open(BytesIO(image_data))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=92)
    print(f"  Converted {filename} → JPEG ({len(image_data):,} → {buf.tell():,} bytes)",
          file=sys.stderr)
    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("OCR_PORT", "8001"))
    uvicorn.run(app, host="0.0.0.0", port=port)
