#!/usr/bin/env python3
"""Core OCR pipeline — stateless, no CLI, no labeling, no Imgur.

Port of parse_exam.py steps 0–4: downsample → PaddleOCR-VL inference →
extract markdown + images (as base64 for transport).
"""

import os
import base64
import mimetypes
import sys
import threading
from pathlib import Path

import paddle

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

# ---------------------------------------------------------------------------
# Pipeline pool — N independent PaddleOCR-VL instances, shared via semaphore.
# Each worker thread acquires a pipeline, uses it, then releases it back.
# This avoids thread-safety issues with a single instance while allowing
# N concurrent OCR operations.
# ---------------------------------------------------------------------------

_POOL_SIZE = int(os.getenv("OCR_WORKERS", "2"))


class PipelinePool:
    """Pool of N PaddleOCR-VL pipeline instances, guarded by a semaphore."""

    def __init__(self, size: int):
        self._sem = threading.Semaphore(size)
        self._pipelines: list = []
        self._lock = threading.Lock()

    def acquire(self):
        """Block until a pipeline is available, then return it.

        Pipelines are lazily created up to `size` instances and reused.
        """
        self._sem.acquire()
        with self._lock:
            if self._pipelines:
                return self._pipelines.pop()
        # No cached instance — create a new one (we hold a semaphore slot)
        from paddleocr import PaddleOCRVL
        print("Loading PaddleOCR-VL v1.6 model (enable_mkldnn=True)...", file=sys.stderr)
        pipeline = PaddleOCRVL(pipeline_version="v1.6", enable_mkldnn=True)
        print("Model loaded.", file=sys.stderr)
        return pipeline

    def release(self, pipeline):
        """Return a pipeline to the pool for reuse."""
        with self._lock:
            self._pipelines.append(pipeline)
        self._sem.release()


_pool = PipelinePool(_POOL_SIZE)


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def _downsample_image(src: Path, dst: Path, max_dim: int):
    """Resize image so longest edge ≤ max_dim, preserving aspect ratio."""
    from PIL import Image
    import shutil

    img = Image.open(src)
    w, h = img.size
    longest = max(w, h)
    if longest <= max_dim:
        shutil.copy2(src, dst)
        return
    scale = max_dim / longest
    new_size = (int(w * scale), int(h * scale))
    img = img.resize(new_size, Image.LANCZOS)
    img.save(dst, format="JPEG", quality=92)
    print(f"  Downsampled: {w}×{h} → {new_size[0]}×{new_size[1]}", file=sys.stderr)


def _guess_mime_type(filename: str) -> str:
    """Guess MIME type from filename extension. Falls back to image/jpeg."""
    mime, _ = mimetypes.guess_type(filename)
    if mime and mime.startswith("image/"):
        return mime
    return "image/jpeg"


# ---------------------------------------------------------------------------
# OCR runner
# ---------------------------------------------------------------------------

def run_ocr(image_path: Path, work_dir: Path, max_dim: int = 1536):
    """Run PaddleOCR-VL on a single page image.

    Args:
        image_path: Path to the JPG/PNG image file.
        work_dir: Scratch directory for intermediate output (auto-created).
        max_dim: Downsample longest edge to this size (0 = no resize).

    Returns:
        dict with keys:
            markdown (str): OCR output with images/〈filename〉 refs.
            images (list[dict]): [{filename, base64, mime_type}, ...].
    """
    import shutil

    work_dir.mkdir(parents=True, exist_ok=True)

    # ---- Step 0: Downsample if needed ----
    input_for_ocr = image_path
    resized_name = None  # track the resized filename to exclude from image list
    if max_dim > 0:
        resized = work_dir / f"resized_{image_path.name}"
        _downsample_image(image_path, resized, max_dim)
        input_for_ocr = resized
        resized_name = resized.name

    # ---- Step 1: PaddleOCR-VL inference ----
    pipeline = _pool.acquire()
    try:
        output = pipeline.predict(
            str(input_for_ocr),
            use_layout_detection=True,
            use_chart_recognition=True,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            format_block_content=True,
        )
        for res in output:
            res.save_to_markdown(save_path=str(work_dir))
    finally:
        _pool.release(pipeline)

    # ---- Step 2: Find the generated Markdown ----
    md_files = sorted(
        work_dir.glob("*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not md_files:
        raise RuntimeError("PaddleOCR-VL produced no Markdown output")
    md_content = md_files[0].read_text(encoding="utf-8")

    # ---- Step 3: Find extracted images in work dir ----
    # Exclude the downsampled input image (resized_<original>.jpg) — it is not
    # an extracted figure.
    work_images: list[Path] = []
    for f in work_dir.rglob("*"):
        if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS:
            if resized_name and f.name == resized_name:
                continue
            work_images.append(f)

    # ---- Step 3b: Normalize image ref paths in markdown ----
    # PaddleOCR-VL writes `<img src="imgs/...">` but the frontend expects
    # `images/...`.  Rewrite before returning.
    md_content = md_content.replace('src="imgs/', 'src="images/')
    md_content = md_content.replace("src='imgs/", "src='images/")
    md_content = md_content.replace("](imgs/", "](images/")

    # ---- Step 4: Base64-encode images for transport ----
    images_payload: list[dict] = []
    for img_file in work_images:
        b64 = base64.b64encode(img_file.read_bytes()).decode("utf-8")
        images_payload.append({
            "filename": img_file.name,
            "base64": b64,
            "mime_type": _guess_mime_type(img_file.name),
        })

    print(f"  Markdown: {len(md_content):,} chars, Images: {len(images_payload)}",
          file=sys.stderr)

    return {
        "markdown": md_content,
        "images": images_payload,
    }
