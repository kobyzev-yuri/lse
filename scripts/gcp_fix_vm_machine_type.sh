#!/usr/bin/env bash
# Restart LSE GCP VM when start fails due to capacity (change machine type, then start).
# Usage (after gcloud auth login):
#   export PATH="$HOME/google-cloud-sdk/bin:$PATH"
#   ./scripts/gcp_fix_vm_machine_type.sh
#
# Optional overrides:
#   GCP_INSTANCE=instance-20260316-154106 GCP_ZONE=us-central1-a GCP_MACHINE_TYPE=n2-standard-2

set -euo pipefail

INSTANCE="${GCP_INSTANCE:-instance-20260316-154106}"
ZONE="${GCP_ZONE:-us-central1-a}"
MACHINE_TYPE="${GCP_MACHINE_TYPE:-}"
# Fallback order when zone has capacity issues (us-central1-a Jul 2026: c3-standard-4 worked).
MACHINE_TYPE_FALLBACKS="${GCP_MACHINE_TYPE_FALLBACKS:-n2-standard-2 e2-standard-2 n1-standard-2 e2-medium c3-standard-4}"

if ! command -v gcloud >/dev/null 2>&1; then
  echo "gcloud not in PATH. Install or: export PATH=\"\$HOME/google-cloud-sdk/bin:\$PATH\"" >&2
  exit 1
fi

if ! gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null | grep -q .; then
  echo "No active gcloud account. Run first:" >&2
  echo "  gcloud auth login" >&2
  echo "  gcloud config set project YOUR_PROJECT_ID" >&2
  exit 1
fi

PROJECT="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
if [[ -z "$PROJECT" || "$PROJECT" == "(unset)" ]]; then
  echo "GCP project not set. Run: gcloud config set project YOUR_PROJECT_ID" >&2
  exit 1
fi

echo "Project: $PROJECT"
echo "Instance: $INSTANCE  Zone: $ZONE"

STATUS="$(gcloud compute instances describe "$INSTANCE" --zone="$ZONE" --format='get(status)' 2>/dev/null || true)"
echo "Current status: ${STATUS:-unknown}"

if [[ "$STATUS" == "RUNNING" ]]; then
  echo "Stopping VM..."
  gcloud compute instances stop "$INSTANCE" --zone="$ZONE" --quiet
fi

try_start() {
  local mt="$1"
  echo "Trying machine type: $mt"
  gcloud compute instances set-machine-type "$INSTANCE" \
    --zone="$ZONE" \
    --machine-type="$mt" \
    --quiet
    if gcloud compute instances start "$INSTANCE" --zone="$ZONE" --quiet; then
      MACHINE_TYPE="$mt"
      return 0
    fi
  return 1
}

if [[ -n "$MACHINE_TYPE" ]]; then
  try_start "$MACHINE_TYPE" || true
fi

if [[ "$(gcloud compute instances describe "$INSTANCE" --zone="$ZONE" --format='get(status)' 2>/dev/null)" != "RUNNING" ]]; then
  for mt in $MACHINE_TYPE_FALLBACKS; do
    [[ "$mt" == "$MACHINE_TYPE" ]] && continue
    if try_start "$mt"; then
      break
    fi
  done
fi

STATUS="$(gcloud compute instances describe "$INSTANCE" --zone="$ZONE" --format='get(status)' 2>/dev/null || true)"
if [[ "$STATUS" != "RUNNING" ]]; then
  echo "ERROR: could not start VM in zone $ZONE. Try another zone or retry later." >&2
  exit 1
fi

IP="$(gcloud compute instances describe "$INSTANCE" --zone="$ZONE" --format='get(networkInterfaces[0].accessConfigs[0].natIP)')"
echo "Done. status=$STATUS machine_type=$MACHINE_TYPE external_ip=${IP:-n/a}"
echo "SSH: ssh ai8049520@${IP:-104.154.205.58}"
echo "Then: cd ~/lse && docker compose ps && docker compose up -d"
