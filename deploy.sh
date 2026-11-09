#!/usr/bin/env bash
# Deploy to Google Cloud Run. Requires: gcloud auth login, a project with billing.
set -euo pipefail
PROJECT="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${GCP_REGION:-us-central1}"
SERVICE="${SERVICE_NAME:-kubecon-agenda-mcp}"

gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com --project "$PROJECT"

gcloud run deploy "$SERVICE" --source . \
  --project "$PROJECT" --region "$REGION" \
  --allow-unauthenticated --cpu 1 --memory 512Mi \
  --min-instances 0 --max-instances 5 --concurrency 80 --timeout 60 \
  --set-env-vars "CACHE_TTL_SECONDS=600"

URL=$(gcloud run services describe "$SERVICE" --project "$PROJECT" --region "$REGION" --format 'value(status.url)')
echo "Service URL: $URL"
echo "MCP endpoint: $URL/mcp"

if [[ -n "${CUSTOM_DOMAIN:-}" ]]; then
  gcloud beta run domain-mappings create --service "$SERVICE" --domain "$CUSTOM_DOMAIN" \
    --project "$PROJECT" --region "$REGION" || true
  echo "Add the DNS records printed above for $CUSTOM_DOMAIN, then: https://$CUSTOM_DOMAIN/mcp"
fi
