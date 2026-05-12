# 🐋 OrcaWatch

**A web UI for detecting killer whale calls in NOAA passive acoustic recordings using orcAI.**

Browse the [NOAA passive bioacoustic GCS bucket](https://console.cloud.google.com/storage/browser/noaa-passive-bioacoustic), select audio files, run [orcAI](https://github.com/ethz-tb/orcAI) detections on demand, and explore results — all from a browser.

---

## Architecture

```
NOAA GCS Bucket (noaa-passive-bioacoustic)
        │
        │  google-cloud-storage (anonymous, public)
        ▼
┌─────────────────────────────────────────────┐
│  FastAPI Backend  (Python 3.11)             │
│  ├── GCS Browser   — list/browse/download   │
│  ├── orcAI Runner  — predict + filter       │
│  ├── Job Manager   — status & log streaming │
│  └── Static files — serves frontend/        │
└─────────────────────────────────────────────┘
        │  REST + WebSocket
        ▼
┌─────────────────────────────────────────────┐
│  HTML Frontend  (single-page, no framework) │
│  ├── File Browser panel                     │
│  ├── Live Log viewer (WebSocket)            │
│  ├── Detection Timeline + table             │
│  └── Audacity .txt export                   │
└─────────────────────────────────────────────┘
```

---

## Quick Start — Local

### Prerequisites

- Python 3.11
- [uv](https://docs.astral.sh/uv/) package manager
- orcAI installed:

```bash
uv tool install git+https://github.com/ethz-tb/orcAI.git --python 3.11
```

### Run

```bash
git clone <this-repo>
cd orca-detector
chmod +x scripts/run-local.sh
./scripts/run-local.sh
```

Open **http://localhost:8080** in your browser.

---

## Deploy to GCP VM

### Prerequisites

- [gcloud CLI](https://cloud.google.com/sdk/docs/install) installed and authenticated
- Docker installed locally
- A GCP project with Compute Engine API and Container Registry API enabled

### One-command deploy

```bash
chmod +x scripts/deploy-gcp.sh

# Uses your current gcloud project
./scripts/deploy-gcp.sh

# Or specify explicitly
./scripts/deploy-gcp.sh --project my-gcp-project --zone us-central1-a
```

The script will:
1. Build the Docker image and push it to Google Container Registry
2. Create a GCP VM (n2-standard-4 by default) running Container-Optimized OS
3. Open firewall port 8080
4. Print the public URL

### VM sizing guide

| Use case | Machine type | Notes |
|---|---|---|
| Light exploration | `n1-standard-2` | 2 vCPU / 7.5 GB |
| **Recommended** | `n2-standard-4` | 4 vCPU / 16 GB |
| Batch / large files | `n2-standard-8` | 8 vCPU / 32 GB |
| GPU-accelerated | `n1-standard-4` + `--accelerator=nvidia-tesla-t4` | Needs CUDA setup |

Override with:
```bash
./scripts/deploy-gcp.sh --machine n2-standard-8
```

### Why co-locate with GCS?

The NOAA bucket is in Google's `us-central1` region. Running the VM in the same region means audio file downloads (which can be 100MB–2GB each) happen over Google's internal network — typically 10–50× faster and at no egress cost.

---

## Project Structure

```
orca-detector/
├── backend/
│   ├── main.py          # FastAPI app, API routes, pipeline orchestration
│   ├── gcs_client.py    # GCS browser + downloader (anonymous access)
│   ├── job_manager.py   # In-memory job store + WebSocket log streaming
│   └── requirements.txt
├── frontend/
│   └── index.html       # Single-page app (HTML + CSS + vanilla JS)
├── scripts/
│   ├── deploy-gcp.sh    # GCP VM deployment
│   └── run-local.sh     # Local dev server
├── Dockerfile
└── README.md
```

---

## API Reference

All endpoints are served at the same origin as the frontend.
Interactive docs available at `/docs` (Swagger UI).

### GCS Browser

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/gcs/stations` | List top-level stations in bucket |
| `GET` | `/api/gcs/browse?prefix=...` | List folders + audio files at prefix |
| `GET` | `/api/gcs/file-info?path=...` | Metadata for a specific blob |

### Jobs

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/jobs/predict` | Start a detection job |
| `GET` | `/api/jobs` | List all jobs |
| `GET` | `/api/jobs/{id}` | Get job status + metadata |
| `GET` | `/api/jobs/{id}/annotations` | Parsed detection results |
| `GET` | `/api/jobs/{id}/download` | Download Audacity .txt annotation file |
| `WS` | `/ws/jobs/{id}/logs` | Stream live log lines (WebSocket) |

### `POST /api/jobs/predict` body

```json
{
  "gcs_path": "pam/site_AMAR_001/2023/01/15/audio_20230115T120000.wav",
  "filter_min_dur": 0.05,
  "filter_max_dur": 30.0
}
```

---

## How Detections Work

1. **Download** — the selected WAV/FLAC is streamed from GCS to a temp directory on the VM.
2. **Predict** — `orcai predict <file.wav>` runs the bundled `orcai-V1` ResNet-CNN + LSTM model over the spectrogram. Outputs an Audacity-compatible `.txt` annotation file.
3. **Filter** — `orcai filter-predictions` removes annotations shorter than `filter_min_dur` or longer than `filter_max_dur`.
4. **Parse** — the annotation file (tab-separated `start_sec`, `end_sec`, `label`) is parsed into structured JSON.
5. **Display** — the frontend shows a timeline, table, and per-call-type stats. The raw `.txt` file can be downloaded for use in Audacity.

### orcAI call types (orcai-V1 model)

Trained on herring-feeding killer whales off Iceland. Detects:
- Pulsed calls (discrete call types)
- Whistles
- Herding calls
- Buzzes
- Breathing / tailslaps / prey handling sounds

See the [orcAI paper](https://doi.org/10.1111/mms.70083) for full model details.

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `GCS_BUCKET` | `noaa-passive-bioacoustic` | GCS bucket name |
| `TEMP_DIR` | `/tmp/orca-detector` | Working directory for job files |
| `PORT` | `8080` | Server port |

---

## Notes & Limitations

- **Job storage is in-memory.** Jobs are lost on restart. For a persistent deployment, swap `job_manager.py`'s `_store` dict for SQLite or Redis.
- **No authentication.** The frontend has no login. For a shared deployment, put it behind Identity-Aware Proxy (IAP) or a VPN.
- **One orcAI job at a time** is safe; the job queue serialises work. For parallel jobs, consider a task queue like Celery or Cloud Tasks.
- **Disk space.** WAVs are deleted after prediction, but annotation files are kept in `TEMP_DIR`. Monitor disk usage on long-running deployments.
- **orcAI model is trained on Icelandic killer whales.** Accuracy on other populations may vary; retraining with local data is supported by orcAI's training pipeline.

---

## References

- [orcAI GitHub](https://github.com/ethz-tb/orcAI)
- [orcAI paper — Marine Mammal Science 2025](https://doi.org/10.1111/mms.70083)
- [NOAA Passive Bioacoustic bucket](https://console.cloud.google.com/storage/browser/noaa-passive-bioacoustic)
