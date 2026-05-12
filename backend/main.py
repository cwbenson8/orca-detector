"""
Orca Detector Backend
FastAPI app that bridges the NOAA GCS bucket with orcAI predictions.
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from backend.gcs_client import GCSClient
from backend.job_manager import JobManager, JobStatus

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Windows requires ProactorEventLoop for asyncio subprocesses
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

app = FastAPI(title="Orca Detector API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BUCKET_NAME = os.getenv("GCS_BUCKET", "noaa-passive-bioacoustic")

# Windows-safe temp directory
_default_temp = str(Path(tempfile.gettempdir()) / "orca-detector")
TEMP_DIR = Path(os.getenv("TEMP_DIR", _default_temp))
TEMP_DIR.mkdir(parents=True, exist_ok=True)

# Path to orcai executable — override with ORCAI_BIN env var
ORCAI_BIN = os.getenv("ORCAI_BIN", r"C:\Users\jones\.local\bin\orcai.exe")

gcs = GCSClient(BUCKET_NAME)
jobs = JobManager()


# ─── GCS Browser endpoints ────────────────────────────────────────────────────

@app.get("/api/gcs/stations")
async def list_stations():
    """List top-level station prefixes in the bucket."""
    try:
        stations = await gcs.list_stations()
        return {"stations": stations}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/gcs/browse")
async def browse(prefix: str = ""):
    """
    List objects and common prefixes under a given prefix.
    Returns folders (prefixes) and audio files separately.
    """
    try:
        result = await gcs.browse(prefix)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/gcs/file-info")
async def file_info(path: str):
    """Get metadata for a specific GCS object."""
    try:
        info = await gcs.get_file_info(path)
        return info
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))


# ─── Job / Prediction endpoints ───────────────────────────────────────────────

class PredictRequest(BaseModel):
    gcs_path: str          # e.g. "pam/site_001/2023/file.wav"
    filter_min_dur: float = 0.05   # seconds
    filter_max_dur: float = 30.0   # seconds


@app.post("/api/jobs/predict")
async def start_prediction(req: PredictRequest):
    """Download a GCS audio file and kick off an orcAI prediction job."""
    job_id = str(uuid.uuid4())[:8]
    jobs.create(job_id, req.gcs_path)

    # Fire off the prediction pipeline in the background
    asyncio.create_task(
        _run_prediction_pipeline(job_id, req.gcs_path, req.filter_min_dur, req.filter_max_dur)
    )
    return {"job_id": job_id, "status": "queued"}


@app.get("/api/jobs/map-data")
async def get_map_data():
    """
    Return all completed jobs with their station coordinates for the map tab.
    """
    results = []
    for job in jobs.list_all():
        if job["status"] != JobStatus.DONE:
            continue
        gcs_path = job.get("gcs_path", "")
        prefix = gcs_path.split("/")[0].lower() if gcs_path else ""

        coords = _user_coords.get(prefix) or KNOWN_STATIONS.get(prefix)
        results.append({
            "job_id": job["job_id"],
            "gcs_path": gcs_path,
            "filename": gcs_path.split("/")[-1] if gcs_path else "—",
            "detection_count": job.get("detection_count", 0),
            "created_at": job.get("created_at", ""),
            "station_prefix": prefix,
            "lat": coords["lat"] if coords else None,
            "lon": coords["lon"] if coords else None,
            "station_name": coords["name"] if coords else prefix,
            "has_coords": coords is not None,
        })
    return {"jobs": results}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    """Poll job status and results."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.get("/api/jobs")
async def list_jobs():
    """List all jobs (most recent first)."""
    return {"jobs": jobs.list_all()}


@app.get("/api/jobs/{job_id}/annotations")
async def get_annotations(job_id: str):
    """Return parsed annotation results for a completed job."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != JobStatus.DONE:
        raise HTTPException(status_code=409, detail="Job not complete")
    return {"annotations": job.get("annotations", [])}


@app.get("/api/jobs/{job_id}/download")
async def download_annotations(job_id: str):
    """Download the raw Audacity-compatible annotation .txt file."""
    job = jobs.get(job_id)
    if not job or job["status"] != JobStatus.DONE:
        raise HTTPException(status_code=404, detail="Annotations not available")

    annotation_path = job.get("annotation_file")
    if not annotation_path or not Path(annotation_path).exists():
        raise HTTPException(status_code=404, detail="Annotation file missing")

    def iterfile():
        with open(annotation_path, "rb") as f:
            yield from f

    filename = Path(annotation_path).name
    return StreamingResponse(
        iterfile(),
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/jobs/{job_id}/snippet/{index}")
async def get_snippet(job_id: str, index: int, padding: float = 0.5):
    """
    Slice a short audio clip around detection[index] and stream it as WAV.
    padding: seconds of context before/after the detection (default 0.5s).
    """
    job = jobs.get(job_id)
    if not job or job["status"] != JobStatus.DONE:
        raise HTTPException(status_code=404, detail="Job not complete")

    annotations = job.get("annotations", [])
    if index < 0 or index >= len(annotations):
        raise HTTPException(status_code=404, detail="Snippet index out of range")

    wav_file = job.get("wav_file")
    if not wav_file or not Path(wav_file).exists():
        raise HTTPException(status_code=404, detail="WAV file no longer available")

    ann = annotations[index]
    start = max(0.0, ann["start"] - padding)
    end = ann["end"] + padding

    # Use ffmpeg to extract the slice into an in-memory bytes buffer
    snippet_bytes = await asyncio.to_thread(
        _extract_snippet, wav_file, start, end
    )

    return StreamingResponse(
        iter([snippet_bytes]),
        media_type="audio/wav",
        headers={"Content-Disposition": f'inline; filename="snippet_{index}.wav"'},
    )


def _extract_snippet(wav_path: str, start: float, end: float) -> bytes:
    """Use ffmpeg to extract a time slice from a WAV, returning raw bytes."""
    import subprocess, io
    duration = end - start
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-t", str(duration),
        "-i", wav_path,
        "-f", "wav",
        "pipe:1",           # output to stdout
    ]
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg snippet extraction failed: {result.stderr.decode(errors='replace')}")
    return result.stdout


@app.get("/api/jobs/{job_id}/download-confirmed")
async def download_confirmed(job_id: str, indices: str = ""):
    """
    Download an Audacity .txt containing only the confirmed (accepted) detections.
    indices: comma-separated list of confirmed annotation indices.
    """
    job = jobs.get(job_id)
    if not job or job["status"] != JobStatus.DONE:
        raise HTTPException(status_code=404, detail="Job not complete")

    annotations = job.get("annotations", [])
    confirmed_indices = set()
    if indices:
        for i in indices.split(","):
            try:
                confirmed_indices.add(int(i.strip()))
            except ValueError:
                pass

    confirmed = [annotations[i] for i in sorted(confirmed_indices) if i < len(annotations)]

    lines = []
    for ann in confirmed:
        lines.append(f"{ann['start']:.6f}\t{ann['end']:.6f}\t{ann['label']}\n")

    content_bytes = "".join(lines).encode("utf-8")
    fname = Path(job.get("annotation_file", "confirmed")).stem + "_confirmed.txt"

    return StreamingResponse(
        iter([content_bytes]),
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


# ─── Station coordinates ──────────────────────────────────────────────────────

# Known NOAA PAM station coordinates extracted from NCEI metadata
# Keyed by GCS path prefix (first folder segment)
# Bounding box centres — good enough for a deployment pin
KNOWN_STATIONS: dict[str, dict] = {
    "adeon": {
        "name": "ADEON — Atlantic Deepwater Ecosystem Observatory Network",
        "lat": 33.24, "lon": -77.26,
        "region": "US Mid/South Atlantic OCS",
        "depth_m": None,
        "ncei_id": "gov.noaa.ncei.pad:ADEON_Raw_Data",
    },
    "nrs": {
        "name": "NRS — NOAA Ocean Noise Reference Station Network",
        "lat": 35.0, "lon": -120.0,
        "region": "Multiple US coastal sites",
        "depth_m": None,
        "ncei_id": "gov.noaa.ngdc.mgg.pad:NRS_Raw_Data",
    },
    "sanctsound": {
        "name": "SanctSound — National Marine Sanctuary Soundscape",
        "lat": 24.5, "lon": -81.8,
        "region": "US National Marine Sanctuaries",
        "depth_m": None,
        "ncei_id": "gov.noaa.ncei.pad:SanctSound",
    },
    "navy": {
        "name": "US Navy PAM Recordings",
        "lat": 32.0, "lon": -77.0,
        "region": "US Atlantic/Pacific ranges",
        "depth_m": None,
        "ncei_id": None,
    },
    "boem": {
        "name": "BOEM — Bureau of Ocean Energy Management",
        "lat": 38.5, "lon": -74.5,
        "region": "US Outer Continental Shelf",
        "depth_m": None,
        "ncei_id": None,
    },
    "md_wea": {
        "name": "MD WEA — Maryland Wind Energy Area",
        "lat": 38.2, "lon": -74.6,
        "region": "Maryland offshore",
        "depth_m": None,
        "ncei_id": None,
    },
}

# User-supplied coordinates (persisted in memory; key = gcs prefix)
_user_coords: dict[str, dict] = {}


@app.get("/api/station/coords")
async def get_station_coords(gcs_path: str):
    """
    Return known or user-supplied coordinates for the station in gcs_path.
    gcs_path: the full GCS object path, e.g. adeon/audio/ble/.../file.flac
    """
    prefix = gcs_path.split("/")[0].lower()

    # User override takes priority
    if prefix in _user_coords:
        return {"source": "user", **_user_coords[prefix]}

    if prefix in KNOWN_STATIONS:
        return {"source": "known", **KNOWN_STATIONS[prefix]}

    return {"source": "unknown", "lat": None, "lon": None, "name": prefix}


@app.post("/api/station/coords")
async def set_station_coords(payload: dict):
    """
    Save user-supplied coordinates for a station prefix.
    Body: { "prefix": "adeon", "lat": 33.0, "lon": -77.0, "name": "My station" }
    """
    prefix = payload.get("prefix", "").lower()
    if not prefix:
        raise HTTPException(status_code=400, detail="prefix required")
    _user_coords[prefix] = {
        "lat": float(payload.get("lat", 0)),
        "lon": float(payload.get("lon", 0)),
        "name": payload.get("name", prefix),
        "region": payload.get("region", ""),
        "depth_m": payload.get("depth_m"),
    }
    return {"ok": True}


# ─── WebSocket for live log streaming ─────────────────────────────────────────

@app.websocket("/ws/jobs/{job_id}/logs")
async def job_logs_ws(websocket: WebSocket, job_id: str):
    """Stream live log lines for a running prediction job."""
    await websocket.accept()
    try:
        async for line in jobs.stream_logs(job_id):
            await websocket.send_text(json.dumps({"log": line}))
        job = jobs.get(job_id)
        await websocket.send_text(json.dumps({"done": True, "status": job["status"]}))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        await websocket.send_text(json.dumps({"error": str(e)}))


# ─── Internal pipeline ────────────────────────────────────────────────────────

async def _run_prediction_pipeline(
    job_id: str, gcs_path: str, filter_min: float, filter_max: float
):
    """
    Full pipeline:
      1. Download WAV from GCS → temp file
      2. Run `orcai predict` → annotation txt
      3. Run `orcai filter-predictions` → filtered txt
      4. Parse annotation txt → structured results
      5. Update job record
    """
    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        # ── Step 1: download ──────────────────────────────────────────────
        jobs.log(job_id, f"⬇ Downloading gs://{BUCKET_NAME}/{gcs_path} …")
        local_wav = job_dir / Path(gcs_path).name
        await gcs.download_file(gcs_path, local_wav)
        jobs.log(job_id, f"✓ Downloaded {local_wav.stat().st_size / 1e6:.1f} MB")

        # ── Step 2: convert FLAC → WAV if needed ────────────────────────
        jobs.set_status(job_id, JobStatus.RUNNING)
        if local_wav.suffix.lower() != ".wav":
            jobs.log(job_id, f"🔄 Converting {local_wav.suffix.upper()} → WAV …")
            wav_path = local_wav.with_suffix(".wav")
            convert_cmd = [
                "ffmpeg", "-y",
                "-i", str(local_wav),
                "-ar", "48000",   # paper §2.2.1: all recordings downsampled to 48 kHz
                # no -ac: orcAI selects channel 1 via --channel 1 (paper methodology)
                str(wav_path),
            ]
            await _stream_subprocess(job_id, convert_cmd, cwd=str(job_dir))
            local_wav.unlink()   # remove original FLAC to save disk
            local_wav = wav_path
            jobs.log(job_id, f"✓ Converted to {wav_path.name}")

        # ── Step 3: orcai predict ─────────────────────────────────────────
        jobs.log(job_id, "🔬 Running orcai predict …")
        predict_cmd = [
            ORCAI_BIN, "predict",
            str(local_wav),
            "--channel", "1",    # paper §2.1.2: channel 1 selected per recording
        ]
        await _stream_subprocess(job_id, predict_cmd, cwd=str(job_dir))

        # orcAI writes output next to the input file — glob to find it
        # regardless of exact model name suffix
        predicted_files = list(job_dir.glob("*_predicted.txt"))
        if not predicted_files:
            all_files = list(job_dir.iterdir())
            jobs.log(job_id, f"  Files in job dir: {[f.name for f in all_files]}")
            raise FileNotFoundError(
                f"No *_predicted.txt annotation file found in {job_dir}"
            )
        raw_annotation = predicted_files[0]
        jobs.log(job_id, f"✓ Predictions written to {raw_annotation.name}")

        # ── Step 4: filter by duration in Python (no CLI needed) ─────────
        jobs.log(job_id, f"🔎 Filtering annotations (min={filter_min}s, max={filter_max}s) …")
        final_annotation = _filter_annotation_file(
            raw_annotation, filter_min, filter_max
        )
        jobs.log(job_id, f"✓ Filtered annotation saved to {final_annotation.name}")
        jobs.log(job_id, f"✓ Final annotations: {final_annotation.name}")

        # ── Step 5: parse ─────────────────────────────────────────────────
        annotations = _parse_annotation_file(final_annotation)
        jobs.log(job_id, f"✅ Done — {len(annotations)} detections found")
        jobs.finish(job_id, annotations=annotations, annotation_file=str(final_annotation), wav_file=str(local_wav))

    except Exception as e:
        jobs.log(job_id, f"❌ Error: {e}")
        jobs.set_status(job_id, JobStatus.FAILED)
        logger.exception(f"Job {job_id} failed")
    finally:
        # WAV is kept for snippet playback — cleaned up when a new job runs
        pass


def _run_subprocess_sync(cmd: list, cwd: str, log_callback) -> None:
    """
    Run a subprocess synchronously, streaming output line-by-line via log_callback.
    Uses subprocess.Popen which works reliably on all platforms including Windows.
    """
    import subprocess
    log_callback(f"  Running: {' '.join(str(c) for c in cmd)}")

    # Force UTF-8 output on Windows — orcAI uses Unicode math symbols that
    # Windows CP1252 codec cannot encode, causing a crash mid-run.
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as e:
        raise RuntimeError(f"Could not find executable: {cmd[0]} — {e}")
    except Exception as e:
        raise RuntimeError(f"Failed to start process: {e}")

    output_lines = []
    for line in proc.stdout:
        stripped = line.rstrip()
        output_lines.append(stripped)
        log_callback(stripped)
    proc.wait()

    if proc.returncode != 0:
        last_output = "\n".join(output_lines[-5:]) if output_lines else "(no output)"
        raise RuntimeError(
            f"Command exited with code {proc.returncode}.\nLast output:\n{last_output}"
        )


async def _stream_subprocess(job_id: str, cmd: list, cwd: str):
    """Wrap synchronous subprocess runner in a thread so it doesn't block the event loop."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        _run_subprocess_sync,
        cmd,
        cwd,
        lambda line: jobs.log(job_id, line),
    )


def _filter_annotation_file(src: Path, min_dur: float, max_dur: float) -> Path:
    """
    Filter an Audacity annotation file by call duration.
    Writes a new *_filtered.txt file next to the source and returns its path.
    """
    out_path = src.with_name(src.stem + "_filtered.txt")
    kept = []
    with open(src, encoding="utf-8", errors="replace") as f:
        for line in f:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                kept.append(line)
                continue
            parts = stripped.split("\t")
            if len(parts) >= 2:
                try:
                    dur = float(parts[1]) - float(parts[0])
                    if min_dur <= dur <= max_dur:
                        kept.append(line)
                except ValueError:
                    kept.append(line)
            else:
                kept.append(line)
    with open(out_path, "w", encoding="utf-8") as f:
        f.writelines(kept)
    return out_path


def _parse_annotation_file(path: Path) -> list[dict]:
    """
    Parse an Audacity-compatible annotation .txt file.
    Format: <start_sec> \\t <end_sec> \\t <label>
    """
    annotations = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 3:
                try:
                    annotations.append({
                        "start": float(parts[0]),
                        "end": float(parts[1]),
                        "label": parts[2],
                        "duration": round(float(parts[1]) - float(parts[0]), 3),
                    })
                except ValueError:
                    continue
    return annotations


# ─── Serve frontend static files ──────────────────────────────────────────────
frontend_dir = Path(__file__).parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(frontend_dir), html=True), name="frontend")
