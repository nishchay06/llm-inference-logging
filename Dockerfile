# Single image shared by both services (chatbot + ingestion) and the one-shot
# schema init — same code, different uvicorn target (set per-service in
# docker-compose.yml).
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /code

# Install deps first so this layer is cached across code changes.
# psycopg[binary] bundles libpq, so no system build packages are needed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default command; each service overrides `command:` in docker-compose.yml.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
