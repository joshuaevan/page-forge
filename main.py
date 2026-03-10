"""
PageForge — Break multi-page PDFs into clean, OCR-ready images.

CLI usage:
    python main.py input.pdf [output_dir]
    python main.py *.pdf

Web/server usage:
    uvicorn main:app --host 0.0.0.0 --port 8000

Environment variables (all optional):
    PAGEFORGE_INBOX           Local folder to watch for PDFs (default: /data/inbox)
    PAGEFORGE_OUTPUT          Output folder for images      (default: /data/output)
    PAGEFORGE_DPI             Render DPI                    (default: 300)
    PAGEFORGE_THRESHOLD       Binarize threshold 0-255      (default: 160)
    PAGEFORGE_ENABLE_UPLOAD   Allow web uploads             (default: true)
    DRIVE_FOLDER_ID           Google Drive folder ID        (enables Drive sync)
    SYNC_INTERVAL_MINUTES     Drive/inbox poll interval     (default: 30)
"""
import io
import json
import os
import re
import sys
import threading
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import fitz  # PyMuPDF
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from PIL import Image, ImageFilter, ImageOps

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_FILE = Path(os.environ.get("PAGEFORGE_CONFIG", "/data/config.json"))

DEFAULTS = {
    "inbox_dir": "/data/inbox",
    "output_dir": "/data/output",
    "dpi": 300,
    "threshold": 160,
    "enable_upload": True,
    "drive_folder_id": "",
    "sync_interval_minutes": 30,
}

ENV_MAP = {
    "PAGEFORGE_INBOX": "inbox_dir",
    "PAGEFORGE_OUTPUT": "output_dir",
    "PAGEFORGE_DPI": ("dpi", int),
    "PAGEFORGE_THRESHOLD": ("threshold", int),
    "PAGEFORGE_ENABLE_UPLOAD": ("enable_upload", lambda v: v.lower() in ("1", "true", "yes")),
    "DRIVE_FOLDER_ID": "drive_folder_id",
    "SYNC_INTERVAL_MINUTES": ("sync_interval_minutes", int),
}


def load_config() -> dict:
    cfg = {**DEFAULTS}
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text()))
        except Exception:
            pass
    for env_key, mapping in ENV_MAP.items():
        val = os.environ.get(env_key)
        if val is None:
            continue
        if isinstance(mapping, tuple):
            key, cast = mapping
            cfg[key] = cast(val)
        else:
            cfg[mapping] = val
    return cfg


def save_config(cfg: dict):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def _state_path(cfg: dict) -> Path:
    return Path(cfg["output_dir"]) / "state.json"


def load_state(cfg: dict) -> dict:
    p = _state_path(cfg)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"processed": {}, "last_sync": None, "last_error": None}


def save_state(state: dict, cfg: dict):
    p = _state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# PDF → images
# ---------------------------------------------------------------------------

def pdf_to_images(pdf_bytes: bytes, stem: str, out_dir: Path, cfg: dict) -> int:
    """Convert each PDF page to a high-contrast B&W PNG. Returns page count."""
    dpi = int(cfg.get("dpi", 300))
    threshold = int(cfg.get("threshold", 160))

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0

    for i, page in enumerate(doc, 1):
        dest = out_dir / f"{stem}_p{i:03d}.png"
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat, alpha=False, colorspace=fitz.csGRAY)
        img = Image.frombytes("L", [pix.width, pix.height], pix.samples)
        img = ImageOps.autocontrast(img, cutoff=2)
        img = img.filter(ImageFilter.SHARPEN)
        img = img.filter(ImageFilter.SHARPEN)
        img = img.point(lambda x: 0 if x < threshold else 255)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        dest.write_bytes(buf.getvalue())
        count += 1

    doc.close()
    return count


# ---------------------------------------------------------------------------
# Google Drive helpers (only imported when Drive is configured)
# ---------------------------------------------------------------------------

def _drive_service(creds_path: Path):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_file(
        str(creds_path),
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _drive_list_pdfs(service, folder_id: str) -> list[dict]:
    res = service.files().list(
        q=f"'{folder_id}' in parents and mimeType='application/pdf' and trashed=false",
        fields="files(id, name, modifiedTime)",
        orderBy="name",
    ).execute()
    return res.get("files", [])


def _drive_download(service, file_id: str) -> bytes:
    from googleapiclient.http import MediaIoBaseDownload
    req = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()


def _drive_delete(service, file_id: str):
    service.files().delete(fileId=file_id).execute()


# ---------------------------------------------------------------------------
# Sync worker (inbox + optional Drive)
# ---------------------------------------------------------------------------

app = FastAPI(title="PageForge")
scheduler = BackgroundScheduler()
_sync_lock = threading.Lock()
_is_syncing = False


def sync_once():
    global _is_syncing
    if not _sync_lock.acquire(blocking=False):
        return

    _is_syncing = True
    cfg = load_config()
    state = load_state(cfg)
    out_dir = Path(cfg["output_dir"])

    try:
        state["last_error"] = None

        # ── Local inbox ──────────────────────────────────────────────────────
        inbox = Path(cfg["inbox_dir"])
        inbox.mkdir(parents=True, exist_ok=True)

        for pdf_path in sorted(inbox.glob("*.pdf")):
            stem = _sanitize_stem(pdf_path.stem)
            if stem in state["processed"]:
                pdf_path.unlink(missing_ok=True)
                continue
            pdf_bytes = pdf_path.read_bytes()
            pages_dir = out_dir / f"{stem}_pages"
            page_count = pdf_to_images(pdf_bytes, stem, pages_dir, cfg)
            state["processed"][stem] = {
                "source": "inbox",
                "processed_at": datetime.now(timezone.utc).isoformat(),
                "page_count": page_count,
            }
            save_state(state, cfg)
            pdf_path.unlink(missing_ok=True)

        # ── Google Drive (optional) ──────────────────────────────────────────
        folder_id = cfg.get("drive_folder_id", "").strip()
        if folder_id:
            creds_path = CONFIG_FILE.parent / "credentials.json"
            if not creds_path.exists():
                raise FileNotFoundError(
                    "Drive folder ID is set but credentials.json not found at "
                    + str(creds_path)
                )
            service = _drive_service(creds_path)
            for f in _drive_list_pdfs(service, folder_id):
                stem = _sanitize_stem(f["name"].rsplit(".", 1)[0])
                if stem in state["processed"]:
                    continue
                pdf_bytes = _drive_download(service, f["id"])
                pages_dir = out_dir / f"{stem}_pages"
                page_count = pdf_to_images(pdf_bytes, stem, pages_dir, cfg)
                try:
                    _drive_delete(service, f["id"])
                except Exception:
                    pass
                state["processed"][stem] = {
                    "source": "drive",
                    "drive_id": f["id"],
                    "processed_at": datetime.now(timezone.utc).isoformat(),
                    "page_count": page_count,
                }
                save_state(state, cfg)

        state["last_sync"] = datetime.now(timezone.utc).isoformat()
        state["last_error"] = None

    except Exception as e:
        state["last_error"] = str(e)

    finally:
        save_state(state, cfg)
        _is_syncing = False
        _sync_lock.release()


def _sanitize_stem(stem: str) -> str:
    return re.sub(r"[^\w\-.]", "_", stem)


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    cfg = load_config()
    Path(cfg["inbox_dir"]).mkdir(parents=True, exist_ok=True)
    Path(cfg["output_dir"]).mkdir(parents=True, exist_ok=True)
    scheduler.add_job(sync_once, "interval", minutes=cfg["sync_interval_minutes"], id="sync")
    scheduler.start()
    threading.Thread(target=sync_once, daemon=True).start()


@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


@app.get("/api/state")
def api_state():
    cfg = load_config()
    state = load_state(cfg)
    out_dir = Path(cfg["output_dir"])
    enriched = {
        stem: {**info, "ready": (out_dir / f"{stem}_pages").exists()}
        for stem, info in state.get("processed", {}).items()
    }
    return {
        **state,
        "processed": enriched,
        "syncing": _is_syncing,
        "config": {
            "sync_interval": cfg["sync_interval_minutes"],
            "drive_enabled": bool(cfg.get("drive_folder_id", "").strip()),
            "enable_upload": cfg.get("enable_upload", True),
            "inbox_dir": cfg["inbox_dir"],
        },
    }


@app.get("/api/config")
def api_get_config():
    return load_config()


@app.post("/api/config")
async def api_set_config(body: dict):
    cfg = load_config()
    allowed = {
        "inbox_dir", "output_dir", "dpi", "threshold",
        "enable_upload", "drive_folder_id", "sync_interval_minutes",
    }
    for k, v in body.items():
        if k in allowed:
            cfg[k] = v
    save_config(cfg)
    try:
        scheduler.reschedule_job(
            "sync", trigger="interval", minutes=int(cfg["sync_interval_minutes"])
        )
    except Exception:
        pass
    return {"ok": True}


@app.post("/api/sync")
def api_sync():
    threading.Thread(target=sync_once, daemon=True).start()
    return {"ok": True}


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    cfg = load_config()
    if not cfg.get("enable_upload", True):
        raise HTTPException(403, "Upload disabled")
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "PDF files only")

    stem = _sanitize_stem(Path(file.filename).stem)
    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(400, "Empty file")

    state = load_state(cfg)
    if stem in state["processed"]:
        return {"ok": True, "stem": stem, "already_processed": True,
                "page_count": state["processed"][stem]["page_count"]}

    def _process():
        try:
            out_dir = Path(cfg["output_dir"])
            pages_dir = out_dir / f"{stem}_pages"
            count = pdf_to_images(pdf_bytes, stem, pages_dir, cfg)
            s = load_state(cfg)
            s["processed"][stem] = {
                "source": "upload",
                "processed_at": datetime.now(timezone.utc).isoformat(),
                "page_count": count,
            }
            save_state(s, cfg)
        except Exception as e:
            s = load_state(cfg)
            s["last_error"] = f"Upload failed ({stem}): {e}"
            save_state(s, cfg)

    threading.Thread(target=_process, daemon=True).start()
    return {"ok": True, "stem": stem, "message": "Processing\u2026"}


@app.get("/api/download/{stem}")
def api_download_stem(stem: str):
    stem = _sanitize_stem(stem)
    cfg = load_config()
    pages_dir = Path(cfg["output_dir"]) / f"{stem}_pages"
    if not pages_dir.exists():
        raise HTTPException(404, "Not found")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for img in sorted(pages_dir.glob("*.png")):
            zf.write(img, img.name)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={stem}.zip"},
    )


@app.get("/api/download-date/{date}")
def api_download_date(date: str):
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        raise HTTPException(400, "Invalid date")
    cfg = load_config()
    out_dir = Path(cfg["output_dir"])
    dirs = sorted(
        d for d in out_dir.iterdir()
        if d.is_dir() and d.name.endswith("_pages") and d.name.startswith(date)
    )
    if not dirs:
        raise HTTPException(404, "No data for this date")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for d in dirs:
            for img in sorted(d.glob("*.png")):
                zf.write(img, img.name)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={date}.zip"},
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def _cli():
    import glob as _glob
    paths = []
    for arg in sys.argv[1:]:
        expanded = _glob.glob(arg)
        if expanded:
            paths.extend(expanded)
        else:
            paths.append(arg)

    cfg = load_config()
    ok = err = 0

    for p in paths:
        pdf_path = Path(p)
        if not pdf_path.exists():
            print(f"[skip] not found: {pdf_path}", file=sys.stderr)
            err += 1
            continue
        if pdf_path.suffix.lower() != ".pdf":
            print(f"[skip] not a PDF: {pdf_path}", file=sys.stderr)
            err += 1
            continue
        stem = _sanitize_stem(pdf_path.stem)
        out_dir = pdf_path.parent / f"{stem}_pages"
        print(f"[>] {pdf_path.name} \u2192 {out_dir}/", end=" ", flush=True)
        try:
            count = pdf_to_images(pdf_path.read_bytes(), stem, out_dir, cfg)
            print(f"({count} page{'s' if count != 1 else ''})")
            ok += 1
        except Exception as e:
            print(f"ERROR: {e}", file=sys.stderr)
            err += 1

    if paths:
        print(f"\nDone: {ok} succeeded, {err} failed.")
    else:
        print("Usage: python main.py input.pdf [more.pdf ...]")
        print("       uvicorn main:app --host 0.0.0.0 --port 8000   (web server)")
        sys.exit(1)


if __name__ == "__main__":
    _cli()


# ---------------------------------------------------------------------------
# Frontend (single-page, three tabs: Files · Upload · Settings)
# ---------------------------------------------------------------------------

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>PageForge</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, -apple-system, sans-serif; background: #0a0f1e; color: #e2e8f0; min-height: 100vh; }

    /* ── Topbar ── */
    .topbar { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: .5rem;
              padding: .85rem 1.5rem; background: #111827; border-bottom: 1px solid #1f2d45;
              position: sticky; top: 0; z-index: 20; }
    .topbar-left  { display: flex; align-items: center; gap: .75rem; }
    .topbar-right { display: flex; align-items: center; gap: .6rem; flex-wrap: wrap; }
    .logo { font-size: 1.05rem; font-weight: 800; letter-spacing: -.02em; color: #f1f5f9; white-space: nowrap; }
    .logo span { color: #818cf8; }
    .badge { font-size: .65rem; background: #1e3a5f; color: #60a5fa; padding: .15rem .5rem;
             border-radius: 999px; font-weight: 600; white-space: nowrap; }
    .sync-lbl { font-size: .78rem; color: #475569; white-space: nowrap; }
    .spin { display: inline-block; animation: spin .9s linear infinite; }
    @keyframes spin { to { transform: rotate(360deg); } }
    .btn { padding: .38rem .9rem; border-radius: 6px; border: none; cursor: pointer;
           font-size: .8rem; font-weight: 600; transition: background .15s, color .15s; white-space: nowrap; }
    .btn-primary { background: #4f46e5; color: #fff; }
    .btn-primary:hover { background: #4338ca; }
    .btn-primary:disabled { background: #1e293b; color: #334155; cursor: not-allowed; }
    .btn-ghost { background: transparent; color: #64748b; border: 1px solid #1f2d45; }
    .btn-ghost:hover { border-color: #4f46e5; color: #a5b4fc; background: #1e1b4b; }

    /* ── Nav tabs ── */
    .nav { display: flex; gap: 0; padding: 0 1.5rem; background: #0d1525;
           border-bottom: 1px solid #1f2d45; overflow-x: auto; }
    .nav-tab { padding: .65rem 1.1rem; font-size: .8rem; font-weight: 600; color: #475569;
               cursor: pointer; border-bottom: 2px solid transparent; white-space: nowrap;
               transition: color .15s, border-color .15s; user-select: none; }
    .nav-tab:hover { color: #94a3b8; }
    .nav-tab.active { color: #818cf8; border-bottom-color: #818cf8; }

    /* ── Layout ── */
    .main { padding: 1.5rem; max-width: 1140px; margin: 0 auto; }
    @media (max-width: 640px) { .main { padding: 1rem .75rem; } }
    section { display: none; }
    section.active { display: block; }

    /* ── Alerts ── */
    .alert { border-radius: 10px; padding: 1rem 1.25rem; margin-bottom: 1.25rem; font-size: .83rem; line-height: 1.6; }
    .alert-info  { background: #130f2a; border: 1px solid #3b0764; color: #a78bfa; }
    .alert-error { background: #1a0a0a; border: 1px solid #7f1d1d; color: #fca5a5; word-break: break-word; }
    .alert h3 { font-size: .85rem; font-weight: 700; margin-bottom: .5rem; }
    .alert ol { margin-left: 1.1rem; }
    .alert li { color: #94a3b8; margin-bottom: .3rem; }
    code { background: #0a0f1e; padding: .1rem .4rem; border-radius: 4px; font-size: .77rem; color: #c4b5fd; word-break: break-all; }

    /* ── Stats ── */
    .stats { display: grid; grid-template-columns: repeat(5, 1fr); gap: .85rem; margin-bottom: 1.5rem; }
    @media (max-width: 860px) { .stats { grid-template-columns: repeat(3, 1fr); } }
    @media (max-width: 520px) { .stats { grid-template-columns: repeat(2, 1fr); } }
    .stat-card { background: #111827; border: 1px solid #1f2d45; border-radius: 10px; padding: 1rem 1.1rem; }
    @media (max-width: 520px) { .stat-card.wide { grid-column: span 2; } }
    .stat-label { font-size: .65rem; color: #475569; text-transform: uppercase; letter-spacing: .08em; margin-bottom: .35rem; font-weight: 700; }
    .stat-value { font-size: 1.9rem; font-weight: 800; color: #f1f5f9; line-height: 1; }
    @media (max-width: 520px) { .stat-value { font-size: 1.55rem; } }
    .stat-card.wide .stat-value { font-size: .85rem; font-weight: 500; color: #94a3b8; line-height: 1.5; }

    /* ── Calendar ── */
    .panel { background: #111827; border: 1px solid #1f2d45; border-radius: 10px; padding: 1.1rem 1.25rem; margin-bottom: 1.5rem; }
    .panel-hd { font-size: .65rem; font-weight: 700; color: #475569; text-transform: uppercase; letter-spacing: .09em; margin-bottom: 1rem; }
    .cal-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; padding-bottom: .2rem; }
    .year-row { display: flex; align-items: center; gap: .85rem; margin-bottom: .45rem; min-width: max-content; }
    .year-row:last-child { margin-bottom: 0; }
    .year-lbl { font-size: .78rem; font-weight: 700; color: #64748b; width: 2.6rem; flex-shrink: 0; }
    .months-grid { display: flex; gap: .3rem; }
    .mc { width: 2.9rem; height: 2.9rem; border-radius: 7px; display: flex; flex-direction: column;
          align-items: center; justify-content: center; font-size: .63rem;
          background: #0d1525; border: 1px solid #1a2740; color: #2d3f58;
          user-select: none; transition: transform .12s, box-shadow .12s; flex-shrink: 0; cursor: default; }
    .mc.has { color: #e2e8f0; cursor: pointer; }
    .mc.has:hover { transform: scale(1.08); box-shadow: 0 0 0 2px #6366f1; }
    .mc.active { box-shadow: 0 0 0 2px #818cf8 !important; transform: scale(1.08); }
    .mc .mn { font-weight: 700; font-size: .68rem; }
    .mc .md { font-size: .55rem; margin-top: .08rem; opacity: .85; }
    .t1 { background: #052e16; border-color: #14532d; color: #86efac; }
    .t2 { background: #14532d; border-color: #166534; color: #4ade80; }
    .t3 { background: #166534; border-color: #15803d; color: #bbf7d0; }
    .t4 { background: #15803d; border-color: #16a34a; color: #dcfce7; }

    /* ── Table toolbar + table ── */
    .toolbar { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: .5rem; margin-bottom: .75rem; }
    .toolbar-title { font-size: .65rem; font-weight: 700; color: #475569; text-transform: uppercase; letter-spacing: .09em; }
    .ytabs { display: flex; gap: .3rem; flex-wrap: wrap; }
    .ytab { padding: .25rem .7rem; border-radius: 5px; border: 1px solid #1f2d45; background: transparent;
            color: #64748b; cursor: pointer; font-size: .73rem; font-weight: 500; transition: all .15s; }
    .ytab:hover { border-color: #4f46e5; color: #a5b4fc; }
    .ytab.active { border-color: #4f46e5; color: #a5b4fc; background: #1e1b4b; }
    .tbl-wrap { overflow-x: auto; -webkit-overflow-scrolling: touch; border-radius: 10px; }
    table { width: 100%; min-width: 400px; border-collapse: collapse; background: #111827;
            border: 1px solid #1f2d45; border-radius: 10px; overflow: hidden; font-size: .86rem; }
    thead { background: #0d1525; }
    th { text-align: left; padding: .55rem 1.1rem; font-size: .64rem; color: #475569;
         text-transform: uppercase; letter-spacing: .07em; border-bottom: 1px solid #1f2d45; font-weight: 700; white-space: nowrap; }
    td { padding: .8rem 1.1rem; border-bottom: 1px solid #0f1e30; vertical-align: middle; }
    tbody tr:last-child td { border-bottom: none; }
    tbody tr:hover td { background: #162032; }
    .yr-sep td { background: #0a1020 !important; padding: .3rem 1.1rem; font-size: .65rem;
                 font-weight: 700; color: #334155; text-transform: uppercase; letter-spacing: .08em;
                 border-bottom: 1px solid #0f1e30; cursor: default; }
    .date-str { font-weight: 700; font-family: monospace; font-size: .9rem; white-space: nowrap; }
    .date-dow { font-size: .7rem; color: #475569; font-family: system-ui; font-weight: 400; margin-left: .4rem; }
    .date-sub { font-size: .72rem; color: #334155; display: block; margin-top: .12rem; }
    .src-badge { font-size: .62rem; padding: .12rem .45rem; border-radius: 999px; font-weight: 600;
                 background: #0f2744; color: #60a5fa; border: 1px solid #1e3a5f; white-space: nowrap; }
    .src-badge.inbox  { background: #0f2744; color: #60a5fa; border-color: #1e3a5f; }
    .src-badge.drive  { background: #14290e; color: #4ade80; border-color: #1a3d12; }
    .src-badge.upload { background: #2a1a0e; color: #fb923c; border-color: #431f08; }
    .pill { display: inline-block; padding: .18rem .6rem; border-radius: 999px; font-size: .7rem;
            font-weight: 600; background: #052e16; color: #4ade80; border: 1px solid #14532d; white-space: nowrap; }
    .btn-dl { padding: .28rem .8rem; font-size: .73rem; border-radius: 5px; border: 1px solid #1f2d45;
              background: transparent; color: #64748b; cursor: pointer; font-weight: 500; transition: all .15s; white-space: nowrap; }
    .btn-dl:hover { border-color: #4f46e5; color: #a5b4fc; background: #1e1b4b; }
    .empty { text-align: center; padding: 2.5rem 1rem; color: #334155; font-size: .86rem; }

    /* ── Upload tab ── */
    .drop-zone { border: 2px dashed #1f2d45; border-radius: 12px; padding: 3rem 2rem;
                 text-align: center; cursor: pointer; transition: all .2s; margin-bottom: 1.25rem; }
    .drop-zone.over { border-color: #4f46e5; background: #1a1740; }
    .drop-zone-icon { font-size: 2.5rem; margin-bottom: .75rem; }
    .drop-zone-text { font-size: .95rem; color: #64748b; margin-bottom: .4rem; }
    .drop-zone-sub  { font-size: .78rem; color: #334155; }
    #uploadInput { display: none; }
    .upload-list { display: flex; flex-direction: column; gap: .5rem; }
    .upload-item { background: #111827; border: 1px solid #1f2d45; border-radius: 8px;
                   padding: .7rem 1rem; display: flex; align-items: center; justify-content: space-between;
                   gap: .75rem; font-size: .82rem; flex-wrap: wrap; }
    .upload-item-name { font-weight: 600; color: #e2e8f0; flex: 1; min-width: 0; word-break: break-all; }
    .upload-item-status { font-size: .75rem; color: #64748b; white-space: nowrap; }
    .upload-item-status.ok  { color: #4ade80; }
    .upload-item-status.err { color: #f87171; }

    /* ── Settings tab ── */
    .settings-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }
    @media (max-width: 640px) { .settings-grid { grid-template-columns: 1fr; } }
    .field-group { background: #111827; border: 1px solid #1f2d45; border-radius: 10px; padding: 1.1rem 1.25rem; }
    .field-group h3 { font-size: .7rem; font-weight: 700; color: #475569; text-transform: uppercase;
                      letter-spacing: .09em; margin-bottom: 1rem; }
    .field { margin-bottom: .9rem; }
    .field:last-child { margin-bottom: 0; }
    .field label { display: block; font-size: .75rem; font-weight: 600; color: #94a3b8; margin-bottom: .35rem; }
    .field input[type="text"], .field input[type="number"] {
      width: 100%; background: #0a0f1e; border: 1px solid #1f2d45; border-radius: 6px;
      padding: .45rem .75rem; color: #e2e8f0; font-size: .83rem; outline: none;
      transition: border-color .15s; }
    .field input:focus { border-color: #4f46e5; }
    .field input[type="range"] { width: 100%; accent-color: #4f46e5; cursor: pointer; }
    .field .range-val { font-size: .75rem; color: #818cf8; font-weight: 600; margin-top: .2rem; }
    .toggle-row { display: flex; align-items: center; justify-content: space-between; }
    .toggle-row label { margin-bottom: 0; }
    .toggle { position: relative; width: 2.4rem; height: 1.3rem; flex-shrink: 0; }
    .toggle input { opacity: 0; width: 0; height: 0; }
    .toggle-slider { position: absolute; inset: 0; background: #1f2d45; border-radius: 999px;
                     cursor: pointer; transition: background .2s; }
    .toggle-slider::before { content: ''; position: absolute; height: 1rem; width: 1rem; left: .15rem; bottom: .15rem;
                              background: #64748b; border-radius: 50%; transition: .2s; }
    .toggle input:checked + .toggle-slider { background: #4f46e5; }
    .toggle input:checked + .toggle-slider::before { transform: translateX(1.1rem); background: #fff; }
    .settings-actions { margin-top: 1rem; display: flex; gap: .75rem; align-items: center; flex-wrap: wrap; }
    .save-msg { font-size: .78rem; color: #4ade80; display: none; }
    .drive-note { font-size: .75rem; color: #475569; line-height: 1.55; margin-top: .5rem; }
  </style>
</head>
<body>

<div class="topbar">
  <div class="topbar-left">
    <div class="logo">Page<span>Forge</span></div>
    <span class="badge" id="intervalBadge"></span>
  </div>
  <div class="topbar-right">
    <span class="sync-lbl" id="syncStatus"></span>
    <button class="btn btn-primary" id="syncBtn" onclick="triggerSync()">Sync Now</button>
  </div>
</div>

<div class="nav">
  <div class="nav-tab active" data-tab="files"    onclick="switchTab('files')">Files</div>
  <div class="nav-tab"        data-tab="upload"   onclick="switchTab('upload')" id="uploadTab">Upload</div>
  <div class="nav-tab"        data-tab="settings" onclick="switchTab('settings')">Settings</div>
</div>

<div class="main">

  <!-- ── Files tab ─────────────────────────────────────────────── -->
  <section id="tab-files" class="active">
    <div id="setupBox" class="alert alert-info" style="display:none">
      <h3>Getting Started</h3>
      <ol>
        <li>Drop PDFs into the inbox folder (default: <code>/data/inbox</code>) and click <strong>Sync Now</strong></li>
        <li>Or use the <strong>Upload</strong> tab to push PDFs directly from your browser</li>
        <li>Or run from the command line: <code>python main.py document.pdf</code></li>
        <li>To enable Google Drive sync, add your <code>credentials.json</code> and set a Drive Folder ID in <strong>Settings</strong></li>
      </ol>
    </div>
    <div id="errorBar" class="alert alert-error" style="display:none"></div>

    <div class="stats">
      <div class="stat-card"><div class="stat-label">Files</div><div class="stat-value" id="statFiles">&mdash;</div></div>
      <div class="stat-card"><div class="stat-label">Pages</div><div class="stat-value" id="statPages">&mdash;</div></div>
      <div class="stat-card"><div class="stat-label">Days</div><div class="stat-value" id="statDays">&mdash;</div></div>
      <div class="stat-card"><div class="stat-label">Years</div><div class="stat-value" id="statYears">&mdash;</div></div>
      <div class="stat-card wide"><div class="stat-label">Last Sync</div><div class="stat-value" id="statSync">&mdash;</div></div>
    </div>

    <div class="panel" id="calWrap" style="display:none">
      <div class="panel-hd">Coverage &mdash; by Year &amp; Month</div>
      <div class="cal-scroll"><div id="calGrid"></div></div>
    </div>

    <div class="toolbar">
      <div class="toolbar-title" id="tableTitle">All Files</div>
      <div class="ytabs" id="ytabs"></div>
    </div>
    <div class="tbl-wrap">
      <div id="tableWrap"><div class="empty">No files processed yet.</div></div>
    </div>
  </section>

  <!-- ── Upload tab ────────────────────────────────────────────── -->
  <section id="tab-upload">
    <div class="drop-zone" id="dropZone" onclick="document.getElementById('uploadInput').click()"
         ondragover="event.preventDefault();this.classList.add('over')"
         ondragleave="this.classList.remove('over')"
         ondrop="handleDrop(event)">
      <div class="drop-zone-icon">&#128196;</div>
      <div class="drop-zone-text">Drop PDF files here</div>
      <div class="drop-zone-sub">or click to browse</div>
    </div>
    <input type="file" id="uploadInput" accept=".pdf" multiple onchange="handleFiles(this.files)">
    <div class="upload-list" id="uploadList"></div>
  </section>

  <!-- ── Settings tab ──────────────────────────────────────────── -->
  <section id="tab-settings">
    <div class="settings-grid">
      <div class="field-group">
        <h3>Paths</h3>
        <div class="field">
          <label>Inbox directory</label>
          <input type="text" id="cfg-inbox_dir" placeholder="/data/inbox">
        </div>
        <div class="field">
          <label>Output directory</label>
          <input type="text" id="cfg-output_dir" placeholder="/data/output">
        </div>
      </div>

      <div class="field-group">
        <h3>Image Quality</h3>
        <div class="field">
          <label>DPI &mdash; render resolution</label>
          <input type="range" id="cfg-dpi" min="72" max="600" step="1"
                 oninput="document.getElementById('dpi-val').textContent=this.value">
          <div class="range-val"><span id="dpi-val">300</span> DPI</div>
        </div>
        <div class="field">
          <label>Binarize threshold (0&ndash;255)</label>
          <input type="range" id="cfg-threshold" min="0" max="255" step="1"
                 oninput="document.getElementById('thr-val').textContent=this.value">
          <div class="range-val">Threshold: <span id="thr-val">160</span></div>
        </div>
      </div>

      <div class="field-group">
        <h3>Web Interface</h3>
        <div class="field">
          <div class="toggle-row">
            <label>Allow PDF upload via browser</label>
            <label class="toggle">
              <input type="checkbox" id="cfg-enable_upload" checked>
              <span class="toggle-slider"></span>
            </label>
          </div>
        </div>
        <div class="field">
          <label>Sync / poll interval (minutes)</label>
          <input type="number" id="cfg-sync_interval_minutes" min="1" max="1440" value="30">
        </div>
      </div>

      <div class="field-group">
        <h3>Google Drive <span style="font-weight:400;color:#475569;font-size:.75rem;">(optional)</span></h3>
        <div class="field">
          <label>Drive Folder ID</label>
          <input type="text" id="cfg-drive_folder_id" placeholder="Leave blank to disable">
        </div>
        <p class="drive-note">
          Place your GCP service account key at <code>/data/credentials.json</code> and enter
          the folder ID above. Share the Drive folder with the service account as <strong>Editor</strong>.
          PageForge will download new PDFs, convert them, and delete the originals from Drive.
        </p>
      </div>
    </div>

    <div class="settings-actions">
      <button class="btn btn-primary" onclick="saveSettings()">Save Settings</button>
      <span class="save-msg" id="saveMsg">&#10003; Saved</span>
    </div>
  </section>

</div>

<script>
  const MN = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const DN = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
  let gState = null;
  let gFilter = null;

  // ── Tab routing ──────────────────────────────────────────────────────────
  function switchTab(name) {
    document.querySelectorAll('.nav-tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
    document.querySelectorAll('section').forEach(s => s.classList.toggle('active', s.id === 'tab-' + name));
    if (name === 'settings' && gState) loadSettingsForm();
  }

  // ── State fetch ──────────────────────────────────────────────────────────
  function fmtSync(iso) {
    if (!iso) return '\u2014';
    const d = new Date(iso);
    return d.toLocaleDateString(undefined, {month:'short',day:'numeric',year:'numeric'})
         + ' ' + d.toLocaleTimeString(undefined, {hour:'2-digit',minute:'2-digit'});
  }

  function stemDate(stem) {
    const m = stem.match(/^(\d{4})-(\d{2})-(\d{2})/);
    return m ? m[1]+'-'+m[2]+'-'+m[3] : null;
  }

  function groupByDate(processed) {
    const bd = {};
    for (const [stem, info] of Object.entries(processed)) {
      const date = stemDate(stem);
      if (!date) continue;
      if (!bd[date]) bd[date] = {images: 0, stems: [], processed_at: info.processed_at};
      bd[date].images += (info.page_count || 1);
      bd[date].stems.push(stem);
    }
    return bd;
  }

  function render(s) {
    gState = s;
    const cfg = s.config || {};
    document.getElementById('intervalBadge').textContent = 'every ' + cfg.sync_interval + 'min';
    document.getElementById('statSync').textContent = fmtSync(s.last_sync);

    const hasData = Object.keys(s.processed || {}).length > 0;
    document.getElementById('setupBox').style.display = hasData ? 'none' : '';

    const err = document.getElementById('errorBar');
    if (s.last_error) { err.style.display=''; err.textContent = '\u26a0\ufe0f  ' + s.last_error; }
    else err.style.display = 'none';

    const btn = document.getElementById('syncBtn');
    const sl  = document.getElementById('syncStatus');
    if (s.syncing) {
      btn.disabled = true;
      sl.innerHTML = '<span class="spin">\u21bb</span>\u00a0Syncing\u2026';
    } else { btn.disabled = false; sl.textContent = ''; }

    // Show/hide upload tab
    document.getElementById('uploadTab').style.display = cfg.enable_upload === false ? 'none' : '';

    const allStems = Object.keys(s.processed || {});
    const byDate   = groupByDate(s.processed || {});
    const dates    = Object.keys(byDate).sort();
    const years    = [...new Set(dates.map(d => d.slice(0,4)))].sort();

    document.getElementById('statFiles').textContent  = allStems.length.toLocaleString();
    document.getElementById('statPages').textContent  = Object.values(s.processed || {}).reduce((a, v) => a + (v.page_count||0), 0).toLocaleString();
    document.getElementById('statDays').textContent   = dates.length;
    document.getElementById('statYears').textContent  = years.length || '\u2014';

    if (dates.length) { renderCalendar(byDate, years); } else { document.getElementById('calWrap').style.display = 'none'; }
    renderYearTabs(years);
    renderTable(s.processed || {}, byDate, dates);
  }

  function renderCalendar(byDate, years) {
    document.getElementById('calWrap').style.display = '';
    const mCount = {};
    for (const d of Object.keys(byDate)) { const ym = d.slice(0,7); mCount[ym] = (mCount[ym]||0) + 1; }
    const maxD = Math.max(...Object.values(mCount));
    let html = '';
    for (const year of [...years].reverse()) {
      html += '<div class="year-row"><div class="year-lbl">'+year+'</div><div class="months-grid">';
      for (let m = 1; m <= 12; m++) {
        const mm = String(m).padStart(2,'0'), ym = year+'-'+mm, cnt = mCount[ym]||0;
        const isAct = gFilter && gFilter.year===year && gFilter.month===mm;
        let tier = '';
        if (cnt > 0) { const p = cnt/maxD; tier = p<.25?'t1':p<.5?'t2':p<.75?'t3':'t4'; }
        html += '<div class="mc '+(cnt?'has '+tier:'')+(isAct?' active':'')+'"'
              + (cnt?' data-year="'+year+'" data-month="'+mm+'" onclick="clickMC(this)"':'')
              + ' title="'+ym+(cnt?': '+cnt+' day'+(cnt!==1?'s':''):': no data')+'">'
              + '<span class="mn">'+MN[m-1]+'</span>'
              + (cnt?'<span class="md">'+cnt+'d</span>':'')
              + '</div>';
      }
      html += '</div></div>';
    }
    document.getElementById('calGrid').innerHTML = html;
  }

  function renderYearTabs(years) {
    let html = '<button class="ytab'+(gFilter?'':' active')+'" onclick="clickYear(this)">All</button>';
    for (const y of [...years].reverse())
      html += '<button class="ytab'+(gFilter&&gFilter.year===y&&!gFilter.month?' active':'')+'" data-year="'+y+'" onclick="clickYear(this)">'+y+'</button>';
    document.getElementById('ytabs').innerHTML = html;
  }

  function renderTable(processed, byDate, allDates) {
    let dates = allDates;
    if (gFilter) {
      const pfx = gFilter.month ? gFilter.year+'-'+gFilter.month : gFilter.year;
      dates = allDates.filter(d => d.startsWith(pfx));
    }

    // non-date stems
    const nonDate = Object.keys(processed).filter(s => !stemDate(s));

    let title = 'All Files \u2014 '+Object.keys(processed).length+' total';
    if (gFilter) {
      if (gFilter.month) title = MN[parseInt(gFilter.month)-1]+' '+gFilter.year+' \u2014 '+dates.length+' day'+(dates.length!==1?'s':'');
      else title = gFilter.year+' \u2014 '+dates.length+' day'+(dates.length!==1?'s':'');
    }
    document.getElementById('tableTitle').textContent = title;

    if (!dates.length && !nonDate.length) {
      document.getElementById('tableWrap').innerHTML = '<div class="empty">No records for this period.</div>';
      return;
    }

    const sorted = [...dates].reverse();
    let rows = '';
    let prevYear = null;
    const multiYear = !gFilter || !gFilter.month;

    for (const date of sorted) {
      const yr = date.slice(0,4);
      if (multiYear && yr !== prevYear) {
        if (prevYear) rows += '</tbody>';
        rows += '<tbody><tr class="yr-sep"><td colspan="4">'+yr+'</td></tr>';
        prevYear = yr;
      }
      const info = byDate[date];
      const jsD  = new Date(date+'T12:00:00');
      const dow  = DN[jsD.getDay()];
      // find any source for this date
      const stemSources = info.stems.map(s => processed[s]?.source || 'inbox');
      const src = [...new Set(stemSources)].join(', ');
      rows += '<tr>'
        + '<td><span class="date-str">'+date+'</span><span class="date-dow">'+dow+'</span>'
        + '<span class="date-sub">'+MN[jsD.getMonth()]+' '+jsD.getDate()+', '+yr+'</span></td>'
        + '<td><span class="src-badge '+stemSources[0]+'">'+src+'</span></td>'
        + '<td><span class="pill">'+info.images+' page'+(info.images!==1?'s':'')+'</span></td>'
        + '<td><button class="btn-dl" data-date="'+date+'" onclick="dlDate(this.dataset.date)">&#8595; ZIP</button></td>'
        + '</tr>';
    }
    if (multiYear && prevYear) rows += '</tbody>';
    else rows = '<tbody>'+rows+'</tbody>';

    // non-date files
    if (!gFilter && nonDate.length) {
      rows += '<tbody><tr class="yr-sep"><td colspan="4">Other files</td></tr>';
      for (const stem of nonDate.sort()) {
        const info = processed[stem];
        const src  = info?.source || 'inbox';
        rows += '<tr>'
          + '<td><span class="date-str" style="font-size:.8rem">'+stem+'</span></td>'
          + '<td><span class="src-badge '+src+'">'+src+'</span></td>'
          + '<td><span class="pill">'+(info?.page_count||'?')+' page'+((info?.page_count||0)!==1?'s':'')+'</span></td>'
          + '<td><button class="btn-dl" data-stem="'+stem+'" onclick="dlStem(this.dataset.stem)">&#8595; ZIP</button></td>'
          + '</tr>';
      }
      rows += '</tbody>';
    }

    document.getElementById('tableWrap').innerHTML =
      '<table><thead><tr><th>Name</th><th>Source</th><th>Pages</th><th></th></tr></thead>'+rows+'</table>';
  }

  function clickMC(el) {
    const {year, month} = el.dataset;
    gFilter = (gFilter && gFilter.year===year && gFilter.month===month) ? null : {year, month};
    render(gState);
  }
  function clickYear(el) {
    const year = el.dataset.year;
    if (!year) gFilter = null;
    else if (gFilter && gFilter.year===year && !gFilter.month) gFilter = null;
    else gFilter = {year};
    render(gState);
  }

  async function loadState() {
    const s = await (await fetch('/api/state')).json();
    render(s);
    return s;
  }

  async function triggerSync() {
    document.getElementById('syncBtn').disabled = true;
    document.getElementById('syncStatus').innerHTML = '<span class="spin">\u21bb</span>\u00a0Syncing\u2026';
    await fetch('/api/sync', {method:'POST'});
    const t = setInterval(async () => { const s = await loadState(); if (!s.syncing) clearInterval(t); }, 1500);
  }

  function dlDate(date) { window.location = '/api/download-date/' + date; }
  function dlStem(stem) { window.location = '/api/download/' + encodeURIComponent(stem); }

  // ── Upload ───────────────────────────────────────────────────────────────
  function handleDrop(e) {
    e.preventDefault();
    document.getElementById('dropZone').classList.remove('over');
    handleFiles(e.dataTransfer.files);
  }

  function handleFiles(files) {
    for (const file of files) {
      if (!file.name.toLowerCase().endsWith('.pdf')) continue;
      uploadFile(file);
    }
  }

  async function uploadFile(file) {
    const list = document.getElementById('uploadList');
    const item = document.createElement('div');
    item.className = 'upload-item';
    item.innerHTML = '<span class="upload-item-name">'+escHtml(file.name)+'</span>'
                   + '<span class="upload-item-status">Uploading\u2026</span>';
    list.prepend(item);
    const status = item.querySelector('.upload-item-status');

    try {
      const fd = new FormData();
      fd.append('file', file);
      const res = await fetch('/api/upload', {method: 'POST', body: fd});
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || 'Upload failed');
      if (data.already_processed) {
        status.textContent = 'Already processed ('+data.page_count+' pages)';
        status.className = 'upload-item-status ok';
      } else {
        status.textContent = 'Processing\u2026';
        // Poll until done
        const stem = data.stem;
        const poll = setInterval(async () => {
          const s = await (await fetch('/api/state')).json();
          if (s.processed[stem]) {
            clearInterval(poll);
            const pc = s.processed[stem].page_count;
            status.textContent = '\u2713 Done \u2014 '+pc+' page'+(pc!==1?'s':'');
            status.className = 'upload-item-status ok';
            render(s);
          }
        }, 1500);
      }
    } catch(e) {
      status.textContent = 'Error: ' + e.message;
      status.className = 'upload-item-status err';
    }
  }

  function escHtml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

  // ── Settings ─────────────────────────────────────────────────────────────
  async function loadSettingsForm() {
    const cfg = await (await fetch('/api/config')).json();
    document.getElementById('cfg-inbox_dir').value = cfg.inbox_dir || '';
    document.getElementById('cfg-output_dir').value = cfg.output_dir || '';
    const dpiEl = document.getElementById('cfg-dpi');
    dpiEl.value = cfg.dpi || 300;
    document.getElementById('dpi-val').textContent = dpiEl.value;
    const thrEl = document.getElementById('cfg-threshold');
    thrEl.value = cfg.threshold || 160;
    document.getElementById('thr-val').textContent = thrEl.value;
    document.getElementById('cfg-enable_upload').checked = cfg.enable_upload !== false;
    document.getElementById('cfg-sync_interval_minutes').value = cfg.sync_interval_minutes || 30;
    document.getElementById('cfg-drive_folder_id').value = cfg.drive_folder_id || '';
  }

  async function saveSettings() {
    const body = {
      inbox_dir:             document.getElementById('cfg-inbox_dir').value.trim(),
      output_dir:            document.getElementById('cfg-output_dir').value.trim(),
      dpi:                   parseInt(document.getElementById('cfg-dpi').value),
      threshold:             parseInt(document.getElementById('cfg-threshold').value),
      enable_upload:         document.getElementById('cfg-enable_upload').checked,
      sync_interval_minutes: parseInt(document.getElementById('cfg-sync_interval_minutes').value),
      drive_folder_id:       document.getElementById('cfg-drive_folder_id').value.trim(),
    };
    await fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    const msg = document.getElementById('saveMsg');
    msg.style.display = 'inline';
    setTimeout(() => msg.style.display = 'none', 2500);
    loadState();
  }

  loadState();
  setInterval(loadState, 15000);
</script>
</body>
</html>"""
