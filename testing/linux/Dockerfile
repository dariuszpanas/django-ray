# syntax=docker/dockerfile:1
# Context: source.tar, source-manifest.json, build-constraints.txt, kubectl.
# Supply digest-pinned images and the reviewed Debian snapshot during preparation.
ARG PYTHON_IMAGE
ARG NODE_IMAGE
ARG UV_IMAGE
FROM ${NODE_IMAGE} AS node
FROM ${UV_IMAGE} AS uv
FROM ${PYTHON_IMAGE} AS tests

ARG PYTHON_IMAGE
ARG NODE_IMAGE
ARG UV_IMAGE
ARG DEBIAN_SNAPSHOT

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=0 \
    UV_PROJECT_ENVIRONMENT=/opt/test-env \
    UV_CACHE_DIR=/opt/test-build-cache \
    UV_BUILD_CONSTRAINT=/opt/test-inputs/build-constraints.txt \
    PATH=/opt/test-env/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin

# The base digest fixes Python and Debian; the snapshot fixes OS dependency resolution.
RUN python -c 'import os,re; assert all(re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}",os.environ[n]) for n in ("PYTHON_IMAGE","NODE_IMAGE","UV_IMAGE")); assert re.fullmatch(r"[0-9]{8}T[0-9]{6}Z",os.environ["DEBIAN_SNAPSHOT"])' \
    && . /etc/os-release && test "$VERSION_CODENAME" = trixie \
    && rm -f /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources \
    && printf 'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/%s/ trixie main\ndeb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/%s/ trixie-security main\n' "$DEBIAN_SNAPSHOT" "$DEBIAN_SNAPSHOT" > /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git make libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
COPY --from=uv /uv /usr/local/bin/uv
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

COPY source.tar source-manifest.json build-constraints.txt /opt/test-inputs/
COPY kubectl /usr/local/bin/kubectl
# Match the repository's existing application-qualification kubectl pin.
RUN echo '874d5e72dbb819f43cff16bcd1e4f8bac5b7f2361fe1e55049b0a6c676fb0cbf  /usr/local/bin/kubectl' | sha256sum -c - \
    && chmod 755 /usr/local/bin/kubectl \
    && mkdir /workspace \
    && python -c 'import tarfile; tarfile.open("/opt/test-inputs/source.tar").extractall("/workspace",filter="data")'

WORKDIR /workspace
RUN python scripts/test_suite_source.py verify --source-manifest /opt/test-inputs/source-manifest.json \
    && test "$(node --version)" = "v$(cat .node-version)" \
    && uv sync --frozen --extra postgres --extra sample \
    && python -m ensurepip --upgrade --default-pip \
    && npm ci --ignore-scripts --no-audit --no-fund \
    && uv cache clean \
    && uv build --require-hashes --out-dir /opt/test-distributions \
    && python scripts/test_suite_source.py verify --source-manifest /opt/test-inputs/source-manifest.json \
    && python testing/linux/image_support.py record \
        --output /opt/test-inputs/image-environment.json

# The editable project remains bound to /workspace/src. Each disposable invocation
# copies the verified source there; installed tools and dependencies stay in the image.
RUN mv /workspace /opt/test-source \
    && mkdir /workspace \
    && groupadd --gid 10001 tests \
    && useradd --uid 10001 --gid tests --no-log-init --home-dir /tmp \
        --no-create-home --shell /usr/sbin/nologin tests \
    && chown 10001:10001 /workspace

ENV UV_NO_SYNC=1 UV_OFFLINE=1 HOME=/tmp
USER 10001:10001
ENTRYPOINT ["python", "/opt/test-source/testing/linux/image_support.py", "execute"]
CMD ["catalogue"]
