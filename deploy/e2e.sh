#!/usr/bin/env bash
# Throwaway copy of the stack for end-to-end checks: project pm-e2e, bound to 127.0.0.1:8650, own volumes.
# Usage: ./e2e.sh up | down
set -euo pipefail
cd "$(dirname "$0")"
export PM_BIND=127.0.0.1 PM_PORT=8650 PM_SUBNET_EDGE=172.30.96.0/24 PM_SUBNET_INTERNAL=172.30.97.0/24 PM_SUBNET_OUTBOUND=172.30.98.0/24 PM_SUBNET_MAIL=172.30.99.0/24
case "${1:-up}" in
  up) docker compose -p pm-e2e -f compose.yaml -f compose.e2e.yaml up -d --build ;;
  down) docker compose -p pm-e2e -f compose.yaml -f compose.e2e.yaml down -v ;;
esac
