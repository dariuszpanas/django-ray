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
| `--all-queues` | Process all configured queues |

#### Execution Mode

| Option | Description |
|--------|-------------|
| `--sync` | Run tasks synchronously (no Ray) |
| `--local` | Use local Ray cluster |
| `--cluster=ADDRESS` | Connect to Ray cluster at ADDRESS |
| *(none)* | Use Ray Job Submission API (default) |

#### Concurrency

| Option | Description |
|--------|-------------|
| `--concurrency=N` | Maximum concurrent tasks (default: 10) |

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

The worker responds to these signals:

| Signal | Behavior |
|--------|----------|
| `SIGTERM` | Graceful shutdown - finish current tasks |
| `SIGINT` | Graceful shutdown (Ctrl+C) |
| `SIGQUIT` | Force quit (Ctrl+\\) |

### Environment Variables

These environment variables override command-line options:

| Variable | Description |
|----------|-------------|
| `RAY_ADDRESS` | Ray cluster address |
| `DJANGO_RAY_QUEUE` | Default queue name |
| `DJANGO_RAY_CONCURRENCY` | Concurrency limit |

### Exit Codes

| Code | Description |
|------|-------------|
| `0` | Normal shutdown |
| `1` | Error during startup |
| `130` | Interrupted (SIGINT) |
| `143` | Terminated (SIGTERM) |

