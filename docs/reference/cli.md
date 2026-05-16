# CLI Reference

## django_ray_worker

The main worker command that processes tasks from the queue.

```bash
python manage.py django_ray_worker [options]
```

### Options

#### Queue Selection

| Option | Description |
|--------|-------------|
| `--queue=QUEUE` | Queue name to process (default: `default`) |
| `--queue=Q1,Q2` | Multiple queues (comma-separated) |
| `--queues Q1 Q2` | Multiple queues (space-separated alternative to `--queue`) |
| `--all-queues` | Process all configured queues |

#### Execution Mode

| Option | Description |
|--------|-------------|
| `--sync` | Run tasks synchronously (no Ray) |
| `--local` | Use local Ray cluster |
| `--cluster=ADDRESS` | Connect to Ray cluster at ADDRESS |
| *(none)* | Use default from `DJANGO_RAY.RUNNER` (`ray_job` by default) |

#### Concurrency

| Option | Description |
|--------|-------------|
| `--concurrency=N` | Maximum concurrent tasks (default: `DJANGO_RAY["DEFAULT_CONCURRENCY"]`, which defaults to `10`) |

#### Verbosity

| Option | Description |
|--------|-------------|
| `-v 0` | Minimal output |
| `-v 1` | Normal output (default) |
| `-v 2` | Verbose output |
| `-v 3` | Debug output |

### Examples

```bash
# Process default queue with local Ray
python manage.py django_ray_worker --queue=default --local

# Process multiple queues
python manage.py django_ray_worker --queue=default,high-priority --local

# Process all queues with high concurrency
python manage.py django_ray_worker --all-queues --local --concurrency=50

# Connect to Ray cluster
python manage.py django_ray_worker --queue=default --cluster=ray://ray-head:10001

# Sync mode for testing
python manage.py django_ray_worker --queue=default --sync

# Verbose output
python manage.py django_ray_worker --queue=default --local -v 2
```

### Signals

The worker explicitly handles these signals for graceful shutdown:

| Signal | Behavior |
|--------|----------|
| `SIGTERM` | Graceful shutdown - finish current tasks |
| `SIGINT` | Graceful shutdown (Ctrl+C) |

### Environment Variables

The management command itself expects CLI flags and Django settings. The Docker entrypoint included
in this repository maps these environment variables into command-line options:

| Variable | Description |
|----------|-------------|
| `DJANGO_RAY_QUEUE` | Queue name passed as `--queue` |
| `DJANGO_RAY_QUEUES` | Comma-separated queue names passed as `--queue`; takes precedence over `DJANGO_RAY_QUEUE` |
| `DJANGO_RAY_CONCURRENCY` | Concurrency limit passed as `--concurrency` |
| `RAY_ADDRESS` | Ray cluster address used by sample settings and `worker-cluster` entrypoint mode |

### Exit Codes

| Code | Description |
|------|-------------|
| `0` | Normal shutdown |
| `1` | Error during startup |
| `130` | Interrupted (SIGINT) |
| `143` | Terminated (SIGTERM) |

