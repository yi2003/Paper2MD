#!/usr/bin/env python3
"""Generate a self-contained HTML page to manually map figures to questions.

Usage:
    python edit_labels.py output_dir
    python edit_labels.py output_dir --exam exam_page.jpg
"""

import argparse
import json
import sys
from pathlib import Path

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Figure → Question Mapping</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, system-ui, sans-serif; background: #1a1a2e; color: #eee; padding: 20px; }
  h1 { text-align: center; margin-bottom: 8px; font-size: 1.3em; }
  .subtitle { text-align: center; color: #888; margin-bottom: 20px; font-size: 0.85em; }
  .exam-preview { text-align: center; margin-bottom: 24px; }
  .exam-preview img { max-width: 100%; max-height: 300px; border: 1px solid #444; border-radius: 6px; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; }
  .card { background: #16213e; border: 1px solid #333; border-radius: 8px; padding: 12px; display: flex; flex-direction: column; align-items: center; }
  .card img { max-width: 100%; max-height: 180px; border: 1px solid #333; border-radius: 4px; cursor: pointer; transition: transform .15s; }
  .card img:hover { transform: scale(1.02); }
  .card .filename { font-size: 0.7em; color: #777; margin: 8px 0 4px; word-break: break-all; }
  .card label { font-size: 0.85em; margin-top: 6px; }
  .card input { width: 80px; text-align: center; padding: 6px; border-radius: 4px; border: 1px solid #555; background: #222; color: #fff; font-size: 1em; margin-top: 4px; }
  .card input:focus { outline: none; border-color: #4fc3f7; }
  .toolbar { position: sticky; bottom: 0; background: #16213e; border-top: 1px solid #333; padding: 14px 20px; display: flex; gap: 12px; justify-content: center; align-items: center; margin-top: 24px; border-radius: 8px; }
  .toolbar button { padding: 10px 28px; border: none; border-radius: 6px; cursor: pointer; font-size: 0.95em; font-weight: 600; }
  .btn-save { background: #4fc3f7; color: #1a1a2e; }
  .btn-save:hover { background: #29b6f6; }
  .btn-auto { background: #444; color: #ddd; }
  .btn-auto:hover { background: #555; }
  .btn-reset { background: transparent; color: #f77; border: 1px solid #f77 !important; }
  .btn-reset:hover { background: #f771; }
  .status { color: #4caf50; font-size: 0.9em; min-width: 200px; text-align: center; }
  .modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,.85); z-index: 100; justify-content: center; align-items: center; }
  .modal.active { display: flex; }
  .modal img { max-width: 95vw; max-height: 95vh; cursor: pointer; }
</style>
</head>
<body>

<h1>Figure → Question Mapping</h1>
<p class="subtitle">For each extracted figure, enter the question number it belongs to.</p>

{{EXAM_SECTION}}

<div class="grid" id="grid"></div>

<div class="toolbar">
  <button class="btn-auto" onclick="autoFill()">Auto-fill from coords</button>
  <button class="btn-reset" onclick="resetAll()">Clear all</button>
  <span class="status" id="status"></span>
  <button class="btn-save" onclick="save()">Download labels.json</button>
</div>

<div class="modal" id="modal" onclick="this.classList.remove('active')">
  <img id="modalImg" src="">
</div>

<script>
const IMAGES = {{IMAGES_JSON}};

// Parse coords from filename: img_in_image_box_Y1_X1_Y2_X2.ext
function parseCoords(filename) {
  const m = filename.match(/img_in_image_box_(\\d+)_(\\d+)_(\\d+)_(\\d+)/);
  return m ? { y1: +m[1], x1: +m[2], y2: +m[3], x2: +m[4] } : null;
}

// Auto-fill question numbers from y-coordinate ordering
function autoFill() {
  let items = IMAGES.map((img, i) => {
    const c = parseCoords(img.originalName || img.name);
    return { ...img, y: c ? c.y1 : 9999, idx: i };
  });
  // Sort by y position (top-to-bottom on exam page)
  items.sort((a, b) => a.y - b.y);

  // Assign sequential question numbers starting from guess
  // Prompt user for starting question number
  let start = parseInt(prompt('Starting question number?', '13')) || 13;
  items.forEach((item, i) => {
    document.getElementById('q_' + item.idx).value = start + i;
  });
  updateStatus('Auto-filled from coordinates. Adjust if needed.');
}

function resetAll() {
  IMAGES.forEach((_, i) => { document.getElementById('q_' + i).value = ''; });
  updateStatus('Cleared.');
}

function updateStatus(msg) {
  document.getElementById('status').textContent = msg;
  setTimeout(() => { document.getElementById('status').textContent = ''; }, 3000);
}

function save() {
  const mapping = {};
  IMAGES.forEach((img, i) => {
    const q = document.getElementById('q_' + i).value.trim();
    if (q) {
      const key = img.originalName || img.name;
      mapping[key] = q.startsWith('Q') ? q : 'Q' + q;
    }
  });

  const blob = new Blob([JSON.stringify(mapping, null, 2)], {type: 'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'labels.json';
  a.click();
  URL.revokeObjectURL(a.href);
  updateStatus(`Saved! ${Object.keys(mapping).length} mappings → labels.json`);
}

function zoom(src) {
  document.getElementById('modalImg').src = src;
  document.getElementById('modal').classList.add('active');
}

// Render cards
const grid = document.getElementById('grid');
IMAGES.forEach((img, i) => {
  const card = document.createElement('div');
  card.className = 'card';
  card.innerHTML = `
    <img src="${img.src}" alt="${img.name}" title="Click to zoom" onclick="zoom('${img.src}')">
    <div class="filename">${img.originalName || img.name}</div>
    <label>Question #</label>
    <input type="text" id="q_${i}" placeholder="e.g. 13" value="${img.guess || ''}">
  `;
  grid.appendChild(card);
});
</script>
</body>
</html>"""


def _find_images(images_dir: Path) -> list[Path]:
    return sorted([f for f in images_dir.iterdir()
                   if f.is_file() and f.suffix.lower() in IMAGE_EXTENSIONS])


def _guess_question(filename: str, md_content: str) -> str:
    """Try to guess question number from filename position in markdown."""
    import re
    # Find the image ref in markdown
    for line in md_content.split('\n'):
        if filename in line:
            # Look backwards for last question number
            pos = md_content.find(filename)
            if pos < 0:
                break
            before = md_content[:pos]
            qs = re.findall(r"^(\d{1,2})[.．,、]\s", before, re.MULTILINE)
            if qs:
                return qs[-1]
    return ""


def generate_editor(
    images_dir: Path,
    md_path: Path,
    exam_image: Path | None,
    output_path: Path,
) -> None:
    """Generate a self-contained HTML mapping editor."""
    images = _find_images(images_dir)
    if not images:
        print(f"No images found in {images_dir}", file=sys.stderr)
        sys.exit(1)

    md_content = md_path.read_text(encoding="utf-8") if md_path.is_file() else ""

    images_json: list[dict] = []
    for img in images:
        # Check if md_content already has Imgur URLs pointing to this image
        src = f"images/{img.name}"
        # Use local file as base64 for self-contained HTML? Too large.
        # Instead, just reference the local file relative to output_dir
        images_json.append({
            "name": img.name,
            "originalName": img.name,
            "src": src,
            "guess": _guess_question(img.name, md_content),
        })

    exam_section = ""
    if exam_image and exam_image.is_file():
        exam_section = f'<div class="exam-preview"><img src="../{exam_image.name}" alt="Exam page"></div>'

    html = HTML_TEMPLATE.replace("{{EXAM_SECTION}}", exam_section)
    html = html.replace("{{IMAGES_JSON}}", json.dumps(images_json, ensure_ascii=False))

    output_path.write_text(html, encoding="utf-8")
    print(f"Editor written to: {output_path}")
    print(f"  Open this file in a browser, map each figure to a question,")
    print(f"  then click 'Download labels.json'. Re-run with:")
    print(f"    python parse_exam.py <image> --labels-file labels.json")


def main():
    parser = argparse.ArgumentParser(
        description="Generate a visual HTML editor to map figures to questions"
    )
    parser.add_argument("output_dir", type=str,
                        help="Path to parse_exam.py output directory")
    parser.add_argument("--exam", type=str, default=None,
                        help="Path to the original exam JPG (for context)")
    parser.add_argument("-o", type=str, default=None,
                        help="Output HTML path (default: output_dir/mapping_editor.html)")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    images_dir = output_dir / "images"
    md_path = output_dir / "result.md"

    if not images_dir.is_dir():
        print(f"ERROR: images/ not found in {output_dir}", file=sys.stderr)
        sys.exit(1)

    exam_image = Path(args.exam) if args.exam else None

    html_path = Path(args.o) if args.o else output_dir / "mapping_editor.html"
    generate_editor(images_dir, md_path, exam_image, html_path)


if __name__ == "__main__":
    main()
