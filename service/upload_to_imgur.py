#!/usr/bin/env python3
"""Upload images from an output_dir/images/ folder to Imgur and rewrite Markdown.

Usage:
    python upload_to_imgur.py output_dir
    python upload_to_imgur.py output_dir --replace    (replace local refs with URLs in result.md)
"""

import argparse
import base64
import json
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

import os

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}
IMGUR_UPLOAD_URL = "https://api.imgur.com/3/image"

# ---------------------------------------------------------------------------
# Imgur client
# ---------------------------------------------------------------------------
# Imgur's public web client ID for anonymous uploads (rate-limited: ~50/hour)
_ANON_CLIENT_ID = "546c25a59c58ad7"
CLIENT_ID = os.getenv("IMGUR_CLIENT_ID", "")

if CLIENT_ID in ("", "your_imgur_client_id_here", None):
    CLIENT_ID = _ANON_CLIENT_ID  # fallback to anonymous upload


def upload_image(file_path: Path) -> dict | None:
    """Upload one image to Imgur. Returns the API response data dict."""

    with open(file_path, "rb") as f:
        b64_data = base64.b64encode(f.read()).decode("utf-8")

    resp = requests.post(
        IMGUR_UPLOAD_URL,
        headers={"Authorization": f"Client-ID {CLIENT_ID}"},
        data={"image": b64_data, "type": "base64"},
        timeout=60,
    )

    data = resp.json()
    if not data.get("success"):
        print(f"  ERROR uploading {file_path.name}: {data.get('data', {}).get('error', resp.text)}",
              file=sys.stderr)
        return None

    return data["data"]


def find_images(images_dir: Path) -> list[Path]:
    """Find all image files in a directory (non-recursive)."""
    images: list[Path] = []
    for f in images_dir.iterdir():
        if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS:
            images.append(f)
    return sorted(images)


def upload_dir(images_dir: Path) -> dict[str, str]:
    """Upload all images in a directory. Returns {filename: imgur_url}."""
    images = find_images(images_dir)
    if not images:
        print(f"No images found in {images_dir}", file=sys.stderr)
        return {}

    print(f"Uploading {len(images)} images from {images_dir} ...\n", file=sys.stderr)

    name_to_url: dict[str, str] = {}
    for i, img_path in enumerate(images, 1):
        print(f"  [{i}/{len(images)}] {img_path.name} ... ", end="", file=sys.stderr)
        data = upload_image(img_path)
        if data:
            url = data["link"]
            name_to_url[img_path.name] = url
            print(f"✓ {url}", file=sys.stderr)
        else:
            print(f"✗ skipped", file=sys.stderr)

    return name_to_url


def replace_refs_in_md(md_path: Path, name_to_url: dict[str, str]) -> str:
    """Replace local image refs with Imgur URLs in the given Markdown file."""
    content = md_path.read_text(encoding="utf-8")

    for filename, url in name_to_url.items():
        # image references like images/filename.jpg
        content = content.replace(f"images/{filename}", url)

        # also catch bare filename references
        content = content.replace(filename, url)

    # Overwrite the file
    md_path.write_text(content, encoding="utf-8")
    return content


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Upload images from output_dir/images/ to Imgur"
    )
    parser.add_argument(
        "output_dir", type=str,
        help="Path to the parse_exam.py output directory (contains result.md + images/)"
    )
    parser.add_argument(
        "--replace", action="store_true",
        help="Rewrite result.md replacing local refs with Imgur URLs"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be uploaded without uploading"
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    images_dir = output_dir / "images"
    md_path = output_dir / "result.md"

    if not images_dir.is_dir():
        print(f"ERROR: images directory not found: {images_dir}", file=sys.stderr)
        sys.exit(1)

    if args.dry_run:
        images = find_images(images_dir)
        print(f"Would upload {len(images)} images:", file=sys.stderr)
        for img in images:
            print(f"  - {img.name}", file=sys.stderr)
        return

    # Upload
    name_to_url = upload_dir(images_dir)

    if not name_to_url:
        print("No images were uploaded.", file=sys.stderr)
        return

    # Print summary
    print(f"\n--- Uploaded {len(name_to_url)} images ---", file=sys.stderr)
    for name, url in name_to_url.items():
        print(f"  {name}  →  {url}", file=sys.stderr)

    # Replace in Markdown
    if args.replace and md_path.is_file():
        replace_refs_in_md(md_path, name_to_url)
        print(f"\n✓ Updated {md_path} with Imgur URLs", file=sys.stderr)

    # Output JSON for scripting
    print(json.dumps(name_to_url, indent=2))


if __name__ == "__main__":
    main()
