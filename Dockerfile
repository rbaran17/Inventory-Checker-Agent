# syntax=docker/dockerfile:1.7
# Inventory Checker Agent - phone-camera inventory count POC (Microsoft Agent Framework + Claude Haiku 5.5)

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------- build: wheel for the app
FROM python:${PYTHON_VERSION}-slim AS build
WORKDIR /src
# Optional CA bundle for building behind a TLS-inspecting corporate proxy:
#   docker build --secret id=pip_ca,src=/path/to/ca-bundle.crt .
RUN --mount=type=secret,id=pip_ca,required=false \
    if [ -f /run/secrets/pip_ca ]; then export PIP_CERT=/run/secrets/pip_ca; fi; \
    pip install --no-cache-dir --upgrade pip build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN --mount=type=secret,id=pip_ca,required=false \
    if [ -f /run/secrets/pip_ca ]; then export PIP_CERT=/run/secrets/pip_ca; fi; \
    python -m build --wheel --outdir /dist

# ---------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim AS runtime
ARG VERSION=dev
ARG REVISION=unknown
LABEL org.opencontainers.image.title="inventory-checker-agent" \
      org.opencontainers.image.description="Phone-camera inventory count POC: Claude Haiku 5.5 on Microsoft Agent Framework" \
      org.opencontainers.image.source="https://github.com/rbaran17/inventory-checker-agent" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    APP_ACCESS_KEY_FILE=/data/access_key

COPY --from=build /dist/*.whl /tmp/
RUN --mount=type=secret,id=pip_ca,required=false \
    if [ -f /run/secrets/pip_ca ]; then export PIP_CERT=/run/secrets/pip_ca; fi; \
    pip install "$(ls /tmp/*.whl)[azure]" && rm -f /tmp/*.whl \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data && chown app:app /data

USER app
WORKDIR /home/app
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/healthz', timeout=4)"

CMD ["inventory-checker", "serve"]
