# Lean image for the long-running serve process (polling + daily 10:00 harvest).
# Base is python:3.9-slim to match the pinned aiogram 3.13.x / interpreter 3.9.
FROM python:3.9-slim

# Clean container logs: no stdout buffering, no .pyc files written.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Bake the CONTAINER default DB path into the image so the SQLite DB is written
# to the host-mounted ./data:/app/data volume (NOT the relative WORKDIR default,
# which would be lost on every recreate). config.py keeps its RELATIVE default
# ("job_hunter.db") for local dev; this ENV only overrides it inside the image.
# A real .env (env_file in docker-compose) can still override DB_PATH at runtime.
ENV DB_PATH=/app/data/job_hunter.db

# Build-time git sha for the ops startup ping. .dockerignore excludes .git, so
# the sha CANNOT be read from git at runtime — it is injected here at build time
# (docker-compose passes args.GIT_SHA) and read via os.environ["GIT_SHA"].
# Operator build command:  GIT_SHA=$(git rev-parse --short HEAD) docker compose up -d --build
ARG GIT_SHA=unknown
ENV GIT_SHA=${GIT_SHA}

WORKDIR /app

# Copy ONLY requirements first so the pip layer is cached and not invalidated by
# code changes. --no-cache-dir keeps the image lean (no pip wheel cache left
# behind). The slim base already has no build toolchain; our deps are pure
# Python wheels (aiogram, apscheduler, httpx, anthropic, tzdata, ...), so no
# apt build packages are needed and none are installed/left behind.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Now copy the application code (this layer changes most often). This wholesale
# `COPY . .` ships EVERY module, INCLUDING job_hunter/tg_logger.py (the ops
# Telegram logger used by the startup ping + error handler). Do NOT replace this
# with narrow per-file COPYs that could silently drop tg_logger.py and trigger a
# ModuleNotFoundError crash-loop at startup.
COPY . .

# Privilege-drop entrypoint: runs as root to chown the bind-mounted /app/data,
# then drops to appuser (uid 10001) before exec'ing the CMD. See the script.
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# Create a non-root user and a writable mount point for the host-mounted SQLite
# DB. The real .env and config/profile.local.yaml are NOT baked in (excluded via
# .dockerignore); they are bind-mounted at runtime by docker-compose. /app/data
# is owned by appuser so the host-mounted DB is read/writable; /app/config is
# owned so the read-only profile mount can be read.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
# NOTE: no `USER appuser` here. The container starts as ROOT so the entrypoint
# can chown the (root-owned) host-mounted /app/data, then it DROPS to appuser
# itself via `runuser` before exec'ing the CMD. The final process is uid 10001.

# Entrypoint drops privileges; CMD is the serve process: aiogram long-polling +
# AsyncIOScheduler daily harvest + heartbeat task, one event loop, one
# asyncio.run. CMD is passed to the entrypoint as "$@".
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["python", "-m", "job_hunter.serve"]
