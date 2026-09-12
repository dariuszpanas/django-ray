# syntax=docker/dockerfile:1
FROM python:3.12-slim-trixie AS released
COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/released-venv UV_LINK_MODE=copy UV_COMPILE_BYTECODE=0
WORKDIR /opt/released-source
COPY --from=released-source source.tar /opt/released-source.tar
RUN python -c "import tarfile; a=tarfile.open('/opt/released-source.tar'); assert a.pax_headers.get('comment') == '95ee5dfe95b1c1bed95ff28c4fcb5fcdc491e485'; a.extractall('/opt/released-source', filter='data')"
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --extra postgres \
    && uv build --wheel --out-dir /opt/released-wheels \
    && rm -f /opt/released-wheels/.gitignore

# Compose supplies the existing transaction image as a native build dependency.
# It contains candidate locked dependencies, candidate wheel and PostgreSQL 17.11.
FROM tools
COPY --from=released /opt/released-venv /opt/released-venv
COPY --from=released /opt/released-source /opt/released-source
COPY --from=released /opt/released-source.tar /opt/released-source.tar
COPY --from=released /opt/released-wheels /opt/released-wheels
WORKDIR /workspace
USER 10001:10001
CMD ["python", "-m", "qualification.upgrade.scenario"]
