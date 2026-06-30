# CONTEXT.md — Glossary

This file defines the canonical domain terms for the Math Exam Parser system.
It is a glossary, not a spec. No implementation details.

---

## Task (Frontend Task)

A user-submitted batch of exam page images that belong to the same exam paper.
One task produces one merged Markdown output.

A task has:
- A unique `front_task_id`
- One or more **pages**
- Merged **Markdown** (built from all pages' OCR results)
- **Image files** stored on disk, referenced by filename in the Markdown (e.g. `images/img_in_image_box_...jpg`)
- **Mapping** between images and question numbers (expressed as `*[Q<N> 附图]*` labels inside the Markdown)

Statuses: `pending` → `processing` → `done` | `failed`

## Page

One image within a task. A page corresponds to one physical exam paper sheet.
Pages are ordered by upload sequence — the first uploaded file is page 1, the second is page 2, etc.

Each page maps to one **OCR task**.

## OCR Task

A request to the PaddleOCR-VL service to parse a single exam page image.
Returns raw Markdown with LaTeX formulas and extracted embedded figures (images as Base64).

Statuses: `pending` → `processing` → `done` | `failed`

OCR tasks are ephemeral — results are retained for 5 minutes after reaching `done` status, then discarded. The **frontend service** may re-fetch the result within that window.

## Markdown

The structured output of OCR. Contains:
- Recognized text, tables, and LaTeX math formulas
- `<img>` or `![alt]()` references to extracted figures (referencing `images/<filename>` paths)
- `<!-- page N -->` … `<!-- /page N -->` wrapper markers denoting page boundaries
- `*[Q<N> 附图]*` labels inserted by the mapping editor, indicating which question a figure belongs to

During editing and preview, image refs point to local files served by the frontend.
The **downloaded** Markdown is self-contained: image refs are converted to inline Base64 data URIs at download time.

## Image (Figure / 附图)

An embedded figure extracted from an exam page by PaddleOCR-VL.
Images are stored as files in the frontend service's `uploads/{task_id}/images/` directory.
Each image has a filename assigned by PaddleOCR-VL (e.g. `img_in_image_box_Y1_X1_Y2_X2.jpg`).

The filename is the stable key for **mapping** and **auto-fill** logic. It is preserved throughout the pipeline and only replaced with a Base64 data URI at final download.

## Mapping (标注)

A relationship between an image filename and a question number (e.g., `img_001.jpg → Q13`).
Mappings are persisted directly inside the Markdown as `*[Q13 附图]*` labels placed before each image reference. There is no separate mapping table.

## OCR Service (PaddleOCR-VL Service)

A stateless HTTP service that accepts page images and returns OCR results.
It manages its own internal task queue (in-memory, serial processing, single model instance + threading.Lock).
Called by the **frontend service** only (both for image submission and polling). The browser never talks to the OCR service directly.
Completed results are retained for 5 minutes after reaching `done`, then discarded. The frontend re-fetches within that window.
The TTL governs **result retention only** — it starts when a task reaches `done`. A task that is still `pending` (queued) or `processing` does **not** expire, so pages queued behind slow serial inference are never auto-failed before they run.

## Frontend Service

The user-facing web application. Responsibilities:
- Task creation and management
- Receiving page image uploads from the browser and forwarding them to the OCR service
- Storing extracted images to disk and serving them for editing/preview
- Polling OCR task status
- Merging multi-page Markdown (in upload order)
- Providing the mapping editor UI
- Converting image refs to Base64 data URIs at download time, producing a self-contained Markdown file
