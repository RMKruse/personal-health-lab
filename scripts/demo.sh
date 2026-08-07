#!/usr/bin/env bash
set -euo pipefail

DEMO_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/healthlab-demo.XXXXXX")
trap 'rm -rf "$DEMO_ROOT"' EXIT

DEMO_EXPORT="$DEMO_ROOT/export"
DEMO_SYNTHETIC_STORE="$DEMO_ROOT/synthetic-store"
DEMO_REAL_STORE="$DEMO_ROOT/real-store"
HEALTHLAB=(
  uv run healthlab
  --mode synthetic
  --synthetic-store "$DEMO_SYNTHETIC_STORE"
  --real-store "$DEMO_REAL_STORE"
)

fingerprint() {
  uv run python -c 'import json, sys; print(json.load(sys.stdin)["fingerprint"])'
}

echo "1/4 Synthetischen Apple-Health-Export erzeugen"
uv run healthlab-dev generate \
  --scenario lag-signal-v1 \
  --seed 42 \
  --destination "$DEMO_EXPORT" \
  --json >/dev/null

echo "2/4 Import planen und ausführen"
IMPORT_PLAN=$("${HEALTHLAB[@]}" import "$DEMO_EXPORT/apple-health-export.zip" --json)
IMPORT_FINGERPRINT=$(fingerprint <<<"$IMPORT_PLAN")
"${HEALTHLAB[@]}" import "$DEMO_EXPORT/apple-health-export.zip" \
  --json --execute --expect-plan "$IMPORT_FINGERPRINT" >/dev/null

echo "3/4 Ruhepulsanalyse planen und ausführen"
ANALYSIS_PLAN=$("${HEALTHLAB[@]}" analyze --json)
ANALYSIS_FINGERPRINT=$(fingerprint <<<"$ANALYSIS_PLAN")
"${HEALTHLAB[@]}" analyze \
  --json --execute --expect-plan "$ANALYSIS_FINGERPRINT" >/dev/null

echo "4/4 Streamlit-Demo starten (Strg-C beendet und löscht die Demo-Daten)"
HEALTHLAB_MODE=synthetic \
HEALTHLAB_SYNTHETIC_STORE="$DEMO_SYNTHETIC_STORE" \
HEALTHLAB_REAL_STORE="$DEMO_REAL_STORE" \
uv run streamlit run src/personal_health_lab/adapters/streamlit/app.py
