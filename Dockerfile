# Multi-stage: build the React frontend, then bake it into the Python image.
# One image is shared by all services (chatbot serves the built UI at /; the
# others ignore it) — command is set per-service in docker-compose.yml.

# ── Stage 1: build the React frontend (frontend/dist) ──
FROM node:22-slim AS frontend
WORKDIR /frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

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
# Overlay the freshly built frontend (app/main.py serves it at "/").
COPY --from=frontend /frontend/dist ./frontend/dist

# Default command; each service overrides `command:` in docker-compose.yml.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
