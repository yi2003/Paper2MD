#!/usr/bin/env python3
"""Frontend task orchestration — polling, merging, mapping, base64 inlining."""

import asyncio
import base64
import mimetypes
import os
import re
import sys
from pathlib import Path

import requests

import models

# ---------------------------------------------------------------------------
# Constants (from parse_exam.py — reused as-is)
# ---------------------------------------------------------------------------

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
IMAGE_REF_RE = re.compile(r"!\[.*?\]\((.*?)\)")
IMG_SRC_RE = re.compile(r'<img[^>]+src="([^"]+)"')

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

OCR_SERVICE_URL = os.getenv("OCR_SERVICE_URL", "http://localhost:8001")
POLL_INTERVAL_SEC = int(os.getenv("POLL_INTERVAL_SEC", "2"))
UPLOADS_DIR = Path(os.getenv("UPLOADS_DIR", Path(__file__).parent / "uploads"))

# ---------------------------------------------------------------------------
# Image ref helpers (reused as-is from parse_exam.py)
# ---------------------------------------------------------------------------

def _find_all_image_refs(md_content: str) -> list[str]:
    """Extract all image paths from a Markdown string."""
    refs = IMAGE_REF_RE.findall(md_content)
    refs += IMG_SRC_RE.findall(md_content)
    return refs


def _extract_image_filenames(md_content: str) -> list[str]:
    """Return ordered list of image filenames referenced in the markdown."""
    names: list[str] = []
    seen = set()
    for m in re.finditer(
        r"""src=["'](?:[^"']*/)?([^/"']+\.(?:jpg|jpeg|png|gif|webp|bmp))["']""",
        md_content,
        re.IGNORECASE,
    ):
        name = m.group(1)
        if name not in seen:
            names.append(name)
            seen.add(name)
    for m in re.finditer(
        r"""!\[.*?\]\((?:[^)]*/)?([^/)]+\.(?:jpg|jpeg|png|gif|webp|bmp))\)""",
        md_content,
        re.IGNORECASE,
    ):
        name = m.group(1)
        if name not in seen:
            names.append(name)
            seen.add(name)
    return names


def _apply_labels_mapping(md_content: str, mapping: dict[str, str]) -> str:
    """Apply a user-provided {filename: 'Q<N>'} mapping.

    * Removes all image references from their original positions.
    * Inserts each image (with a ``*[Q<N> 附图]*`` label) right after the
      corresponding question text in the markdown.
    * Unassigned images are appended at the end.
    """
    # 1. Remove existing labels
    md_content = re.sub(r"\*\[Q\d+\s*附图\]\*\s*\n*", "", md_content)

    # 2. Extract all image references and remove them from the content.
    img_refs: list[tuple[str, str]] = []  # (filename, full_html_or_md)

    def _extract_and_remove(content: str) -> str:
        # <img src="images/...">  —  PaddleOCR-VL primary format
        for m in re.finditer(
            r'<img\s[^>]*?src=["\']([^"\']+)["\'][^>]*?>',
            content, re.IGNORECASE,
        ):
            fn = Path(m.group(1)).name
            img_refs.append((fn, m.group(0)))
        content = re.sub(r'<img\s[^>]*?>', '', content, flags=re.IGNORECASE)

        # Clean up <div> wrappers left empty after image removal
        content = re.sub(r'<div[^>]*?>\s*</div>', '', content, flags=re.IGNORECASE)

        # ![](images/...)  —  secondary format
        for m in re.finditer(r'!\[.*?\]\(([^)]+)\)', content):
            fn = Path(m.group(1)).name
            img_refs.append((fn, m.group(0)))
        content = re.sub(r'!\[.*?\]\([^)]+\)', '', content)

        return content

    md_content = _extract_and_remove(md_content)

    if not img_refs:
        return md_content

    # 3. Find question-header positions in the cleaned content.
    q_boundaries: list[tuple[int, str, int]] = []  # (start, num_str, end)
    for m in re.finditer(r'^(\d{1,2})\.\s', md_content, re.MULTILINE):
        q_boundaries.append((m.start(), m.group(1), 0))  # end filled below

    # 4. Group images by assigned question.
    q_images: dict[str, list[tuple[str, str]]] = {}  # num_str → [(fn, ref), …]
    unassigned: list[tuple[str, str]] = []
    for fn, ref in img_refs:
        q_val = mapping.get(fn, "").strip()
        if q_val:
            if not q_val.startswith("Q"):
                q_val = f"Q{q_val}"
            q_num = q_val[1:]  # strip "Q"
            q_images.setdefault(q_num, []).append((fn, ref))
        else:
            unassigned.append((fn, ref))

    # 5. Rebuild: walk through question blocks, insert assigned images after each.
    if not q_boundaries:
        # No questions detected — just append everything at the end with labels.
        result = [md_content]
        for q_num, imgs in q_images.items():
            for fn, ref in imgs:
                result.append(f"\n*[Q{q_num} 附图]*\n\n{ref}\n")
        for fn, ref in unassigned:
            result.append(f"\n{ref}\n")
        return "".join(result)

    result: list[str] = []
    last_end = 0

    for i, (q_start, q_num) in enumerate(
        [(b[0], b[1]) for b in q_boundaries]
    ):
        if i + 1 < len(q_boundaries):
            q_end = q_boundaries[i + 1][0]
        else:
            q_end = len(md_content)

        # Content from last cut to end of this question block
        result.append(md_content[last_end:q_end])

        # Insert images assigned to this question
        if q_num in q_images:
            for fn, ref in q_images.pop(q_num):
                result.append(f"\n\n*[Q{q_num} 附图]*\n\n{ref}\n")

        last_end = q_end

    # Trailing content after the last question
    result.append(md_content[last_end:])

    # Remaining assigned images (for questions not found in text)
    for q_num, imgs in q_images.items():
        for fn, ref in imgs:
            result.append(f"\n\n*[Q{q_num} 附图]*\n\n{ref}\n")

    # Unassigned images at the end
    if unassigned:
        result.append("\n\n")
        for fn, ref in unassigned:
            result.append(f"{ref}\n")

    # Normalise excessive blank lines
    final = "".join(result)
    final = re.sub(r"\n{4,}", "\n\n\n", final)
    return final


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------

async def poll_loop():
    """Background asyncio task: poll OCR service for all active tasks."""
    print("Polling loop started", file=sys.stderr)
    while True:
        try:
            active_tasks = await models.get_active_tasks()
            for task in active_tasks:
                task_id = task["id"]
                pending_pages = await models.get_pending_pages(task_id)

                all_done = True
                any_failed = False

                for page in pending_pages:
                    ocr_id = page["ocr_task_id"]
                    if not ocr_id:
                        all_done = False
                        continue

                    try:
                        r = requests.get(
                            f"{OCR_SERVICE_URL}/tasks/{ocr_id}", timeout=10
                        )
                        r.raise_for_status()
                        ocr_data = r.json()
                    except requests.RequestException as e:
                        print(f"  Poll error {ocr_id}: {e}", file=sys.stderr)
                        all_done = False
                        continue

                    ocr_status = ocr_data["status"]

                    if ocr_status == "done":
                        # Fetch result, store images, save markdown
                        result = ocr_data["result"]
                        images_dir = UPLOADS_DIR / task_id / "images"
                        images_dir.mkdir(parents=True, exist_ok=True)

                        for img in result["images"]:
                            img_path = images_dir / img["filename"]
                            img_path.write_bytes(base64.b64decode(img["base64"]))

                        await models.update_page_result(
                            task_id=task_id,
                            page_index=page["page_index"],
                            status="done",
                            markdown=result["markdown"],
                        )
                        print(f"  [{task_id}] page {page['page_index']} done",
                              file=sys.stderr)

                    elif ocr_status == "failed":
                        await models.update_page_result(
                            task_id=task_id,
                            page_index=page["page_index"],
                            status="failed",
                        )
                        any_failed = True
                        print(f"  [{task_id}] page {page['page_index']} failed: "
                              f"{ocr_data.get('error', 'unknown')}", file=sys.stderr)

                    else:
                        # pending or processing — keep waiting
                        all_done = False

                # Check if all pages are terminal
                task_full = await models.get_task(task_id)
                if not task_full:
                    continue

                pages = task_full["pages"]
                if all(p["status"] == "done" for p in pages):
                    # Merge markdowns
                    merged = _merge_markdowns(pages)
                    await models.update_task_status(task_id, "done", merged)
                    print(f"  [{task_id}] all done → merged {len(merged)} chars",
                          file=sys.stderr)
                elif any(
                    p["status"] in ("failed",) and not _can_retry(p)
                    for p in pages
                ):
                    # At least one page failed and hasn't been retried
                    pass  # keep task 'processing', let user retry

        except Exception as e:
            print(f"Poll loop error: {e}", file=sys.stderr)

        await asyncio.sleep(POLL_INTERVAL_SEC)


def _can_retry(page: dict) -> bool:
    """A failed page can be retried (user action)."""
    return page["status"] == "failed"


# ---------------------------------------------------------------------------
# Markdown merging
# ---------------------------------------------------------------------------

def _merge_markdowns(pages: list[dict]) -> str:
    """Concatenate page markdowns with page boundary markers."""
    parts: list[str] = []
    for i, page in enumerate(pages):
        md = page.get("markdown") or ""
        n = i + 1
        if page["status"] == "failed":
            parts.append(f"<!-- page {n} FAILED -->\n\n")
        else:
            parts.append(f"<!-- page {n} -->\n\n{md}\n\n<!-- /page {n} -->\n\n")
    return "".join(parts)


# ---------------------------------------------------------------------------
# Base64 inlining (download step)
# ---------------------------------------------------------------------------

def inline_images(task_id: str, md_content: str) -> str:
    """Replace images/<filename> refs with base64 data URIs.

    Reads image files from uploads/{task_id}/images/.
    """
    images_dir = UPLOADS_DIR / task_id / "images"
    refs = _find_all_image_refs(md_content)

    for ref in refs:
        filename = Path(ref).name
        filepath = images_dir / filename
        if not filepath.exists():
            print(f"  [download] image not found: {filename}", file=sys.stderr)
            continue

        mime, _ = mimetypes.guess_type(filename)
        if not mime or not mime.startswith("image/"):
            mime = "image/jpeg"

        b64 = base64.b64encode(filepath.read_bytes()).decode("utf-8")
        data_uri = f"data:{mime};base64,{b64}"
        md_content = md_content.replace(ref, data_uri)

    return md_content
