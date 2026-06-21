# Lean image for the long-running serve process (polling + daily 10:00 harvest).
# Base is python:3.9-slim to match the pinned aiogram 3.13.x / interpreter 3.9.
FROM python:3.9-slim

# Clean container logs: no stdout buffering, no .pyc files written.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Copy ONLY requirements first so the pip layer is cached and not invalidated by
# code changes. --no-cache-dir keeps the image lean (no pip wheel cache left
# behind). The slim base already has no build toolchain; our deps are pure
# Python wheels (aiogram, apscheduler, httpx, anthropic, tzdata, ...), so no
# apt build packages are needed and none are installed/left behind.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Now copy the application code (this layer changes most often).
COPY . .

# Create a non-root user and a writable mount point for the host-mounted SQLite
# DB. The real .env and config/profile.local.yaml are NOT baked in (excluded via
# .dockerignore); they are bind-mounted at runtime by docker-compose. /app/data
# is owned by appuser so the host-mounted DB is read/writable; /app/config is
# owned so the read-only profile mount can be read.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

# The serve process: aiogram long-polling + AsyncIOScheduler daily harvest, one
# event loop, one asyncio.run.
ENTRYPOINT ["python", "-m", "job_hunter.serve"]
