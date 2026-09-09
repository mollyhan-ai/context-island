#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [[ ! -f "${LEXICON_DB_PATH:-data/lexicon.db}" ]]; then
  echo "词库不存在。请先运行：python3 data/build_lexicon.py" >&2
  exit 1
fi

exec python3 server.py
