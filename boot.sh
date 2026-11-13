#!/bin/bash
set -eu
python manage.py check --deploy --fail-level WARNING
python manage.py prepare_database
python manage.py migrate --noinput
python manage.py prepare_database --secure
python manage.py bootstrap_admin
# Keep the web process and durable inbox worker together in the existing App service.
python manage.py runworker &
worker_pid=$!
gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 2 --threads 2 --timeout 45 --error-logfile - &
web_pid=$!
trap 'kill -TERM "$worker_pid" "$web_pid" 2>/dev/null || true; wait "$worker_pid" "$web_pid" 2>/dev/null || true' EXIT
trap 'exit 0' TERM INT
wait -n "$worker_pid" "$web_pid"
exit 1
