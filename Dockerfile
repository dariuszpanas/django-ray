# syntax=docker/dockerfile:1
# Django-Ray Docker Image
# Multi-stage build for optimized production image
#
# Usage:
#   docker run django-ray web          # Production web server (gunicorn)
#   docker run django-ray web-dev      # Development web server
#   docker run django-ray worker       # Django-Ray task worker (local Ray)
#   docker run django-ray worker-cluster  # Worker connecting to Ray cluster
#   docker run django-ray migrate      # Run migrations
#   docker run django-ray shell        # Django shell

ARG PYTHON_VERSION=3.12

# =============================================================================
# Stage 1: Build stage - install dependencies
# =============================================================================
FROM python:${PYTHON_VERSION}-slim AS builder

# Install uv for fast dependency management
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Copy dependency files
COPY pyproject.toml uv.lock ./

# Install dependencies with postgres support
# Note: We include dev dependencies because testproject requires django-ninja and whitenoise
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --extra postgres

# Copy source code
COPY src/ src/
COPY testproject/ testproject/
COPY README.md ./

# Install the project itself
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra postgres

# Install gunicorn for production
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install gunicorn

# =============================================================================
# Stage 2: Runtime stage - minimal production image
# =============================================================================
FROM python:${PYTHON_VERSION}-slim AS runtime

# Create non-root user for security
RUN groupadd --gid 1000 django && \
    useradd --uid 1000 --gid django --shell /bin/bash --create-home django

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE="testproject.settings"

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder --chown=django:django /app/.venv /app/.venv

# Copy application code
COPY --chown=django:django src/ src/
COPY --chown=django:django testproject/ testproject/

# Create staticfiles directory with proper permissions
RUN mkdir -p /app/staticfiles && chown django:django /app/staticfiles

# Copy and set up entrypoint script
COPY --chown=django:django docker-entrypoint.sh /app/
RUN chmod +x /app/docker-entrypoint.sh

# Switch to non-root user
USER django

# Expose ports
# 8000 - Django web server
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import django; django.setup()" || exit 1

# Entrypoint handles different modes
ENTRYPOINT ["/app/docker-entrypoint.sh"]

# Default mode is production web server
CMD ["web"]

