# --- Frontend build stage (issue #6 part 2): the React dashboard bundle -------
# Node is needed ONLY to build the static SPA; it does NOT ship in the runtime
# image (the final image stays python:3.9-slim). We build frontend/ ->
# /frontend/dist here and COPY that dist into the python stage's /app/static
# below. `npm ci` uses the committed package-lock for a reproducible install.
# The build context ships frontend/ SOURCE but EXCLUDES frontend/node_modules
# and frontend/dist (see .dockerignore): node_modules is installed fresh here
# and dist is produced in-stage.
FROM node:20-alpine AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
# Build-time DISPLAY NAME for the dashboard profile. The repo is de-identified:
# the real name is NOT tracked. Vite exposes VITE_-prefixed env vars to the
# client bundle, so this ARG -> ENV is baked into the SPA by `npm run build`.
# Unset -> Sidebar falls back to the generic "Кандидат" (a fresh clone stays
# generic). The real value is supplied at deploy time by the VPS .env via
# docker-compose build args (see docker-compose.yml) and is NEVER committed.
# The AVATAR is supplied the same out-of-repo way: a gitignored
# frontend/src/assets/avatar.local.png placed on the VPS is in the build context
# (it is NOT excluded by .dockerignore) and import.meta.glob bundles it; absent
# -> the initials placeholder renders. Declared right before the build so a name
# change busts only this layer, not the cached `npm ci`.
ARG VITE_PROFILE_NAME=""
ENV VITE_PROFILE_NAME=${VITE_PROFILE_NAME}
RUN npm run build

# Lean image for the long-running serve process (polling + daily 10:00 harvest).
# Base is python:3.9-slim to match the pinned aiogram 3.13.x / interpreter 3.9.
FROM python:3.9-slim

# Clean container logs: no stdout buffering, no .pyc files written.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# The DB now lives in a separate PostgreSQL service (see docker-compose.yml).
# The app reads its connection string from DATABASE_URL at runtime (supplied via
# env_file / environment in compose) — there is no on-disk DB file in this image.

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

# Copy the built SPA from the frontend stage into /app/static. Placed AFTER the
# wholesale `COPY . .` so it is NOT clobbered by it (and so .dockerignore's
# exclusion of frontend/dist in the build context is irrelevant — this dist is
# the freshly-built one from the node stage). webapi.create_app() serves this
# dir via StaticFiles + an SPA catch-all when DASHBOARD_STATIC_DIR (default
# /app/static) exists. This image is shared with the bot (job-hunter:latest);
# the extra static dir is harmless to the bot, which runs serve (not uvicorn).
COPY --from=frontend /frontend/dist /app/static

# Privilege-drop entrypoint: starts as root, then drops to appuser (uid 10001)
# before exec'ing the CMD. See the script. (No data-dir chown is needed anymore:
# the DB lives in the separate Postgres service, not an on-disk file here.)
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# Create the non-root user. The real .env and config/profile.local.yaml are NOT
# baked in (excluded via .dockerignore); they are bind-mounted at runtime by
# docker-compose. /app/config is owned so the read-only profile mount can be read.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
# NOTE: no `USER appuser` here. The container starts as ROOT so the entrypoint
# can DROP to appuser via `runuser` before exec'ing the CMD. The final process
# is uid 10001.

# Entrypoint drops privileges; CMD is the serve process: aiogram long-polling +
# AsyncIOScheduler daily harvest + heartbeat task, one event loop, one
# asyncio.run. CMD is passed to the entrypoint as "$@".
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["python", "-m", "job_hunter.serve"]
