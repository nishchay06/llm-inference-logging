# Multi-stage: build the React frontend, then bake it into the Python image.
# One image is shared by all services (chatbot serves the built UI at /; the
# others ignore it) — command is set per-service in docker-compose.yml.

# ── Stage 1: build the two React apps (chat UI + dashboard) ──
FROM node:22-slim AS frontend
WORKDIR /build
COPY frontend/package*.json ./frontend/
COPY dashboard/package*.json ./dashboard/
RUN cd frontend && npm ci
RUN cd dashboard && npm ci
COPY frontend/ ./frontend/
COPY dashboard/ ./dashboard/
RUN cd frontend && npm run build
RUN cd dashboard && npm run build

# ── Stage 2: the Python app ──
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /code

# Install deps first so this layer is cached across code changes.
# psycopg[binary] bundles libpq, so no system build packages are needed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# Overlay the freshly built apps: chat UI (served by the chatbot at "/") and
# dashboard (served by ingestion at "/").
COPY --from=frontend /build/frontend/dist ./frontend/dist
COPY --from=frontend /build/dashboard/dist ./dashboard/dist

# Drop root: nothing here needs it at runtime, and a container escape should not
# land on uid 0. Done after COPY so the image layers stay owned by root (the app
# cannot rewrite its own code).
RUN useradd --create-home --uid 10001 app
USER app

# Default command; each service overrides `command:` in docker-compose.yml.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
