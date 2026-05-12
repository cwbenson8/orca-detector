# ── Stage 1: orcAI + app dependencies ────────────────────────────────────────
FROM python:3.11-slim AS base

# System deps for librosa / audio processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv (fast package manager used by orcAI)
RUN curl -Ls https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

# Install orcAI via uv tool (installs orcai CLI globally)
RUN uv tool install git+https://github.com/ethz-tb/orcAI.git --python 3.11
ENV PATH="/root/.local/share/uv/tools/orcai/bin:$PATH"

# ── Stage 2: Python backend ───────────────────────────────────────────────────
WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/

# Temp directory for job working files
RUN mkdir -p /tmp/orca-detector

# ── Runtime ───────────────────────────────────────────────────────────────────
ENV GCS_BUCKET=noaa-passive-bioacoustic
ENV TEMP_DIR=/tmp/orca-detector
ENV PORT=8080

EXPOSE 8080

# FastAPI serves both API and the static HTML frontend
CMD ["uvicorn", "backend.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8080", \
     "--workers", "2", \
     "--log-level", "info"]
