#!/usr/bin/env python3
"""PaddleOCR-VL Math Exam Parser — CLI.

Parses a single JPG exam paper image into Markdown with LaTeX formulas.
Extracts embedded figures and optionally uploads them to Imgur.

Usage:
    python parse_exam.py input.jpg
    python parse_exam.py input.jpg -o result.md
    python parse_exam.py input.jpg --upload                    # auto-upload to Imgur
    python parse_exam.py input.jpg --upload --no-keep-local    # upload + discard local images
    python parse_exam.py input.jpg --max-dim 1536 --cpu-threads 4
"""

import argparse
import base64
import json
import os
import re
import shutil
import sys
import threading
from pathlib import Path

import paddle
import requests
from dotenv import load_dotenv

load_dotenv()

# oneDNN is auto-detected by PaddlePaddle; FLAGS_use_mkldnn crashes
# PaddleOCR-VL due to missing pir::ArrayAttribute support
# paddle.set_flags({'FLAGS_use_mkldnn': True})

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
IMAGE_REF_RE = re.compile(r"!\[.*?\]\((.*?)\)")
IMG_SRC_RE = re.compile(r'<img[^>]+src="([^"]+)"')
IMGUR_UPLOAD_URL = "https://api.imgur.com/3/image"
# Imgur's public web client ID for anonymous uploads (rate-limited: ~50/hour)
ANON_CLIENT_ID = "546c25a59c58ad7"

# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

_pipeline = None
_pipeline_lock = threading.Lock()


def get_pipeline():
    """Lazy-load the PaddleOCR-VL pipeline (thread-safe, singleton)."""
    global _pipeline
    if _pipeline is None:
        with _pipeline_lock:
            if _pipeline is None:
                from paddleocr import PaddleOCRVL
                print("Loading PaddleOCR-VL v1.6 model...", file=sys.stderr)
                _pipeline = PaddleOCRVL(pipeline_version="v1.6")
                print("Model loaded.", file=sys.stderr)
    return _pipeline


def _find_all_image_refs(md_content: str) -> list[str]:
    """Extract all image paths from a Markdown string."""
    refs = IMAGE_REF_RE.findall(md_content)
    refs += IMG_SRC_RE.findall(md_content)
    return refs


def _downsample_image(src: Path, dst: Path, max_dim: int):
    """Resize image so longest edge ≤ max_dim, preserving aspect ratio."""
    from PIL import Image
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


def _extract_image_filenames(md_content: str) -> list[str]:
    """Return ordered list of image filenames referenced in the markdown."""
    import re as _re
    names: list[str] = []
    seen = set()
    # <img src=".../filename.jpg" or ![alt](.../filename.jpg)
    for m in _re.finditer(r"""src=["'](?:[^"']*/)?([^/"']+\.(?:jpg|jpeg|png|gif|webp|bmp))["']""", md_content, _re.IGNORECASE):
        name = m.group(1)
        if name not in seen:
            names.append(name)
            seen.add(name)
    for m in _re.finditer(r"""!\[.*?\]\((?:[^)]*/)?([^/)]+\.(?:jpg|jpeg|png|gif|webp|bmp))\)""", md_content, _re.IGNORECASE):
        name = m.group(1)
        if name not in seen:
            names.append(name)
            seen.add(name)
    return names


def _auto_label_images(md_content: str) -> tuple[str, dict[str, str]]:
    """Auto-detect question→image mapping, prepend labels, and return mapping.

    Returns (labeled_md, {filename: 'Q<N>'}).
    """
    import re as _re

    # Match question headers: "13.", "13．", "13,", "13、" at start of line
    q_pattern = _re.compile(r"^(\d{1,2})[.．,、]\s", _re.MULTILINE)
    img_pattern = _re.compile(r"(<img\s|[!]\[)")

    questions: list[tuple[int, str]] = []  # (char_pos, number)
    for m in q_pattern.finditer(md_content):
        questions.append((m.start(), m.group(1)))

    if not questions:
        return md_content, {}

    # Collect image files in order
    filenames = _extract_image_filenames(md_content)
    filename_to_q: dict[str, str] = {}

    qi = 0
    for m in img_pattern.finditer(md_content):
        pos = m.start()
        while qi + 1 < len(questions) and questions[qi + 1][0] < pos:
            qi += 1
        if questions[qi][0] < pos:
            q_num = questions[qi][1]

    # Second pass: build filename→Q mapping by position
    replacements: list[tuple[int, int, str]] = []
    qi = 0
    fi = 0
    for m in img_pattern.finditer(md_content):
        pos = m.start()
        while qi + 1 < len(questions) and questions[qi + 1][0] < pos:
            qi += 1
        if questions[qi][0] < pos:
            q_num = questions[qi][1]
            label = f"*[Q{q_num} 附图]*\n\n"
            replacements.append((pos, pos, label))
            if fi < len(filenames):
                filename_to_q[filenames[fi]] = f"Q{q_num}"
                fi += 1

    # Apply replacements in reverse order
    result = list(md_content)
    for start, end, label in reversed(replacements):
        result[start:end] = label + (md_content[start:end] if start == end else "")

    return "".join(result), filename_to_q


def _apply_labels_mapping(md_content: str, mapping: dict[str, str]) -> str:
    """Apply a user-provided {filename: 'Q<N>'} mapping, adding labels before images."""
    import re as _re

    img_pattern = _re.compile(r"""(<img\s[^>]*src=["'](?:[^"']*/)?({name})["'][^>]*>)""")
    md_pattern = _re.compile(r"""(!\[.*?\]\((?:[^)]*/)?({name})\))""")

    filenames = _extract_image_filenames(md_content)
    if not filenames:
        return md_content

    # Remove any existing auto-labels
    md_content = _re.sub(r"\*\[Q\d+\s*附图\]\*\s*\n*", "", md_content)

    # Sort filenames by their position in md_content
    positions: list[tuple[int, str]] = []
    for fn in filenames:
        # Find the img tag containing this filename
        for m in _re.finditer(rf"""(<img\s[^>]*src=["'][^"']*{_re.escape(fn)}["'][^>]*>)""", md_content):
            positions.append((m.start(), fn))
            break
        else:
            for m in _re.finditer(rf"""(!\[.*?\]\([^)]*{_re.escape(fn)}\))""", md_content):
                positions.append((m.start(), fn))
                break

    positions.sort(key=lambda x: x[0])

    # Build replacements in reverse order
    replacements: list[tuple[int, int, str]] = []
    for pos, fn in positions:
        q = mapping.get(fn, "")
        if q:
            # Normalize to Q<N> format
            q = q.strip()
            if not q.startswith("Q"):
                q = f"Q{q}"
            label = f"*[{q} 附图]*\n\n"
            replacements.append((pos, pos, label))

    # Apply in reverse
    result = list(md_content)
    for start, end, label in reversed(replacements):
        result[start:end] = label + (md_content[start:end] if start == end else "")

    return "".join(result)


def _upload_images_to_imgur(images_dir: Path, md_content: str) -> tuple[str, dict]:
    """Upload all images to Imgur anonymously, replace local refs with URLs.

    Returns (updated_md_content, {filename: url}).
    """
    images = sorted([
        f for f in images_dir.iterdir()
        if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS
    ])
    if not images:
        return md_content, {}

    client_id = os.getenv("IMGUR_CLIENT_ID", "")
    if client_id in ("", "your_imgur_client_id_here", None):
        client_id = ANON_CLIENT_ID

    print(f"  Uploading {len(images)} images to Imgur...", file=sys.stderr)
    name_to_url: dict[str, str] = {}

    for i, img_path in enumerate(images, 1):
        with open(img_path, "rb") as f:
            b64_data = base64.b64encode(f.read()).decode("utf-8")

        resp = requests.post(
            IMGUR_UPLOAD_URL,
            headers={"Authorization": f"Client-ID {client_id}"},
            data={"image": b64_data, "type": "base64"},
            timeout=60,
        )
        data = resp.json()
        if data.get("success"):
            url = data["data"]["link"]
            name_to_url[img_path.name] = url
            print(f"    [{i}/{len(images)}] {img_path.name} → {url}", file=sys.stderr)
        else:
            err = data.get("data", {}).get("error", resp.text)
            print(f"    [{i}/{len(images)}] {img_path.name} ✗ {err}", file=sys.stderr)

    # Replace local refs with Imgur URLs
    for filename, url in name_to_url.items():
        md_content = md_content.replace(f"images/{filename}", url)
        md_content = md_content.replace(filename, url)

    return md_content, name_to_url


def parse_exam(
    image_path: Path,
    output_dir: Path,
    max_dim: int = 1536,
    upload: bool = False,
    keep_local_images: bool = True,
    label_images: bool = False,
    labels_file: str | None = None,
    save_labels: str | None = None,
) -> str:
    """Parse a single JPG. Returns the Markdown string.

    Output structure:
        output_dir/
            result.md       (final Markdown with local or Imgur image refs)
            images/          (extracted figures, unless keep_local_images=False)
    """

    if not image_path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")
    if image_path.suffix.lower() not in (".jpg", ".jpeg"):
        raise ValueError(f"Only JPG files supported, got: {image_path.suffix}")

    output_dir.mkdir(parents=True, exist_ok=True)
    images_dir = output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    work_dir = output_dir / "_work"
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        # ---- Step 0: Downsample if needed ----
        input_for_ocr = image_path
        if max_dim > 0:
            resized = work_dir / f"resized_{image_path.name}"
            _downsample_image(image_path, resized, max_dim)
            input_for_ocr = resized

        # ---- Step 1: Inference ----
        print(f"Parsing: {image_path.name}", file=sys.stderr)
        pipeline = get_pipeline()
        with _pipeline_lock:
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

        # ---- Step 2: Find the generated Markdown ----
        md_files = sorted(work_dir.glob("*.md"),
                          key=lambda p: p.stat().st_mtime, reverse=True)
        if not md_files:
            raise RuntimeError("PaddleOCR-VL produced no Markdown output")
        md_content = md_files[0].read_text(encoding="utf-8")
        print(f"  Markdown: {len(md_content):,} chars", file=sys.stderr)

        # ---- Step 3: Find all extracted images in work dir ----
        work_images: list[Path] = []
        for f in work_dir.rglob("*"):
            if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS:
                work_images.append(f)
        print(f"  Images extracted: {len(work_images)}", file=sys.stderr)

        # ---- Step 4: Match Markdown refs to image files ----
        refs = _find_all_image_refs(md_content)
        ref_to_src: dict[str, Path] = {}  # old_ref → source_file

        for ref in refs:
            ref_name = Path(ref).name
            for img_file in work_images:
                if img_file.name == ref_name:
                    ref_to_src[ref] = img_file
                    break

        if not ref_to_src and work_images:
            for img_file in work_images:
                if img_file.name in md_content:
                    ref_to_src[img_file.name] = img_file

        # ---- Step 5: Copy images to output images/ dir ----
        for old_ref, src_path in ref_to_src.items():
            dst_path = images_dir / src_path.name
            if dst_path.exists():
                stem = dst_path.stem
                suffix = dst_path.suffix
                dst_path = images_dir / f"{stem}_{abs(hash(src_path)) % 10000:04d}{suffix}"
            shutil.copy2(src_path, dst_path)

            new_ref = f"images/{dst_path.name}"
            md_content = md_content.replace(old_ref, new_ref)
            print(f"  [image] {src_path.name} → {dst_path}", file=sys.stderr)

        if ref_to_src:
            print(f"  Images saved to: {images_dir}", file=sys.stderr)

        # ---- Step 6: Upload to Imgur (optional) ----
        if upload:
            md_content, uploaded = _upload_images_to_imgur(images_dir, md_content)
            if uploaded:
                print(f"  Uploaded {len(uploaded)} images to Imgur", file=sys.stderr)
            if not keep_local_images:
                shutil.rmtree(images_dir, ignore_errors=True)
                print("  Local images deleted (--no-keep-local)", file=sys.stderr)

        # ---- Step 6b: Label images with question numbers ----
        if labels_file:
            # Apply user-provided manual mapping
            mapping = json.loads(Path(labels_file).read_text(encoding="utf-8"))
            md_content = _apply_labels_mapping(md_content, mapping)
            print(f"  Labels applied from: {labels_file}", file=sys.stderr)
        elif label_images:
            # Auto-detect + optionally save mapping for manual edit
            md_content, auto_mapping = _auto_label_images(md_content)
            print("  Images auto-labeled", file=sys.stderr)
            if save_labels:
                Path(save_labels).write_text(
                    json.dumps(auto_mapping, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(f"  Label mapping saved to: {save_labels} (edit and re-run with --labels-file)",
                      file=sys.stderr)

        # ---- Step 7: Write final Markdown ----
        result_path = output_dir / "result.md"
        result_path.write_text(md_content, encoding="utf-8")
        print(f"Saved: {result_path} ({len(md_content):,} chars)", file=sys.stderr)

        return md_content

    finally:
        # Clean up work dir (not the output)
        shutil.rmtree(work_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Parse a math exam JPG into Markdown"
    )
    parser.add_argument("image", type=str, help="Path to the JPG image")
    parser.add_argument("-o", "--output-dir", type=str, default=None,
                        help="Output directory (default: ./<image_name>_out/)")
    parser.add_argument("--max-dim", type=int, default=1536,
                        help="Downsample so longest edge ≤ N pixels "
                             "(default: 1536, 0 = no resize)")
    parser.add_argument("--cpu-threads", type=int, default=None,
                        help="Limit PaddlePaddle CPU threads (default: all cores)")
    parser.add_argument("--upload", action="store_true",
                        help="Upload extracted images to Imgur anonymously")
    parser.add_argument("--no-keep-local", action="store_true",
                        help="Delete local images/ dir after uploading to Imgur")
    parser.add_argument("--label-images", action="store_true",
                        help="Auto-label images with nearest question number")
    parser.add_argument("--save-labels", type=str, default=None,
                        help="Save auto-detected {filename: QN} mapping to JSON file")
    parser.add_argument("--labels-file", type=str, default=None,
                        help="Apply manual {filename: QN} mapping from a JSON file "
                             "(overrides --label-images)")
    args = parser.parse_args()

    if args.cpu_threads:
        os.environ["OMP_NUM_THREADS"] = str(args.cpu_threads)
        os.environ["MKL_NUM_THREADS"] = str(args.cpu_threads)
        print(f"CPU threads: {args.cpu_threads}", file=sys.stderr)

    image_path = Path(args.image)

    output_dir = Path(args.output_dir) if args.output_dir else Path(image_path.stem + "_out")

    try:
        parse_exam(
            image_path=image_path,
            output_dir=output_dir,
            max_dim=args.max_dim,
            upload=args.upload,
            keep_local_images=not args.no_keep_local,
            label_images=args.label_images,
            labels_file=args.labels_file,
            save_labels=args.save_labels,
        )
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
