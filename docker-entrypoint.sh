#!/bin/bash
# Django-Ray Docker Entrypoint Script
# Handles different run modes: web, worker, migrate, shell

set -e

# Default mode is web server
MODE=${1:-web}

# Wait for database to be ready
wait_for_db() {
    echo "Waiting for database..."
    python -c "
import time
import django
django.setup()
from django.db import connections
from django.db.utils import OperationalError

for i in range(30):
    try:
        connections['default'].ensure_connection()
        print('Database is ready!')
        break
    except OperationalError:
        print(f'Database not ready, waiting... ({i+1}/30)')
        time.sleep(2)
else:
    print('Could not connect to database!')
    exit(1)
"
}

case "$MODE" in
    web)
        echo "Starting Django web server..."
        wait_for_db
        gunicorn_args=(
            testproject.wsgi:application
            --bind 0.0.0.0:8000
            --workers "${GUNICORN_WORKERS:-2}"
            --threads "${GUNICORN_THREADS:-4}"
            --worker-class gthread
            --timeout "${GUNICORN_TIMEOUT:-120}"
            --graceful-timeout "${GUNICORN_GRACEFUL_TIMEOUT:-30}"
            --keep-alive "${GUNICORN_KEEPALIVE:-5}"
            --max-requests "${GUNICORN_MAX_REQUESTS:-1000}"
            --max-requests-jitter "${GUNICORN_MAX_REQUESTS_JITTER:-100}"
            --worker-tmp-dir "${GUNICORN_WORKER_TMP_DIR:-/dev/shm}"
            --error-logfile -
            --capture-output
            --enable-stdio-inheritance
        )

        access_logfile="${GUNICORN_ACCESS_LOGFILE:--}"
        if [[ -n "$access_logfile" && "$access_logfile" != "off" ]]; then
            gunicorn_args+=(--access-logfile "$access_logfile")
        fi

        control_socket_disable="${GUNICORN_CONTROL_SOCKET_DISABLE:-true}"
        case "$control_socket_disable" in
            1|true|TRUE|True|yes|YES|Yes)
                gunicorn_args+=(--no-control-socket)
                ;;
            *)
                if [[ -n "${GUNICORN_CONTROL_SOCKET:-}" ]]; then
                    gunicorn_args+=(--control-socket "${GUNICORN_CONTROL_SOCKET}")
                fi
                ;;
        esac

        exec gunicorn "${gunicorn_args[@]}"
        ;;

    web-dev)
        echo "Starting Django development server..."
        wait_for_db
        exec python testproject/manage.py runserver 0.0.0.0:8000
        ;;

    worker)
        echo "Starting Django-Ray worker..."
        wait_for_db
        QUEUE=${DJANGO_RAY_QUEUES:-${DJANGO_RAY_QUEUE:-default}}
        CONCURRENCY=${DJANGO_RAY_CONCURRENCY:-10}
        exec python testproject/manage.py django_ray_worker \
            --queue="$QUEUE" \
            --concurrency="$CONCURRENCY" \
            --local
        ;;

    worker-cluster)
        echo "Starting Django-Ray worker (cluster mode)..."
        wait_for_db
        QUEUE=${DJANGO_RAY_QUEUES:-${DJANGO_RAY_QUEUE:-default}}
        CONCURRENCY=${DJANGO_RAY_CONCURRENCY:-10}
        RAY_CLUSTER=${RAY_ADDRESS:-ray://ray-head-svc:10001}
        exec python testproject/manage.py django_ray_worker \
            --queue="$QUEUE" \
            --concurrency="$CONCURRENCY" \
            --cluster="$RAY_CLUSTER"
        ;;

    migrate)
        echo "Running Django migrations..."
        wait_for_db
        exec python testproject/manage.py migrate --noinput
        ;;

    collectstatic)
        echo "Collecting static files..."
        exec python testproject/manage.py collectstatic --noinput
        ;;

    createsuperuser)
        echo "Creating superuser..."
        # Uses DJANGO_SUPERUSER_USERNAME, DJANGO_SUPERUSER_EMAIL, DJANGO_SUPERUSER_PASSWORD env vars
        exec python testproject/manage.py createsuperuser --noinput
        ;;

    shell)
        echo "Starting Django shell..."
        exec python testproject/manage.py shell
        ;;

    bash)
        echo "Starting bash shell..."
        exec /bin/bash
        ;;

    *)
        # Pass through any other command
        exec "$@"
        ;;
esac

