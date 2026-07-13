#!/bin/sh
set -eu

if [ "$1" = "web" ]; then
  python manage.py validate_config --web
  python manage.py migrate --noinput
  python manage.py enforce_ebay_account_closure
  exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 2 --threads 4 --access-logfile - --access-logformat '%(h)s %(m)s %(s)s %(M)sms %(b)s' --error-logfile -
fi

if [ "$1" = "worker" ]; then
  python manage.py validate_config --worker
  exec python manage.py run_worker
fi

exec "$@"
