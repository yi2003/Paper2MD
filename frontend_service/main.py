#!/usr/bin/env python3
"""Frontend Service — FastAPI app.

User-facing web application for:
- Task creation and page upload
- OCR progress polling
- Mapping editor (figure → question labels)
- Self-contained Markdown download (Imgur-hosted images)
"""

import base64
import os
import re
import sys
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

import models
from task_manager import (
    OCR_SERVICE_URL,
    UPLOADS_DIR,
    _apply_labels_mapping,
    _extract_image_filenames,
    poll_loop,
)

load_dotenv()

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

TEMPLATES_DIR = Path(__file__).parent / "templates"

from jinja2 import Environment, FileSystemLoader

jinja_env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))


def _ts_to_date(ts: float) -> str:
    """Convert Unix timestamp to readable date string."""
    import datetime
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


jinja_env.filters["ts_to_date"] = _ts_to_date
jinja_env.filters["tojson"] = lambda v: __import__("json").dumps(v, ensure_ascii=False)


def render(template: str, **kwargs) -> HTMLResponse:
    """Render a Jinja2 template to an HTML response."""
    tmpl = jinja_env.get_template(template)
    return HTMLResponse(tmpl.render(**kwargs))


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start background poller
    poll_task = asyncio.create_task(poll_loop())
    print(f"Frontend service started (uploads: {UPLOADS_DIR})", file=sys.stderr)
    yield
    poll_task.cancel()
    await models.close_db()


app = FastAPI(title="Math Exam Parser — Frontend", version="1.0.0", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Image serving (for mapping editor preview)
# ---------------------------------------------------------------------------

# Images are served via a path-parameter route, not StaticFiles, because
# each task has its own images directory.
@app.get("/tasks/{task_id}/images/{filename}")
async def serve_image(task_id: str, filename: str):
    """Serve an extracted image for the mapping editor preview."""
    filepath = UPLOADS_DIR / task_id / "images" / filename
    if not filepath.exists():
        raise HTTPException(404, f"Image not found: {filename}")
    from fastapi.responses import FileResponse
    return FileResponse(str(filepath))


# ---------------------------------------------------------------------------
# Page endpoints
# ---------------------------------------------------------------------------

@app.post("/tasks")
async def create_task(pages: int = Form(...)):
    """Create a new frontend task. Returns the task_id."""
    if pages < 1 or pages > 50:
        raise HTTPException(400, "pages must be between 1 and 50")
    task_id = await models.create_task(pages)
    return JSONResponse({"task_id": task_id, "pages": pages}, status_code=201)


@app.post("/tasks/{task_id}/pages")
async def upload_page(
    task_id: str,
    index: int,
    file: UploadFile = File(...),
):
    """Upload a single page image. Forwards to OCR service immediately."""
    # Validate task exists
    task = await models.get_task(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")

    if index < 0 or index >= len(task["pages"]):
        raise HTTPException(400, f"index must be 0–{len(task['pages']) - 1}")

    page = task["pages"][index]
    if page["status"] not in ("awaiting_upload", "failed"):
        raise HTTPException(409, f"Page {index} already uploaded (status: {page['status']})")

    # Validate and possibly convert image
    suffix = Path(file.filename or "page.jpg").suffix.lower()
    allowed = {".jpg", ".jpeg", ".png"}
    if suffix not in allowed:
        raise HTTPException(415, f"Unsupported type: {suffix}")

    image_data = await file.read()
    if suffix == ".png":
        image_data = _convert_png_to_jpeg(image_data, file.filename or "page.png")

    # Save original for retry
    page_dir = UPLOADS_DIR / task_id
    page_dir.mkdir(parents=True, exist_ok=True)
    page_path = page_dir / f"page_{index}.jpg"
    page_path.write_bytes(image_data)

    # Forward to OCR service
    try:
        r = requests.post(
            f"{OCR_SERVICE_URL}/tasks",
            files={"file": (f"page_{index}.jpg", image_data, "image/jpeg")},
            timeout=30,
        )
        r.raise_for_status()
        ocr_data = r.json()
    except requests.RequestException as e:
        raise HTTPException(502, f"OCR service unavailable: {e}")

    ocr_task_id = ocr_data["task_id"]
    await models.register_page_ocr(task_id, index, ocr_task_id)

    return JSONResponse({
        "page_index": index,
        "ocr_task_id": ocr_task_id,
        "status": "pending",
    })


@app.post("/tasks/{task_id}/pages/{index}/retry")
async def retry_page(task_id: str, index: int):
    """Retry a failed page by re-submitting to OCR."""
    task = await models.get_task(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")

    page = task["pages"][index]
    if page["status"] != "failed":
        raise HTTPException(409, f"Page {index} is not failed (status: {page['status']})")

    # Re-upload the original page image
    image_path = UPLOADS_DIR / task_id / f"page_{index}.jpg"
    if not image_path.exists():
        raise HTTPException(400, "Original page image not found — please re-upload")

    with open(image_path, "rb") as f:
        image_data = f.read()

    try:
        r = requests.post(
            f"{OCR_SERVICE_URL}/tasks",
            files={"file": (f"page_{index}.jpg", image_data, "image/jpeg")},
            timeout=30,
        )
        r.raise_for_status()
        ocr_data = r.json()
    except requests.RequestException as e:
        raise HTTPException(502, f"OCR service unavailable: {e}")

    ocr_task_id = ocr_data["task_id"]
    await models.register_page_ocr(task_id, index, ocr_task_id)

    return JSONResponse({
        "page_index": index,
        "ocr_task_id": ocr_task_id,
        "status": "pending",
    })


# ---------------------------------------------------------------------------
# View endpoints (HTML pages)
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    return render("upload.html")


@app.get("/tasks", response_class=HTMLResponse)
async def task_list_page():
    """Task list — shows all tasks."""
    tasks = await models.get_all_tasks()
    return render("task_list.html", tasks=tasks)


@app.get("/tasks/{task_id}", response_class=HTMLResponse)
async def task_status_page(task_id: str):
    """Task status page — shows per-page progress with polling."""
    task = await models.get_task(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    return render("task_status.html", task=task)


@app.get("/tasks/{task_id}/edit", response_class=HTMLResponse)
async def edit_page(task_id: str):
    """Mapping editor — images shown under their questions with inline Q# inputs."""
    task = await models.get_task(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    if task["status"] != "done":
        raise HTTPException(400, "Task not yet completed")

    md = task["merged_markdown"] or ""
    filenames = _extract_image_filenames(md)
    return render("edit.html", task=task, filenames=filenames)


# ---------------------------------------------------------------------------
# Data endpoints (JSON)
# ---------------------------------------------------------------------------

@app.get("/tasks/{task_id}/status")
async def task_status_json(task_id: str):
    """JSON status endpoint for AJAX polling."""
    task = await models.get_task(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    return {
        "task_id": task["id"],
        "status": task["status"],
        "pages": [
            {
                "page_index": p["page_index"],
                "ocr_task_id": p["ocr_task_id"],
                "status": p["status"],
            }
            for p in task["pages"]
        ],
    }


@app.get("/tasks/{task_id}/markdown")
async def get_markdown(task_id: str):
    """Return the current merged markdown (for editor preview)."""
    task = await models.get_task(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    return {"markdown": task.get("merged_markdown") or ""}


@app.put("/tasks/{task_id}/mapping")
async def save_mapping(task_id: str, request: Request):
    """Apply a {filename: "QN"} mapping to the merged markdown.

    Body: JSON object mapping filename → question label.
    """
    task = await models.get_task(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")

    mapping = await request.json()
    md = task["merged_markdown"] or ""
    updated = _apply_labels_mapping(md, mapping)
    await models.update_task_markdown(task_id, updated)
    return {"markdown": updated}


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

@app.get("/tasks/{task_id}/download")
async def download(task_id: str):
    """Download self-contained Markdown with images uploaded to Imgur."""
    task = await models.get_task(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")

    md = task.get("merged_markdown") or ""
    md = _upload_images_to_imgur(task_id, md)

    return PlainTextResponse(
        md,
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="exam_{task_id}.md"'},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _convert_png_to_jpeg(image_data: bytes, filename: str) -> bytes:
    """Convert PNG to JPEG in-memory."""
    from PIL import Image

    img = Image.open(BytesIO(image_data))
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=92)
    buf.seek(0)
    print(f"  Converted {filename} → JPEG", file=sys.stderr)
    return buf.read()


# ---------------------------------------------------------------------------
# Imgur upload
# ---------------------------------------------------------------------------

_IMGUR_UPLOAD_URL = "https://api.imgur.com/3/image"
_ANON_CLIENT_ID = "546c25a59c58ad7"


def _upload_images_to_imgur(task_id: str, md_content: str) -> str:
    """Upload all referenced images to Imgur, replace local refs with URLs.

    Reads images from uploads/{task_id}/images/, uploads each to Imgur,
    then replaces images/<filename> refs in the markdown with the Imgur URLs.
    """
    images_dir = UPLOADS_DIR / task_id / "images"
    if not images_dir.is_dir():
        return md_content

    client_id = os.getenv("IMGUR_CLIENT_ID", "")
    if client_id in ("", "your_imgur_client_id_here", None):
        client_id = _ANON_CLIENT_ID

    # Find images referenced in the markdown
    refs = set()
    for m in re.finditer(r"""src=["']images/([^"']+)["']""", md_content):
        refs.add(m.group(1))
    for m in re.finditer(r"""!\[.*?\]\(images/([^)]+)\)""", md_content):
        refs.add(m.group(1))

    if not refs:
        return md_content

    print(f"  [download:{task_id}] uploading {len(refs)} images to Imgur...",
          file=sys.stderr)

    for filename in refs:
        filepath = images_dir / filename
        if not filepath.exists():
            print(f"    ✗ {filename} — not found", file=sys.stderr)
            continue

        b64 = base64.b64encode(filepath.read_bytes()).decode("utf-8")
        try:
            r = requests.post(
                _IMGUR_UPLOAD_URL,
                headers={"Authorization": f"Client-ID {client_id}"},
                data={"image": b64, "type": "base64"},
                timeout=60,
            )
            data = r.json()
            if data.get("success"):
                url = data["data"]["link"]
                md_content = md_content.replace(f"images/{filename}", url)
                print(f"    ✓ {filename} → {url}", file=sys.stderr)
            else:
                err = data.get("data", {}).get("error", r.text)
                print(f"    ✗ {filename} — {err}", file=sys.stderr)
        except requests.RequestException as e:
            print(f"    ✗ {filename} — {e}", file=sys.stderr)

    return md_content

import asyncio

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("FRONTEND_PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
