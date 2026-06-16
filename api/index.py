#!/usr/bin/env python3
"""
api.py
------
FastAPI service that:
  1. Receives a Vercel Blob URL pointing to a zip of the input folder
  2. Downloads + extracts the zip
  3. Runs the Figma → HTML prototype pipeline
  4. Deploys the output to a new Netlify site
  5. Returns the live Netlify URL

Expected zip structure (either flat or in a subfolder):
  input.zip/
    figma_file.json          ← required  (any .json with a "document" key)
    images/                  ← optional  (pre-exported PNGs skip Figma API calls)
      frame_<id>.png
      ...

Environment variables (loaded from .env automatically):
  NETLIFY_API_TOKEN          ← required
  FIGMA_PERSONAL_ACCESS_TOKEN← optional fallback when not supplied in request body
"""

import asyncio, io, json, os, sys, time, zipfile
from concurrent.futures import ThreadPoolExecutor

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Make the repo root importable when running from inside api/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from figma_to_prototype import (
    load_env,
    extract_file_key,
    fetch_figma_json,
    extract_frames,
    extract_interactions,
    detect_entry_frame,
    download_images,
    build_html,
    validate,
)

# ── Startup ───────────────────────────────────────────────────────────────────

load_env()  # pull .env into os.environ before anything reads it

app = FastAPI(title="Figma Prototype API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)

_executor = ThreadPoolExecutor(max_workers=4)

# ── Request / Response schemas ────────────────────────────────────────────────


class ProcessRequest(BaseModel):
    figma_url: str
    """Full Figma design/prototype URL, e.g. https://www.figma.com/design/ABC123/Name"""

    figma_token: str = ""
    """Figma personal access token. Falls back to FIGMA_PERSONAL_ACCESS_TOKEN env var if omitted."""



class ProcessResponse(BaseModel):
    url: str
    site_id: str
    deploy_id: str
    screens: int
    nav_hotspots: int
    toggles: int
    timers: int


class UploadHTMLResponse(BaseModel):
    url: str
    site_id: str
    deploy_id: str


# ── Pipeline helpers (sync, run in thread pool) ───────────────────────────────


def _fetch_and_run_pipeline(
    figma_url: str,
    figma_token: str,
    output_dir: str,
) -> dict:
    """Fetch Figma JSON + images, build HTML prototype. Returns stats dict."""
    file_key = extract_file_key(figma_url)
    figma_data = fetch_figma_json(file_key, figma_token)

    frames = extract_frames(figma_data)
    if not frames:
        raise ValueError("No frames found in the Figma file")

    hotspots, toggles, timeouts = extract_interactions(figma_data, set(frames.keys()))
    entry_id = detect_entry_frame(frames, hotspots)

    img_dir = os.path.join(output_dir, "images")
    image_map = download_images(frames.keys(), file_key, figma_token, img_dir)

    html = build_html(frames, hotspots, toggles, timeouts, image_map, entry_id)
    os.makedirs(output_dir, exist_ok=True)
    with open(os.path.join(output_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)

    issues = validate(frames, hotspots, toggles, timeouts, image_map, html)
    if issues:
        raise ValueError(f"Prototype validation failed: {'; '.join(issues[:5])}")

    return {
        "screens": len(frames),
        "nav_hotspots": sum(len(v) for v in hotspots.values()),
        "toggles": sum(len(v) for v in toggles.values()),
        "timers": len(timeouts),
    }


def _zip_directory(directory: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(directory):
            for filename in files:
                filepath = os.path.join(root, filename)
                arcname = os.path.relpath(filepath, directory)
                zf.write(filepath, arcname)
    return buf.getvalue()


# ── Netlify deployment (async) ────────────────────────────────────────────────


async def _deploy_to_netlify(output_dir: str) -> dict:
    """
    1. Create a fresh Netlify site
    2. Deploy the output directory as a zip
    3. Poll until state == 'ready'
    Returns { url, site_id, deploy_id }
    """
    token = os.environ.get("NETLIFY_API_TOKEN") or os.environ.get("NETLIFY_TOKEN")
    if not token:
        raise HTTPException(500, "NETLIFY_API_TOKEN is not configured")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        # 1. Create site
        r = await client.post(
            "https://api.netlify.com/api/v1/sites",
            headers=headers,
            json={},
        )
        if r.status_code not in (200, 201):
            raise HTTPException(502, f"Netlify site creation failed ({r.status_code}): {r.text}")
        site = r.json()
        site_id: str = site["id"]
        subdomain: str = site["subdomain"]

        # 2. Zip output and deploy
        loop = asyncio.get_event_loop()
        zip_bytes = await loop.run_in_executor(_executor, _zip_directory, output_dir)

        r = await client.post(
            f"https://api.netlify.com/api/v1/sites/{site_id}/deploys",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/zip",
            },
            content=zip_bytes,
            timeout=120.0,
        )
        if r.status_code not in (200, 201):
            raise HTTPException(502, f"Netlify deploy upload failed ({r.status_code}): {r.text}")
        deploy = r.json()
        deploy_id: str = deploy["id"]

        # 3. Poll until ready (max ~2 min, 3s intervals)
        for _ in range(40):
            await asyncio.sleep(3)
            r = await client.get(
                f"https://api.netlify.com/api/v1/deploys/{deploy_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            payload = r.json()
            state = payload.get("state", "")
            if state == "ready":
                break
            if state == "error":
                msg = payload.get("error_message", "unknown error")
                raise HTTPException(502, f"Netlify deploy failed: {msg}")
        else:
            raise HTTPException(504, "Netlify deploy timed out after 2 minutes")

    return {
        "url": f"https://{subdomain}.netlify.app",
        "site_id": site_id,
        "deploy_id": deploy_id,
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────


@app.post("/process", response_model=ProcessResponse)
async def process(req: ProcessRequest):
    """
    Full pipeline:
      figma_url + figma_token → fetch JSON from Figma API → download frame images
      → build HTML prototype → deploy to Netlify → return live URL + stats
    """
    import tempfile

    figma_token = req.figma_token or os.environ.get("FIGMA_PERSONAL_ACCESS_TOKEN", "")
    if not figma_token:
        raise HTTPException(400, "figma_token is required (or set FIGMA_PERSONAL_ACCESS_TOKEN env var)")

    try:
        extract_file_key(req.figma_url)  # validate URL format early
    except ValueError as e:
        raise HTTPException(400, str(e))

    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = os.path.join(tmpdir, "output")
        loop = asyncio.get_event_loop()
        try:
            stats = await loop.run_in_executor(
                _executor,
                _fetch_and_run_pipeline,
                req.figma_url,
                figma_token,
                output_dir,
            )
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(422, str(e))
        except RuntimeError as e:
            raise HTTPException(502, str(e))

        deploy_info = await _deploy_to_netlify(output_dir)

    return ProcessResponse(
        url=deploy_info["url"],
        site_id=deploy_info["site_id"],
        deploy_id=deploy_info["deploy_id"],
        **stats,
    )


@app.post("/upload-html", response_model=UploadHTMLResponse)
async def upload_html(
    file: UploadFile | None = File(default=None),
    blob_url: str | None = Form(default=None),
):
    """
    Direct HTML deploy — no Figma processing.
    Supply exactly one of:
      • file     — multipart .html file upload
      • blob_url — Vercel Blob URL that returns an .html file when downloaded
    Returns the live Netlify URL.
    """
    import tempfile

    if file is None and not blob_url:
        raise HTTPException(400, "Provide either a file upload or a blob_url")
    if file is not None and blob_url:
        raise HTTPException(400, "Provide only one of file or blob_url, not both")

    if file is not None:
        if not file.filename or not file.filename.lower().endswith(".html"):
            raise HTTPException(400, "Only .html files are accepted")
        contents = await file.read()
        if not contents:
            raise HTTPException(400, "Uploaded file is empty")
    else:
        async with httpx.AsyncClient(timeout=120.0) as client:
            try:
                r = await client.get(blob_url)
                r.raise_for_status()
            except httpx.HTTPStatusError as e:
                raise HTTPException(400, f"Blob download failed ({e.response.status_code}): {blob_url}")
            except httpx.RequestError as e:
                raise HTTPException(400, f"Blob download error: {e}")
        contents = r.content
        if not contents:
            raise HTTPException(400, "File at blob_url is empty")

    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = os.path.join(tmpdir, "output")
        os.makedirs(output_dir, exist_ok=True)

        with open(os.path.join(output_dir, "index.html"), "wb") as f:
            f.write(contents)

        deploy_info = await _deploy_to_netlify(output_dir)

    return UploadHTMLResponse(
        url=deploy_info["url"],
        site_id=deploy_info["site_id"],
        deploy_id=deploy_info["deploy_id"],
    )


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "netlify_token_set": bool(
            os.environ.get("NETLIFY_API_TOKEN") or os.environ.get("NETLIFY_TOKEN")
        ),
        "figma_token_set": bool(os.environ.get("FIGMA_PERSONAL_ACCESS_TOKEN")),
    }
