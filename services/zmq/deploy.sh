#!/usr/bin/env bash
# Build the image and start the ZMQ broker.
#
#   ./deploy.sh
#   ./deploy.sh --restart
set -euo pipefail

cd "$(dirname "$0")"

RESTART=false

usage() {
  echo "Usage: $0 [--restart] [-h]"
  echo "  --restart    Recreate the container from a fresh image build"
  echo "  -h           Show this help"
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      ;;
    --restart|-r)
      RESTART=true
      shift
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      ;;
  esac
done

if [[ ! -f .env ]] && [[ -f .env.example ]]; then
  cp .env.example .env
fi

if [[ "${RESTART}" == "true" ]]; then
  docker compose -f docker-compose.yaml down --remove-orphans
fi

echo "Deploying ZMQ service..."
exec docker compose -f docker-compose.yaml up -d --build --force-recreate
