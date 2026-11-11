#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${APP_ROOT:-/opt/mtg-table-bot}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ ! -f "$SCRIPT_DIR/docker-compose.yml" ]]; then
  echo "ERROR: Run this script from the extracted mtg-table-bot directory." >&2
  exit 1
fi

mkdir -p "$APP_ROOT/data"
cp -a "$SCRIPT_DIR/." "$APP_ROOT/"
rm -rf "$APP_ROOT/mtg_table_bot/__pycache__" "$APP_ROOT/.pytest_cache"

cd "$APP_ROOT"

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "Edit $APP_ROOT/.env before starting the container."
fi

if [[ ! -f config.yaml ]]; then
  cp config.example.yaml config.yaml
fi

chmod 600 .env

docker compose up -d --build

echo
echo "=============================================="
echo " The Spellbook Clerk installed successfully"
echo "=============================================="
echo "App:      $APP_ROOT"
echo "Database: $APP_ROOT/data"
echo "Logs:     docker logs -f mtg-table-bot"
