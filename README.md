# PageForge

> Break multi-page PDFs into clean, OCR-ready images — from the command line or a self-hosted web UI.

![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)
![Docker](https://img.shields.io/badge/docker-ready-2496ED)
![License MIT](https://img.shields.io/badge/license-MIT-green)

---

## Features

- **CLI-first** — run `python main.py document.pdf` with no server required
- **Watch folder** — drop PDFs into an inbox directory; PageForge processes them automatically on each sync cycle
- **Web UI** with drag-and-drop upload, file browser, coverage calendar, and per-file ZIP download
- **Google Drive optional** — connect a Drive folder to pull PDFs automatically (requires a GCP service account)
- **High-contrast B&W output** — autocontrast, double sharpen, and binarize for maximum OCR accuracy
- **300 DPI by default** — configurable from 72 to 600 DPI
- **Configurable** — all settings adjustable via environment variables, a JSON config file, or the web Settings tab
- **Self-hosted** — runs entirely on your machine; no external services required unless you enable Drive sync

---

## Quick Start

### CLI

```bash
# Single file — output next to the source
python main.py document.pdf

# Glob — batch convert
python main.py *.pdf

# Custom output directory
python main.py scan.pdf --output /path/to/images/

# JPEG output with quality control (much smaller files)
python main.py scan.pdf --format jpeg --quality 85

# Grayscale — no binarization, good for photos or mixed documents
python main.py scan.pdf --mode grayscale

# Full color output
python main.py scan.pdf --mode color --format jpeg

# Override DPI and threshold inline
python main.py scan.pdf --dpi 150 --threshold 180
```

Output images are written to `{stem}_pages/` by default, or the directory specified with `--output`.

### Docker

```bash
docker run --rm -v $(pwd)/data:/data -p 8000:8000 pageforge
```

Place PDFs in `./data/inbox/` and open `http://localhost:8000` in your browser.

### Docker Compose

```bash
cp docker-compose.example.yml docker-compose.yml
docker compose up -d
```

Edit `docker-compose.yml` to uncomment and set environment variables as needed.

---

## How It Works

Each PDF page passes through a five-step image processing pipeline:

1. **Grayscale render** — PyMuPDF renders the page at the configured DPI directly into a grayscale pixel buffer, skipping color conversion overhead.
2. **Autocontrast** — Pillow's `ImageOps.autocontrast` stretches the histogram with a 2% cutoff, compensating for yellowed paper or uneven scan exposure.
3. **Double sharpen** — Two passes of `ImageFilter.SHARPEN` enhance edge definition so character strokes are crisp.
4. **Binarize** — A point transform converts every pixel below the threshold to pure black and every pixel at or above it to pure white, producing a true 1-bit-style image in an 8-bit container.
5. **PNG optimize** — The result is saved with `optimize=True`, letting the PNG encoder find the smallest lossless encoding of the high-contrast data.

The output is a sequence of small, high-contrast PNG files that OCR engines and AI vision models handle reliably.

---

## Configuration

All settings are optional. PageForge works with zero configuration.

| Name | Default | Description |
|---|---|---|
| `PAGEFORGE_INBOX` | `/data/inbox` | Local folder watched for incoming PDFs |
| `PAGEFORGE_OUTPUT` | `/data/output` | Root directory where page images are saved |
| `PAGEFORGE_DPI` | `300` | Render resolution; higher values increase file size and quality |
| `PAGEFORGE_THRESHOLD` | `160` | Binarize cutoff (0–255); lower values keep more pixels black |
| `PAGEFORGE_MODE` | `bw` | `bw` (binarized, best for OCR), `grayscale` (no binarize), or `color` (full color) |
| `PAGEFORGE_FORMAT` | `png` | Output format: `png` (lossless) or `jpeg` (smaller files) |
| `PAGEFORGE_JPEG_QUALITY` | `85` | JPEG quality 1–95; only applies when format is `jpeg` |
| `PAGEFORGE_ENABLE_UPLOAD` | `true` | Set to `false` to disable the web upload endpoint |
| `SYNC_INTERVAL_MINUTES` | `30` | How often (in minutes) to poll the inbox and Drive folder |
| `DRIVE_FOLDER_ID` | _(empty)_ | Google Drive folder ID; leave blank to disable Drive sync |
| `PAGEFORGE_CONFIG` | `/data/config.json` | Path to the JSON config file (overrides defaults, overridden by env vars) |

Settings can also be changed at runtime via the web Settings tab, which writes to `config.json`.

---

## Google Drive Setup

> **You supply your own credentials.** PageForge includes no Google auth of any kind. You create a Google Cloud project, generate a service account key, and share whichever Drive folder you want with that account. Your credentials stay on your machine.

See **[docs/google-drive-setup.md](docs/google-drive-setup.md)** for the full step-by-step walkthrough, including screenshots guidance, troubleshooting, and how to disable Drive sync without removing the key file.

**Short version:**

1. Create a GCP project and enable the **Google Drive API**.
2. Create a **Service Account** and download its JSON key.
3. Place the key at `./data/credentials.json` (mounted as `/data/credentials.json` in the container).
4. Share your Drive folder with the service account email as **Editor**.
5. Set `DRIVE_FOLDER_ID` in your environment or via the web Settings tab.

PageForge will poll the folder on each sync cycle, download new PDFs, convert them, and delete the originals from Drive.

---

## Web UI

The web interface is available at `http://localhost:8000` when running as a server.

- **Files tab** — shows summary statistics (total files, pages, days, years), an interactive monthly coverage calendar, and a filterable table of all processed documents. Each row has a ZIP download button.
- **Upload tab** — drag-and-drop or click-to-browse PDF upload. Files are processed in the background; the UI polls for completion and updates the status in real time. Hidden when `PAGEFORGE_ENABLE_UPLOAD` is `false`.
- **Settings tab** — live configuration editor for all settings. Changes are saved to `config.json` and take effect immediately without restarting the server.

---

## Output

For a PDF named `2024-03-15_invoice.pdf`, PageForge creates:

```
2024-03-15_invoice_pages/
  2024-03-15_invoice_p001.png
  2024-03-15_invoice_p002.png
  2024-03-15_invoice_p003.png
  ...
```

- Images are named `{stem}_p{page:03d}.png` (zero-padded to three digits).
- Each directory is self-contained and can be zipped for download via the API or web UI.
- When files are named with a `YYYY-MM-DD` prefix, the web UI groups and filters them by date, month, and year.

---

## Use Cases

- **OCR pipelines** — feed the output PNGs directly into Tesseract, EasyOCR, or a cloud OCR API
- **AI document ingestion** — send high-contrast page images to vision-capable language models for extraction or summarization
- **Digitizing paper records** — scan documents to PDF, drop them in the inbox, and get clean per-page images automatically
- **Batch archival** — process hundreds of PDFs overnight using the CLI glob mode or the watch-folder scheduler

---

## License

MIT
