# syntax=docker/dockerfile:1
# Derived from the committed candidate with fixture-only browser tooling.
ARG DJANGO_RAY_RUNTIME_IMAGE
FROM ${DJANGO_RAY_RUNTIME_IMAGE}
COPY --chown=1000:1000 qualification/__init__.py /app/qualification/__init__.py
COPY --chown=1000:1000 qualification/monitoring/__init__.py qualification/monitoring/probe.py qualification/monitoring/browser.py /app/qualification/monitoring/
USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/qualification-browser-cache
RUN /usr/local/bin/python -m venv /opt/qualification-browser && \
    /opt/qualification-browser/bin/pip install --no-cache-dir playwright==1.63.0 && \
    /opt/qualification-browser/bin/python -m playwright install --with-deps --only-shell chromium && \
    rm -rf /var/lib/apt/lists/*
USER django
