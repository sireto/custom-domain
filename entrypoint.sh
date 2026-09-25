#!/bin/bash
set -euo pipefail

cd /app
custom-domain db upgrade
/usr/bin/caddy start
exec uvicorn app.main:app --host 0.0.0.0 --port 9000
