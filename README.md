# Math Exam Parser

Parse math exam paper images into Markdown with LaTeX formulas and embedded figures. Two-service architecture: OCR engine + web frontend.

## Setup

```bash
# 1. Install PaddlePaddle CPU-only FIRST (from PaddlePaddle mirror)
pip install --upgrade pip setuptools wheel
pip install paddlepaddle==3.3.0 -i https://www.paddlepaddle.org.cn/packages/stable/cpu/

# 2. Install OCR service deps
cd ocr_service
pip install -r requirements.txt

# 3. Install frontend service deps
cd ../frontend_service
pip install -r requirements.txt
```

## Run

```bash
# Terminal 1 — OCR service (port 8001)
cd ocr_service
python main.py

# Terminal 2 — Frontend service (port 8000)
cd frontend_service
python main.py
```

Open **http://localhost:8000** in a browser.

## Usage

1. **Create a task** — enter the number of pages, click "Create Task"
2. **Upload pages** — select an image file for each page (in order: first upload = page 1)
3. **Wait for OCR** — the status page polls progress automatically (~2–5 min per page on CPU)
4. **Edit mapping** — when done, open the editor to link figures to question numbers
5. **Download** — get a self-contained `.md` file with all images embedded as Base64 data URIs

## Architecture

```
Browser ──→ Frontend (8000) ──→ OCR Service (8001)
              FastAPI               FastAPI
              SQLite                In-memory queue
              Jinja2 templates      PaddleOCR-VL v1.6
```

| Service | Role |
|---|---|
| `ocr_service/` | Receives page images, runs PaddleOCR-VL, returns Markdown + extracted figures |
| `frontend_service/` | Task management, multi-page merge, figure→question mapping editor, download |

## Configuration

| Env var | Default | Description |
|---|---|---|
| `OCR_PORT` | `8001` | OCR service port |
| `OCR_WORK_DIR` | `ocr_service/temp/` | Scratch directory for OCR intermediates |
| `FRONTEND_PORT` | `8000` | Frontend service port |
| `OCR_SERVICE_URL` | `http://localhost:8001` | OCR service address (set in frontend `.env`) |
| `POLL_INTERVAL_SEC` | `2` | Frontend polling interval |
