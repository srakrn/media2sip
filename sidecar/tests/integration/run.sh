#!/usr/bin/env bash
# End-to-end suite: the sidecar against a real Asterisk, no FreePBX involved.
#
#   ./tests/integration/run.sh          bring up, test, tear down
#   KEEP=1 ./tests/integration/run.sh   leave the stack running afterwards
set -euo pipefail
cd "$(dirname "$0")"

PYTEST="${PYTEST:-../../../.venv/bin/python -m pytest}"

cleanup() {
  if [[ -z "${KEEP:-}" ]]; then
    docker compose down -v >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

echo "building the sidecar image..."
docker build -q -t pbx-page-sidecar:latest ../.. >/dev/null

echo "bringing up asterisk + sidecar..."
docker compose down -v >/dev/null 2>&1 || true
docker compose up -d >/dev/null

echo -n "waiting for registration"
for _ in $(seq 1 40); do
  if curl -sS --max-time 2 http://127.0.0.1:18080/health 2>/dev/null \
     | grep -q '"registered": *true'; then
    echo " ok"
    break
  fi
  echo -n "."
  sleep 2
done

$PYTEST test_e2e.py -p no:cacheprovider "$@"
