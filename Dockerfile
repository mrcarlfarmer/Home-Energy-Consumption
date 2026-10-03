# syntax=docker/dockerfile:1
FROM --platform=$BUILDPLATFORM node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3 AS python-builder
WORKDIR /build
COPY requirements.lock ./
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir --require-hashes -r requirements.lock
COPY pyproject.toml ./
COPY backend/ backend/
COPY --from=frontend /build/backend/app/static/ backend/app/static/
RUN /opt/venv/bin/pip install --no-cache-dir --no-deps .

FROM python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_DATABASE_PATH=/data/energy.sqlite3
RUN groupadd --gid 10001 energy && useradd --uid 10001 --gid 10001 --no-create-home energy \
    && mkdir /data && chown energy:energy /data && chmod 700 /data
COPY --from=python-builder /opt/venv /opt/venv
USER 10001:10001
RUN python -c "import platform, sqlite3; from app.main import create_app; from fastapi.sse import EventSourceResponse; assert sqlite3.sqlite_version_info >= (3, 37); print(platform.machine(), sqlite3.sqlite_version)"
WORKDIR /data
EXPOSE 8443
HEALTHCHECK --interval=30s --timeout=8s --start-period=20s --retries=3 CMD ["python", "-m", "app.healthcheck"]
CMD ["energy-monitor"]
