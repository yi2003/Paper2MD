#!/usr/bin/env python3
"""Frontend Service — FastAPI app.

User-facing web application for:
- Task creation and page upload
- OCR progress polling
- Mapping editor (figure → question labels)
- Self-contained Markdown download (Imgur-hosted images)
"""

import base64
import mimetypes
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
    normalize_markdown,
    poll_loop,
    remarge_task,
)

load_dotenv()

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

TEMPLATES_DIR = Path(__file__).parent / "templates"

from jinja2 import Environment, FileSystemLoader

jinja_env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), auto_reload=True)


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


# Disable browser caching during development
@app.middleware("http")
async def no_cache(request: Request, call_next):
    from fastapi.responses import Response
    response = await call_next(request)
    if "text/html" in response.headers.get("content-type", ""):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


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


@app.get("/tasks/{task_id}/pages/{page_index}/raw")
async def serve_raw_page(task_id: str, page_index: int):
    """Serve the original uploaded page image."""
    filepath = UPLOADS_DIR / task_id / f"page_{page_index}.jpg"
    if not filepath.exists():
        # Fallback: look in OCR service temp dir
        ocr_temp = Path(os.getenv("OCR_WORK_DIR", ""))
        if ocr_temp:
            # Find OCR task dirs for this frontend task's page
            page = await models.get_page(task_id, page_index)
            if page and page.get("ocr_task_id"):
                ocr_page = ocr_temp / page["ocr_task_id"] / "page.jpg"
                if ocr_page.exists():
                    from fastapi.responses import FileResponse
                    return FileResponse(str(ocr_page))
        raise HTTPException(404, "Raw page image not found")
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
    print(f"  Saved original: {page_path} ({len(image_data):,} bytes)", file=sys.stderr)
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
async def edit_page(task_id: str, page: int = 0):
    """Mapping editor — per-page, images shown under their questions."""
    task = await models.get_task(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")
    if task["status"] != "done":
        raise HTTPException(400, "Task not yet completed")

    pages_count = len(task["pages"])
    if page < 0 or page >= pages_count:
        raise HTTPException(404, f"Page {page} not found (task has {pages_count} pages)")

    page_data = task["pages"][page]
    if page_data["status"] != "done":
        raise HTTPException(400, f"Page {page} is not yet completed (status: {page_data['status']})")

    md = page_data.get("markdown") or ""
    filenames = _extract_image_filenames(md)
    return render("edit.html", task=task, filenames=filenames,
                  page_index=page, pages_count=pages_count)


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


# ---------------------------------------------------------------------------
# Per-page markdown & mapping
# ---------------------------------------------------------------------------

@app.get("/tasks/{task_id}/pages/{page_index}/markdown")
async def get_page_markdown(task_id: str, page_index: int):
    """Return the markdown + label mapping for a single page."""
    page = await models.get_page(task_id, page_index)
    if page is None or page["status"] != "done":
        raise HTTPException(404, "Page not found or not done")
    import json as _json
    mapping = {}
    if page.get("label_mapping"):
        try:
            mapping = _json.loads(page["label_mapping"])
        except _json.JSONDecodeError:
            pass
    md = normalize_markdown(page.get("markdown") or "")
    return {"markdown": md, "label_mapping": mapping}


@app.put("/tasks/{task_id}/pages/{page_index}/mapping")
async def save_page_mapping(task_id: str, page_index: int, request: Request):
    """Save per-page {filename: "QN"} mapping and re-merge.

    Body: JSON object mapping filename → question label.
    """
    page = await models.get_page(task_id, page_index)
    if page is None or page["status"] != "done":
        raise HTTPException(404, "Page not found or not done")

    mapping = await request.json()
    import json as _json
    mapping_json = _json.dumps(mapping, ensure_ascii=False)

    await models.save_page_mapping(task_id, page_index, mapping_json)

    # Apply mapping to this page and return the labelled markdown
    md = page.get("markdown") or ""
    labelled = _apply_labels_mapping(md, mapping)

    # Re-merge all pages with their mappings
    await remarge_task(task_id)

    return {"markdown": labelled}


# ---------------------------------------------------------------------------
# ImgBB upload helpers
# ---------------------------------------------------------------------------

_IMGBB_UPLOAD_URL = "https://api.imgbb.com/1/upload"


def _upload_images_for_markdown(task_id: str, md_content: str, images_dir: Path) -> str:
    """Upload images referenced in markdown to ImgBB, replace local refs.

    Reads images from `images_dir`, uploads each to ImgBB, replaces
    images/<filename> refs with ImgBB URLs, and saves mappings to DB.
    """
    md_content = normalize_markdown(md_content)

    if not images_dir.is_dir():
        return md_content

    api_key = os.getenv("IMGBB_API_KEY", "")
    if not api_key:
        print("  [download] IMGBB_API_KEY not set — using base64 inline",
              file=sys.stderr)
        return _inline_all_images(task_id, md_content, images_dir)

    refs: set[str] = set()
    for m in re.finditer(r"""src=["']images/([^"']+)["']""", md_content):
        refs.add(m.group(1))
    for m in re.finditer(r"""!\[.*?\]\(images/([^)]+)\)""", md_content):
        refs.add(m.group(1))

    if not refs:
        return md_content

    print(f"  [download] uploading {len(refs)} images to ImgBB...",
          file=sys.stderr)

    import asyncio

    for filename in refs:
        # 1. Check if we already have a cached URL for this image
        cached_url = models.get_image_url_sync(task_id, filename)
        if cached_url:
            md_content = md_content.replace(f"images/{filename}", cached_url)
            print(f"    ↻ {filename} → {cached_url} (cached)", file=sys.stderr)
            continue

        filepath = images_dir / filename
        if not filepath.exists():
            print(f"    ✗ {filename} — not found", file=sys.stderr)
            continue

        b64 = base64.b64encode(filepath.read_bytes()).decode("utf-8")
        mime, _ = mimetypes.guess_type(filename)
        if not mime or not mime.startswith("image/"):
            mime = "image/jpeg"

        # 2. Try ImgBB upload
        url = None
        try:
            r = requests.post(
                _IMGBB_UPLOAD_URL,
                data={"key": api_key, "image": b64},
                timeout=60,
            )
            resp_data = r.json()
            if resp_data.get("success"):
                url = resp_data["data"]["url"]
                md_content = md_content.replace(f"images/{filename}", url)
                print(f"    ✓ {filename} → {url}", file=sys.stderr)

                # Save mapping to DB
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        asyncio.ensure_future(
                            models.save_image_mapping(task_id, filename, url, b64)
                        )
                    else:
                        asyncio.run(
                            models.save_image_mapping(task_id, filename, url, b64)
                        )
                except Exception as e:
                    print(f"    ⚠ DB save failed for {filename}: {e}", file=sys.stderr)
            else:
                err = resp_data.get("error", {}).get("message", r.text)
                print(f"    ✗ ImgBB: {filename} — {err}", file=sys.stderr)
        except requests.RequestException as e:
            print(f"    ✗ ImgBB: {filename} — {e}", file=sys.stderr)

        # 3. Fallback: inline as base64 if ImgBB failed
        if url is None:
            data_uri = f"data:{mime};base64,{b64}"
            md_content = md_content.replace(f"images/{filename}", data_uri)
            print(f"    ⚡ {filename} → base64 inline (ImgBB failed)", file=sys.stderr)

    return md_content


def _inline_all_images(task_id: str, md_content: str, images_dir: Path) -> str:
    """Fallback: inline all image refs as base64 data URIs (no external upload)."""
    refs: set[str] = set()
    for m in re.finditer(r"""src=["']images/([^"']+)["']""", md_content):
        refs.add(m.group(1))
    for m in re.finditer(r"""!\[.*?\]\(images/([^)]+)\)""", md_content):
        refs.add(m.group(1))

    for filename in refs:
        filepath = images_dir / filename
        if not filepath.exists():
            continue
        b64 = base64.b64encode(filepath.read_bytes()).decode("utf-8")
        mime, _ = mimetypes.guess_type(filename)
        if not mime or not mime.startswith("image/"):
            mime = "image/jpeg"
        data_uri = f"data:{mime};base64,{b64}"
        md_content = md_content.replace(f"images/{filename}", data_uri)

    return md_content


# ---------------------------------------------------------------------------
# Per-page download
# ---------------------------------------------------------------------------


@app.get("/tasks/{task_id}/pages/{page_index}/download")
async def download_page(task_id: str, page_index: int):
    """Download a single page's Markdown with Imgur-hosted images."""
    page = await models.get_page(task_id, page_index)
    if page is None or page["status"] != "done":
        raise HTTPException(404, "Page not found or not done")

    md = page.get("markdown") or ""

    # Apply per-page label mapping if one exists
    import json as _json
    raw = page.get("label_mapping")
    if raw:
        try:
            mapping = _json.loads(raw)
            from task_manager import _apply_labels_mapping
            md = _apply_labels_mapping(md, mapping)
        except _json.JSONDecodeError:
            pass

    images_dir = UPLOADS_DIR / task_id / "images"
    md = _upload_images_for_markdown(task_id, md, images_dir)

    return PlainTextResponse(
        md,
        media_type="text/markdown",
        headers={"Content-Disposition":
                 f'attachment; filename="exam_{task_id}_page{page_index + 1}.md"'},
    )


# ---------------------------------------------------------------------------
# Download (full task)
# ---------------------------------------------------------------------------

@app.get("/tasks/{task_id}/download")
async def download(task_id: str):
    """Download self-contained Markdown with Imgur-hosted images."""
    task = await models.get_task(task_id)
    if task is None:
        raise HTTPException(404, "Task not found")

    # Re-merge to apply any per-page label mappings
    merged = await remarge_task(task_id)
    md = merged if merged else (task.get("merged_markdown") or "")

    images_dir = UPLOADS_DIR / task_id / "images"
    md = _upload_images_for_markdown(task_id, md, images_dir)

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


import asyncio

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("FRONTEND_PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
