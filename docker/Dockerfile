# ── Stage 1: Build dependencies ──────────────────────────────────────────────
FROM python:3.11-slim AS builder
WORKDIR /build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

COPY pyproject.toml .
RUN pip install --upgrade pip && \
    pip install --prefix=/install -e "." && \
    pip install --prefix=/install playwright==1.44.0 faiss-cpu \
        groq spotipy google-api-python-client python-telegram-bot 2>/dev/null || true

# ── Stage 2: Playwright browsers (cached layer) ───────────────────────────────
FROM python:3.11-slim AS playwright
RUN pip install playwright==1.44.0 && \
    playwright install chromium --with-deps && \
    playwright install-deps chromium

# ── Stage 3: Production runtime ───────────────────────────────────────────────
FROM python:3.11-slim AS runtime
WORKDIR /app

# Security: non-root user
RUN groupadd -r nexus && useradd -r -g nexus -u 1000 nexus && \
    mkdir -p /app/data/logs /app/data/screenshots /app/data/cache && \
    chown -R nexus:nexus /app

# Copy installed packages
COPY --from=builder /install/lib /usr/local/lib
COPY --from=builder /install/bin /usr/local/bin

# Copy Playwright browsers
COPY --from=playwright /root/.cache/ms-playwright /home/nexus/.cache/ms-playwright
COPY --from=playwright /usr/lib/x86_64-linux-gnu /usr/lib/x86_64-linux-gnu

# Copy application code
COPY --chown=nexus:nexus . .

USER nexus

# Environment
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    HOST=0.0.0.0 \
    PORT=8000 \
    ENVIRONMENT=production \
    WORKERS=2

VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/ping',timeout=5)" || exit 1

CMD uvicorn src.api.main:app \
    --host ${HOST} \
    --port ${PORT} \
    --workers ${WORKERS} \
    --log-level info \
    --access-log
