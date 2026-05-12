#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy-gcp.sh  —  Deploy OrcaWatch to a GCP VM
#
# Usage:
#   chmod +x scripts/deploy-gcp.sh
#   ./scripts/deploy-gcp.sh [--project my-gcp-project] [--zone us-central1-a]
#
# Prerequisites:
#   - gcloud CLI installed and authenticated (gcloud auth login)
#   - Docker installed locally (for the build step)
#   - A GCP project with Compute Engine API enabled
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Defaults (override with flags or env vars) ────────────────────────────────
PROJECT="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
ZONE="${GCP_ZONE:-us-central1-a}"
MACHINE_TYPE="${MACHINE_TYPE:-n2-standard-4}"   # 4 vCPU / 16 GB — good for orcAI
DISK_SIZE="${DISK_SIZE:-50GB}"
VM_NAME="orcawatch-vm"
IMAGE_NAME="orcawatch"
REGION="${ZONE%-*}"                              # strip trailing -a/-b/-c

# Parse flags
while [[ $# -gt 0 ]]; do
  case $1 in
    --project) PROJECT="$2"; shift 2;;
    --zone)    ZONE="$2"; REGION="${ZONE%-*}"; shift 2;;
    --machine) MACHINE_TYPE="$2"; shift 2;;
    *) echo "Unknown flag: $1"; exit 1;;
  esac
done

if [[ -z "$PROJECT" ]]; then
  echo "❌ No GCP project set. Pass --project or run: gcloud config set project YOUR_PROJECT"
  exit 1
fi

REGISTRY="gcr.io/${PROJECT}/${IMAGE_NAME}"

echo ""
echo "┌─────────────────────────────────────────────────────┐"
echo "│  OrcaWatch — GCP Deployment                         │"
echo "├─────────────────────────────────────────────────────┤"
echo "│  Project  : ${PROJECT}"
echo "│  Zone     : ${ZONE}"
echo "│  Machine  : ${MACHINE_TYPE}"
echo "│  Image    : ${REGISTRY}"
echo "└─────────────────────────────────────────────────────┘"
echo ""

# ── Step 1: Build & push Docker image ─────────────────────────────────────────
echo "🐳 Building Docker image…"
cd "$(dirname "$0")/.."   # repo root
docker build -t "${REGISTRY}:latest" .

echo "📤 Pushing image to GCR…"
docker push "${REGISTRY}:latest"

# ── Step 2: Create VM (skip if already exists) ────────────────────────────────
if gcloud compute instances describe "${VM_NAME}" --zone="${ZONE}" --project="${PROJECT}" &>/dev/null; then
  echo "ℹ  VM '${VM_NAME}' already exists — updating container image instead."
  gcloud compute instances update-container "${VM_NAME}" \
    --zone="${ZONE}" \
    --project="${PROJECT}" \
    --container-image="${REGISTRY}:latest"
else
  echo "🖥  Creating VM '${VM_NAME}'…"
  gcloud compute instances create-with-container "${VM_NAME}" \
    --project="${PROJECT}" \
    --zone="${ZONE}" \
    --machine-type="${MACHINE_TYPE}" \
    --boot-disk-size="${DISK_SIZE}" \
    --container-image="${REGISTRY}:latest" \
    --container-env="GCS_BUCKET=noaa-passive-bioacoustic,TEMP_DIR=/tmp/orca-detector,PORT=8080" \
    --tags="orcawatch-server" \
    --scopes="storage-read-only,cloud-platform" \
    --image-family="cos-stable" \
    --image-project="cos-cloud"
fi

# ── Step 3: Firewall rule ─────────────────────────────────────────────────────
if ! gcloud compute firewall-rules describe "allow-orcawatch" --project="${PROJECT}" &>/dev/null; then
  echo "🔒 Creating firewall rule for port 8080…"
  gcloud compute firewall-rules create "allow-orcawatch" \
    --project="${PROJECT}" \
    --allow="tcp:8080" \
    --target-tags="orcawatch-server" \
    --description="OrcaWatch web UI and API"
fi

# ── Step 4: Print access URL ──────────────────────────────────────────────────
echo ""
EXTERNAL_IP=$(gcloud compute instances describe "${VM_NAME}" \
  --zone="${ZONE}" \
  --project="${PROJECT}" \
  --format="get(networkInterfaces[0].accessConfigs[0].natIP)")

echo "✅ Deployment complete!"
echo ""
echo "   Open in browser:  http://${EXTERNAL_IP}:8080"
echo "   API docs:         http://${EXTERNAL_IP}:8080/docs"
echo ""
echo "   To SSH into the VM:"
echo "   gcloud compute ssh ${VM_NAME} --zone=${ZONE} --project=${PROJECT}"
echo ""
echo "   To view container logs:"
echo "   gcloud compute ssh ${VM_NAME} --zone=${ZONE} --project=${PROJECT} \\"
echo "     --command='docker logs \$(docker ps -q) -f'"
echo ""
