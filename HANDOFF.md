# Handoff — Math Exam Parser: Two-Service Architecture

## Context

The current codebase is a single CLI script (`service/parse_exam.py`) that runs PaddleOCR-VL on a JPG exam paper and outputs Markdown. The task is to split this into two FastAPI services and a web frontend.

Reference docs in repo:
- `CLAUDE.md` — setup, architecture overview, commands
- `CONTEXT.md` — glossary of canonical domain terms

## What to build

Two FastAPI services (`ocr_service/` and `frontend_service/`) implementing the architecture below.

### Directory target

```
math-exam-parser/
├── CLAUDE.md                          # already exists
├── CONTEXT.md                         # already exists
├── HANDOFF.md                         # this file
├── service/                           # existing — keep as reference
├── ocr_service/
│   ├── main.py                        # FastAPI app, POST/GET /tasks
│   ├── pipeline.py                    # core OCR pipeline (from parse_exam.py)
│   ├── task_manager.py                # in-memory FIFO task queue + background worker
│   ├── requirements.txt
│   └── .env.example
└── frontend_service/
    ├── main.py                        # FastAPI app, all frontend endpoints
    ├── models.py                      # SQLite models (tasks, pages, markdown)
    ├── task_manager.py                # OCR polling, Markdown merging, mapping logic
    ├── templates/
    │   ├── upload.html                # create task + upload pages
    │   ├── task_list.html             # all tasks overview
    │   ├── task_status.html           # single task progress (polling)
    │   └── edit.html                  # mapping editor (left Markdown + right image grid)
    ├── static/
    │   └── (CSS, JS as needed)
    ├── uploads/                        # runtime: task images stored here
    ├── requirements.txt
    └── .env.example
```

## Architecture decisions (all 17 settled)

| # | Decision | Choice |
|---|---|---|
| 1 | Task | Multi-page exam → single merged Markdown |
| 2 | Page order | Upload order = page order. First uploaded = page 1. Merged sequentially with `<!-- page N -->` markers |
| 3 | Service boundary | OCR service: OCR only. Frontend: merge, mapping, output |
| 4 | OCR API | Async REST: `POST /tasks`, `GET /tasks/{id}` |
| 5 | Image format (internal) | Filename references (`images/img_...jpg`). Images stored as files on disk. Stable key for labeling/auto-fill |
| 6 | Image format (download) | Base64 data URIs inline. Converted at download time only. Single self-contained `.md` file |
| 7 | Mapping persistence | Mappings stored as `*[Q<N> 附图]*` labels inside Markdown — no separate mapping table. Labels key on filenames |
| 8 | Task linking | Browser uploads all pages to **frontend service** first. Frontend forwards to OCR, tracks `ocr_task_id` internally. No `front_task_id` passed to OCR — avoids orphan tasks |
| 9 | Progress | Frontend polls OCR service (not webhook) |
| 10 | Auth | None. Task list visible to all |
| 11 | OCR concurrency | **Serial.** Single model instance + `threading.Lock` + FIFO queue |
| 12 | Failure | Per-page failure, retryable. Other pages unaffected |
| 13 | OCR lifecycle | OCR retains completed results for **5 minutes** after `done` (frontend may re-fetch), then discards. TTL is result-retention only — `pending`/`processing` tasks do **not** expire while queued, so serial-queue backlog never auto-fails un-run pages |
| 14 | Editor UI | Left/right split: Markdown preview + image grid. Auto-save (blur) |
| 15 | Tech stack | FastAPI (both), SQLite (frontend), Jinja2 templates, filesystem for images |
| 16 | Base64 conversion | Deferred to download. Markdown keeps `images/<filename>` refs during editing. `GET /tasks/{id}/download` inlines Base64 on the fly |
| 17 | Upload format | Frontend accepts `.jpg`/`.jpeg`/`.png`; non-JPEG inputs are converted to JPEG before forwarding to OCR (the ported `parse_exam.py` pipeline is JPG-only — see its `suffix` guard). Reject other types with HTTP 415 |

## Data flow (detailed)

### Phase 1 — Task creation

```
Browser  POST /tasks
         Content-Type: application/json
         body: {"pages": 3}

Frontend INSERT INTO tasks (id, status, created_at)
              VALUES ('ft-001', 'pending', now)
         INSERT INTO pages (task_id, page_index, status)
              VALUES
                ('ft-001', 0, 'awaiting_upload'),
                ('ft-001', 1, 'awaiting_upload'),
                ('ft-001', 2, 'awaiting_upload')

         ← 201  {"task_id": "ft-001", "pages": 3}
```

Task status: `pending` (not yet submitted to OCR).
Page status: `awaiting_upload` → `pending` → `processing` → `done` | `failed` | `retrying`.

### Phase 2 — Page upload (browser uploads each page to frontend)

```
Browser  POST /tasks/ft-001/pages?index=0
         Content-Type: multipart/form-data
         file: page_0.jpg

Frontend Receives file → saves to temp path
         Forwards to OCR service immediately (sync HTTP call):

         POST http://ocr-service:8001/tasks
         Content-Type: multipart/form-data
         file: page_0.jpg

         ← 201  {"task_id": "ocr-aaa", "status": "pending"}

Frontend UPDATE pages SET ocr_task_id = 'ocr-aaa', status = 'pending'
              WHERE task_id = 'ft-001' AND page_index = 0

         ← 200  {"page_index": 0, "ocr_task_id": "ocr-aaa", "status": "pending"}
```

Browser repeats for index=1 → `ocr-bbb`, index=2 → `ocr-ccc`.

If OCR service is unreachable at upload time: frontend returns 502, page stays `awaiting_upload`. Browser retries the upload. No orphan — OCR task was never created.

### Phase 3 — OCR processing (inside OCR service)

```
OCR worker thread (single, serial):

1. Dequeue next task from queue.Queue
2. task.status = "processing"
3. pipeline.predict(image_path):
     a. _downsample_image(src, dst, max_dim)     # PIL/LANCZOS
     b. PaddleOCRVL.predict(dst, ...)             # layout + chart recognition
     c. Save markdown to _work/ dir               # images/ refs with filenames
     d. Read markdown from _work/*.md
     e. Find images in _work/, base64-encode each
     f. Return (markdown, [{filename, base64, mime_type}])
4. task.result = {markdown: "...", images: [...]}
5. task.status = "done"
6. task.completed_at = now()    # TTL clock starts

On exception: task.status = "failed", task.error = str(e)
```

OCR service task TTL: 300s from creation or last `done`. Cleanup thread runs every 30s, removes expired tasks from dict.

### Phase 4 — Frontend polling & result collection

```
Frontend background loop (runs every 2s):

for each task in SELECT * FROM tasks WHERE status IN ('pending', 'processing'):
    for each page in task.pages WHERE status IN ('pending', 'processing'):

        GET http://ocr-service:8001/tasks/{page.ocr_task_id}

        ┌─ status: "pending"   → no-op, continue polling
        ├─ status: "processing" → no-op, continue polling
        ├─ status: "done":
        │     response.result = {
        │       markdown: "...![](images/img_001.jpg)...",
        │       images: [
        │         {filename: "img_001.jpg", base64: "iVBORw...", mime_type: "image/jpeg"},
        │         ...
        │       ]
        │     }
        │
        │     Frontend:
        │       for each image in result.images:
        │         write base64 → uploads/ft-001/images/img_001.jpg
        │       UPDATE pages SET markdown = result.markdown, status = 'done'
        │       UPDATE tasks SET status = 'processing'  # some pages still pending
        │
        └─ status: "failed":
              UPDATE pages SET status = 'failed', error = result.error

    if all pages.status == 'done':
        merge_markdown = ""
        for page in pages ORDER BY page_index:
            merge_markdown += f"<!-- page {page.page_index + 1} -->\n\n"
            merge_markdown += page.markdown
            merge_markdown += f"\n\n<!-- /page {page.page_index + 1} -->\n\n"

        UPDATE tasks SET merged_markdown = merge_markdown, status = 'done'

    if any pages.status == 'failed' AND all others == 'done':
        merge as above, failed pages get placeholder:
            f"<!-- page {i+1} FAILED: {page.error} -->\n\n"
        UPDATE tasks SET status = 'partial'
```

### Phase 5 — Mapping editor

```
Browser  GET /tasks/ft-001/edit

Frontend Renders edit.html:
  - Left panel:  <div id="preview">{merged_markdown rendered as HTML}</div>
                 Images show via <img src="/tasks/ft-001/images/img_001.jpg">
                 Frontend serves these from uploads/ft-001/images/ via static route

  - Right panel: Image cards built from filenames found in markdown
                 (via _extract_image_filenames on merged_markdown)
                 Each card:
                   <img src="/tasks/ft-001/images/img_001.jpg">
                   <input id="q_img_001" value="13" onblur="autoSave()">
                   (value pre-filled from existing *[Q13 附图]* label if present)

Auto-fill button:
  parseCoords("img_in_image_box_41_393_521_565.jpg") → {y1: 41, x1: 393, ...}
  Sort cards by y1 ascending
  Prompt user: "Starting question number?" → user enters "13"
  Assign: img_001 → Q13, img_002 → Q14, ...
  Fill input values, trigger autoSave

Auto-save (on input blur):
  Browser reads all input values, builds mapping dict:
    {"img_in_image_box_41_393_521_565.jpg": "Q13",
     "img_in_image_box_524_413_645_538.jpg": "Q14", ...}

  PUT /tasks/ft-001/mapping
  Content-Type: application/json
  body: {"img_in_image_box_41_393_521_565.jpg": "Q13", ...}

  Frontend runs _apply_labels_mapping(merged_markdown, mapping)
           UPDATE tasks SET merged_markdown = result
           ← 200  {"markdown": "<updated markdown>"}

  Browser updates preview panel with returned markdown
```

Mapping application is **server-side**. The `_apply_labels_mapping()` function from parse_exam.py is reused as-is. The client only sends `{filename: "QN"}` dict.

```
Browser  PUT /tasks/ft-001/mapping
         body: {"img_001.jpg": "Q13", "img_002.jpg": "Q14", ...}

Frontend _apply_labels_mapping(merged_markdown, mapping)
         UPDATE tasks SET merged_markdown = result
         ← 200  {"markdown": result}
```

### Phase 6 — Download

```
Browser  GET /tasks/ft-001/download

Frontend md = SELECT merged_markdown FROM tasks WHERE id = 'ft-001'

         # Step 1: find all image refs
         refs = _find_all_image_refs(md)
         # → ["images/img_001.jpg", "images/img_002.jpg", ...]

         # Step 2: replace each with data URI
         for each ref in refs:
             filename = Path(ref).name    # "img_001.jpg"
             filepath = uploads/ft-001/images/filename
             mime = mimetypes.guess_type(filename)[0]  # "image/jpeg"
             b64 = base64.b64encode(read(filepath)).decode()
             data_uri = f"data:{mime};base64,{b64}"
             md = md.replace(ref, data_uri)

         # Step 3: return as file download
         ← 200
           Content-Type: text/markdown
           Content-Disposition: attachment; filename="exam_result.md"
           body: <self-contained markdown>
```

### Timing & concurrency summary

```
t=0s    User creates task                        ← instant
t=0s    User uploads page 0                       ← ~1s (OCR POST returns pending)
t=0s    User uploads page 1
t=0s    User uploads page 2
t=0s    User navigates away / creates new task

t=2s    OCR worker starts page 0                  ← dequeued from FIFO
t=2s    Frontend polls: ocr-aaa=pending, ocr-bbb=pending, ocr-ccc=pending

t=15s   OCR finishes page 0 → status=done
t=16s   Frontend poll: ocr-aaa=done → fetch result, write images, update DB

t=17s   OCR worker starts page 1 (page 0 was blocking it until now)
t=17s   Frontend poll: ocr-bbb=pending

t=30s   OCR finishes page 1 → status=done
t=31s   Frontend poll: ocr-bbb=done → fetch, store

t=32s   OCR worker starts page 2
t=45s   OCR finishes page 2 → status=done
t=46s   Frontend poll: ocr-ccc=done → fetch, store
        All pages done → merge → task status = 'done'

t=50s   User checks task list → sees ft-001 = 'done'
        User opens mapping editor → edits labels → auto-saves
        User downloads final markdown
```

### Error paths

```
OCR service unreachable on upload:
  Frontend returns 502. Page stays 'awaiting_upload'.
  Browser retries upload. No orphan OCR task.

OCR service unreachable during polling:
  Frontend logs warning, skips this poll cycle.
  After 5 minutes of no response: page marked 'failed'.
  OCR task expires via TTL on OCR side independently.

OCR task fails (pipeline exception):
  Page marked 'failed'. User clicks "Retry page N":
    POST /tasks/ft-001/pages/1/retry
    Frontend re-forwards to OCR → new ocr_task_id → polling resumes.

OCR task TTL expires before frontend fetches:
  Frontend gets 404 on next poll → page marked 'failed'.
  User can retry.

Page upload interrupted (browser crash after uploading 2 of 3 pages):
  Task stays 'pending'. Pages 0,1 in 'pending', page 2 in 'awaiting_upload'.
  User can return to task, upload remaining page.
  Or pages expire on OCR side (TTL), frontend marks them 'failed'.
```

## OCR service details

**Endpoints:**
- `POST /tasks` — multipart upload (`file`) → `{task_id, status: "pending"}`
- `GET /tasks/{id}` — → `{task_id, status, result?: {markdown, images: [{filename, base64}]}}`

**Internals:**
- `task_manager.py`: in-memory dict `{task_id: Task}` + `queue.Queue` + single background thread
- `pipeline.py`: port from `parse_exam.py` — downsample → `PaddleOCRVL.predict()` → extract images → return markdown (with `images/<filename>` refs) + `[{filename, base64}]` image list. Markdown uses filename refs, NOT inline Base64
- Model: lazy singleton with `threading.Lock` (already in existing code)
- Status flow: `pending` → `processing` → `done` | `failed`
- **Result-retention TTL: 5 minutes.** The TTL clock starts when a task reaches `done`; results are discarded 5 min later. `pending` (queued) and `processing` tasks do **not** expire — a long serial backlog must never auto-fail pages that haven't run yet.
- Frontend polls with GET; result returned on first GET after `done`, and any subsequent GET within the 5-min retention window.

## Frontend service details

**Database (SQLite):**
- `tasks` table: id, status, created_at, merged_markdown (nullable)
- `pages` table: id, task_id, page_index, ocr_task_id, status, markdown (nullable)

**Endpoints:**
- `POST /tasks` — create task (body: `{pages: N}`) → `{task_id}`
- `POST /tasks/{id}/pages?index=N` — upload one page image (multipart). Validate content type (decision #17), convert to JPEG if needed, then forward to OCR and store `ocr_task_id`. Reject `index` outside `[0, pages)` or a duplicate index for an already-uploaded page (HTTP 409)
- `POST /tasks/{id}/pages/{index}/retry` — re-submit a `failed` page to OCR (new `ocr_task_id`, page status → `pending`); other pages unaffected (decision #12)
- `GET /tasks` — task list page (HTML)
- `GET /tasks/{id}` — task status page (HTML, polls via JS or server-sent)
- `GET /tasks/{id}/status` — JSON status endpoint for polling → `{status, pages: [{index, ocr_task_id, status}]}`
- `GET /tasks/{id}/edit` — mapping editor page (HTML)
- `GET /tasks/{id}/images/<filename>` — serve stored image file for editor preview (static mount on `uploads/{task_id}/images/`)
- `GET /tasks/{id}/markdown` — return current merged markdown as JSON `{markdown}` (for editor preview sync after auto-save)
- `PUT /tasks/{id}/mapping` — save mapping `{filename: "QN", ...}`. Server-side applies `_apply_labels_mapping()` to `merged_markdown`. Returns `{markdown}`
- `GET /tasks/{id}/download` — download final self-contained .md file (images inlined as base64 data URIs)

**Polling logic (in `task_manager.py`):**
- A single **asyncio background task** (started on app startup) polls all non-terminal frontend tasks. Use asyncio throughout to stay consistent with `aiosqlite` — do not mix a sync thread with the async DB driver.
- For each task, poll each page's `ocr_task_id` at OCR service
- On `done`: fetch result. Write images to `uploads/{task_id}/images/` from base64. Store markdown (with `images/<filename>` refs) in DB. Update page status.
- On `failed`: mark the page `failed` (leave other pages running); the page can be re-submitted via the retry endpoint.
- When all pages `done`: merge markdowns (with `<!-- page N -->` markers), store merged_markdown in tasks table, set task status `done`. If any page is terminally `failed`, the task stays non-`done` until the page is retried and succeeds.

**Markdown merging:**
- Concatenate with `<!-- page N -->` / `<!-- /page N -->` wrappers
- No cross-page dedup

**Mapping editor (`edit.html`):**
- Left panel: rendered Markdown preview. Images served by frontend static mount (`uploads/{task_id}/images/` → `/static/images/...`). Markdown refs (`images/filename.jpg`) resolve naturally.
- Right panel: grid of image cards, each with question number input. Image filenames used as card keys.
- "Auto-fill" button: sort images by Y coordinate from filename (`img_in_image_box_Y1_X1_Y2_X2`), assign sequential Q numbers starting from user-specified number
- Auto-save on input blur: PUT `/tasks/{id}/markdown` with updated markdown
- When saving: remove existing `*[Q<N> 附图]*` labels, re-insert based on current inputs before each `<img src="images/...">` or `![](images/...)` reference

## Code to reuse from existing service/

All labeling functions work **unchanged** because filenames are preserved throughout. No adaptation needed.

- `parse_exam.py`:
  - `_downsample_image()` — **reuse as-is** (OCR service pipeline)
  - `get_pipeline()` — **reuse as-is** (OCR service, lazy singleton + lock)
  - `_find_all_image_refs()` — **reuse as-is** (frontend, works on filename refs)
  - `_extract_image_filenames()` — **reuse as-is** (frontend, parses `images/foo.jpg` refs)
  - `_auto_label_images()` — **reuse as-is** (frontend, keys on filenames)
  - `_apply_labels_mapping()` — **reuse as-is** (frontend, keys on filenames)
- `edit_labels.py`:
  - `parseCoords()` JS — **reuse as-is** (parses `img_in_image_box_Y1_X1_Y2_X2` filename)
  - `autoFill()` JS — **reuse** with adaptation (client-side, same coord logic)
  - HTML template CSS/layout — **reuse** with adaptation for Jinja2 + left/right split
- `requirements.txt`: `paddleocr[doc-parser]` + add `fastapi`, `uvicorn`, `python-multipart`, `jinja2`, `aiosqlite`

## New code required

### OCR service

- `main.py`: FastAPI app, `POST /tasks` (multipart), `GET /tasks/{id}` (JSON). 5-min TTL cleanup thread.
- `task_manager.py`: in-memory `dict[str, Task]`, `queue.Queue`, background worker thread.

### Frontend service

- `main.py`: FastAPI app, all user-facing endpoints. Static mount for `uploads/{task_id}/images/`.
- `models.py`: SQLite schema, async CRUD for tasks + pages.
- `task_manager.py`: OCR polling loop, markdown merging, mapping application.
- `templates/`: Four Jinja2 HTML pages (upload, task_list, task_status, edit).
- **Base64 inlining at download** (`GET /tasks/{id}/download`):
  1. Read merged markdown from DB
  2. Regex-find all `images/(*.jpg|.png|...)` refs in `<img src="...">` and `![](...)` 
  3. For each match, read file from `uploads/{task_id}/images/<filename>`, base64-encode, build `data:image/<mime>;base64,...` URI
  4. Replace ref with data URI, return resulting markdown as `.md` download

## Suggested skills for the implementing agent

- **/init** — re-read CLAUDE.md and CONTEXT.md for full domain context before starting
- **prototype** — build one service at a time, starting with OCR service (no UI dependency)
- **tdd** — write tests for OCR service endpoints first, then frontend service
- **verify** — run the app after each service is built to confirm end-to-end flow works
