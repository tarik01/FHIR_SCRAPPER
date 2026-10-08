#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/app/backend"

if [[ "${1:-}" == "--reset" ]]; then
  rm -f db.sqlite3
  echo "Database removed."
fi

uv sync
uv run python manage.py migrate
uv run python manage.py migrate_fhir extract
uv run python manage.py migrate_fhir transform
uv run python manage.py migrate_fhir validate

echo
echo "Done. Start the API with:"
echo "  cd app/backend && uv run uvicorn config.asgi:application --port 8010"
