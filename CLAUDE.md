# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project purpose

A PaddleOCR-VL–based CLI pipeline that parses a single JPG math exam paper into Markdown with LaTeX formulas and embedded figures. It also supports Imgur upload and manual/auto figure→question labeling.

## Setup & install

```bash
# 1. Install PaddlePaddle CPU-only FIRST (from their mirror)
pip install --upgrade pip setuptools wheel
pip install paddlepaddle==3.3.0 -i https://www.paddlepaddle.org.cn/packages/stable/cpu/

# 2. Then install the remaining dependency
cd service
pip install -r requirements.txt
```

Set `IMGUR_CLIENT_ID` in `service/.env` if you want authenticated Imgur uploads (otherwise it falls back to the anonymous public client ID, rate-limited ~50/hour).

## Common commands

```bash
# Basic parse (outputs to ./<imagename>_out/)
python service/parse_exam.py service/exam_page.jpg

# Custom output dir + downsample
python service/parse_exam.py service/exam_page.jpg -o my_output --max-dim 1536

# Parse + upload extracted figures to Imgur
python service/parse_exam.py service/exam_page.jpg --upload

# Parse + auto-label figures with nearest question number + save mapping
python service/parse_exam.py service/exam_page.jpg --label-images --save-labels labels.json

# Re-apply a manually-edited label mapping
python service/parse_exam.py service/exam_page.jpg --labels-file labels.json

# Generate HTML editor for manual figure→question mapping review
python service/edit_labels.py output_dir --exam service/exam_page.jpg

# Post-hoc upload images to Imgur and rewrite Markdown refs
python service/upload_to_imgur.py output_dir --replace

# Quick smoke test
python service/test_service.py service/exam_page.jpg
```

## Architecture

The pipeline (`parse_exam.py`) runs in 7 sequential steps:

1. **Downsample** — resize the input JPG so its longest edge ≤ `--max-dim` (1536 by default), via PIL/LANCZOS
2. **PaddleOCR-VL inference** — `PaddleOCRVL(pipeline_version="v1.6")` with layout detection + chart recognition enabled; writes intermediate Markdown + extracted figures into a `_work/` scratch dir
3. **Find generated Markdown** — picks the most recent `.md` file from `_work/`
4. **Match image refs** — regex-extracts `![alt](path)` and `<img src="path">` refs from the Markdown, links them to actual image files discovered in `_work/`
5. **Copy images** — copies matched images to `output_dir/images/` and rewrites refs to `images/<filename>`
6. **Imgur upload (optional)** — base64-uploads each image via Imgur API, replaces local `images/` refs with Imgur URLs; `--no-keep-local` deletes the local `images/` dir afterward
7. **Write final Markdown** — saves `result.md`; the `_work/` scratch dir is always cleaned up (finally block)

The pipeline uses a thread-safe **singleton** for the PaddleOCR-VL model (lazy-loaded, guarded by `threading.Lock`).

### Labeling subsystem

- `--label-images` auto-detects question headers (`^\d{1,2}[.．,、]`) and inserts `*[Q<N> 附图]*` labels before each image based on positional proximity.
- `--save-labels labels.json` persists the auto-generated `{filename: "QN"}` mapping for manual editing.
- `--labels-file labels.json` applies a user-curated mapping (overrides auto-labeling).
- `edit_labels.py` generates a standalone HTML page showing each extracted figure with a text input for the question number, plus coordinate-based auto-fill and a "Download labels.json" button. No server needed — just open the HTML file in a browser.

### Standalone upload tool

`upload_to_imgur.py` can upload images from any existing output directory independently of the main pipeline. `--replace` overwrites the local image refs in `result.md` with Imgur URLs.

## Notes

- PaddleOCR-VL requires a PaddlePaddle installation compatible with its wheel; CPU-only is used here.
- The `oneDNN` flag (`FLAGS_use_mkldnn`) is explicitly disabled — it crashes PaddleOCR-VL due to missing `pir::ArrayAttribute` support.
- Imgur anonymous uploads use the public web client ID `546c25a59c58ad7` (rate-limited). Set `IMGUR_CLIENT_ID` in `.env` for higher limits.
- The model output quality depends heavily on the input resolution and layout clarity. Adjust `--max-dim` to balance speed vs. OCR accuracy.
