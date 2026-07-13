#!/bin/sh
set -eu

if [ "$1" = "web" ]; then
  python manage.py validate_config
  python manage.py migrate --noinput
  exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 2 --threads 4 --access-logfile - --error-logfile -
fi

if [ "$1" = "worker" ]; then
  python manage.py validate_config
  exec python manage.py run_worker
fi

exec "$@"
