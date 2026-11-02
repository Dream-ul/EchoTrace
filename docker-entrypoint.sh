#!/bin/sh
set -eu
echo "==> EchoTrace backend starting"
if [ "${ECHOTRACE_PERSISTENCE:-mysql}" = "mysql" ] && [ "${ECHOTRACE_SKIP_MIGRATIONS:-0}" != "1" ]; then
  echo "==> Applying database migrations"
  alembic upgrade head
fi
if [ "${ECHOTRACE_PROCESS:-api}" = "worker" ]; then
  exec python -m echotrace.worker
fi
exec uvicorn app.main:app --host "${ECHOTRACE_HOST:-0.0.0.0}" --port "${ECHOTRACE_PORT:-8000}" --log-level "${ECHOTRACE_LOG_LEVEL:-info}"
