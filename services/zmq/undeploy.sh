#!/usr/bin/env bash
# Stop the ZMQ broker and remove its container.
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f .env ]] && [[ -f .env.example ]]; then
  cp .env.example .env
fi

echo "Stopping ZMQ service..."
docker compose -f docker-compose.yaml down --remove-orphans
