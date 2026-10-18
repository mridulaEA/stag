# ---- frontend build stage -------------------------------------------------------------------
FROM node:20-alpine AS frontend
WORKDIR /ui
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- runtime -------------------------------------------------------------------------------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system stag && useradd --system --gid stag --home /app stag

COPY requirements.txt .
RUN pip install -r requirements.txt

# Application code and the catalogs it serves (skills, crews, agents)
COPY stag/ ./stag/
COPY skills/ ./skills/
COPY crews/ ./crews/
COPY agents/ ./agents/
COPY --from=frontend /ui/dist ./frontend/dist

RUN mkdir -p /app/data && chown -R stag:stag /app
USER stag

# Configuration is injected at deploy time (env vars / secrets); no example .env is baked in.
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "stag.main"]
