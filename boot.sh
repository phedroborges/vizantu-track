#!/bin/sh
set -eu
python manage.py check --deploy --fail-level WARNING
python manage.py migrate --noinput
python manage.py bootstrap_admin
exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 2 --threads 2 --timeout 45 --error-logfile -
