#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONNOUSERSITE=1
export PYTHONPATH="$PWD/vendor"
export PROBE_DATA="$PWD/nltk_data"
exec python3 -B -m uvicorn probe:app --host 0.0.0.0 --port "${_FAAS_RUNTIME_PORT:-8000}" --workers 1 --no-access-log
