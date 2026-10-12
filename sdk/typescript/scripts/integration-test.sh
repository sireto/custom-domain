#!/usr/bin/env bash
# Run the TypeScript client against the real service.
#
# Starts the API on a temporary SQLite database (workers off, private webhook
# URLs allowed, as the Python SDK tests do; the setup page on), creates an application and a
# credential with the `custom-domain` CLI, then runs test/integration.test.ts.
# Needs uv and the repository's Python environment (`uv sync --all-packages`).
#
#   cd sdk/typescript && npm run test:integration
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="$(cd "${here}/../.." && pwd)"
work="$(mktemp -d)"
server_pid=""

cleanup() {
  if [ -n "${server_pid}" ]; then
    kill "${server_pid}" 2>/dev/null || true
    wait "${server_pid}" 2>/dev/null || true
  fi
  rm -rf "${work}"
}
trap cleanup EXIT

export DATABASE_URL="sqlite:///${work}/custom_domain.db"
export EDGE_RECONCILE_ENABLED=false
export DNS_WORKER_ENABLED=false
export WEBHOOK_WORKER_ENABLED=false
export ORIGIN_ALLOW_PRIVATE=true
# The setup page (connect sessions) needs both; nothing is served on the name.
export PUBLIC_API=true
export EDGE_HOSTNAME=edge.example.net

cd "${repo}"
uv run --quiet custom-domain db upgrade >/dev/null 2>&1
uv run --quiet custom-domain application create --slug acme --name "Acme Forms" \
  --cname-target acme.edge.example.net >/dev/null
# The secret is the last line of the output; it stays in this process only.
api_key="$(uv run --quiet custom-domain credential issue --application acme --label ts-sdk | tail -n 1)"
case "${api_key}" in
  cd_*) ;;
  *) echo "could not issue a credential" >&2; exit 1 ;;
esac

port="$(uv run --quiet python -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')"
uv run --quiet uvicorn app.main:app --host 127.0.0.1 --port "${port}" --log-level warning \
  >"${work}/server.log" 2>&1 &
server_pid=$!

for _ in $(seq 1 100); do
  if curl -fsS "http://127.0.0.1:${port}/v1/openapi.json" >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "${server_pid}" 2>/dev/null; then
    cat "${work}/server.log" >&2
    exit 1
  fi
  sleep 0.2
done

cd "${here}"
npm run --silent pretest
CUSTOM_DOMAIN_TEST_URL="http://127.0.0.1:${port}" CUSTOM_DOMAIN_TEST_API_KEY="${api_key}" \
  node --test build/test/integration.test.js
