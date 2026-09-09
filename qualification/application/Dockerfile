# syntax=docker/dockerfile:1
# Build this and its application base from the same committed Git archive.
# The build caller must require a digest-pinned DJANGO_RAY_RUNTIME_IMAGE and
# verify the resulting base-layer ancestry before registering the test image.
ARG DJANGO_RAY_RUNTIME_IMAGE
FROM ${DJANGO_RAY_RUNTIME_IMAGE}

WORKDIR /app
COPY --chown=1000:1000 qualification/__init__.py /app/qualification/__init__.py
COPY --chown=1000:1000 qualification/application/*.py /app/qualification/application/

# Preserve the base application's non-root user, entrypoint and default mode.
# Assertion Jobs select their finite Python module with an explicit command.
