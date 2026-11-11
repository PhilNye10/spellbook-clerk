#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

if [[ ! -d .git ]]; then
  echo "ERROR: This folder is not a Git repository. See GITHUB_SETUP.md first."
  exit 1
fi

for required in .env config.yaml docker-compose.yml; do
  if [[ ! -e "$required" ]]; then
    echo "ERROR: Required local file '$required' is missing. Stopping without changing anything."
    exit 1
  fi
done

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "ERROR: Tracked files have local changes. Commit/stash them before updating."
  git status --short
  exit 1
fi

BACKUP="$HOME/spellbook-clerk-backup-$(date +%F-%H%M%S).tar.gz"
PREVIOUS_COMMIT="$(git rev-parse HEAD)"

echo "Creating backup: $BACKUP"
tar -czf "$BACKUP" .env config.yaml data docker-compose.yml

echo "Fetching latest version..."
git fetch origin
BRANCH="$(git branch --show-current)"
git pull --ff-only origin "$BRANCH"

rollback() {
  local exit_code=$?
  echo "Update failed (exit $exit_code). Rolling source code back to $PREVIOUS_COMMIT..."
  git reset --hard "$PREVIOUS_COMMIT" || true
  docker compose up -d --build || true
  echo "Rollback attempted. Your configuration/data backup is at: $BACKUP"
  exit "$exit_code"
}
trap rollback ERR

echo "Building and starting updated bot..."
docker compose up -d --build
sleep 5
if ! docker compose ps --status running --services | grep -qx 'mtg-table-bot'; then
  echo "ERROR: mtg-table-bot service is not running."
  docker compose logs --tail=100
  false
fi

trap - ERR
echo
echo "Update completed successfully."
echo "Backup: $BACKUP"
echo "Recent logs:"
docker compose logs --tail=40
