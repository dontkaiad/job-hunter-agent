#!/bin/sh
# Container entrypoint: fix the bind-mounted data dir ownership AS ROOT, then
# DROP to the non-root appuser (uid 10001) before exec'ing the serve process.
#
# Why this exists (Part A — DB persistence fix):
# The container runs the SQLite DB under /app/data, which is bind-mounted from
# the host (./data:/app/data in docker-compose). A fresh host ./data is created
# root-owned, but the app runs as appuser (uid 10001) and so cannot create the
# .db file there — the watermark + card state would be lost on every recreate.
# This script runs as root just long enough to chown /app/data to appuser, then
# permanently drops privileges. The final process is appuser, NOT root.
#
# Privilege-drop tool: `runuser` (util-linux, present at /usr/sbin/runuser in
# python:3.9-slim / debian bookworm-slim — verified). The slim base has NO
# `su-exec` and no `gosu`; installing either would add an apt layer. `runuser`
# is already in the base image, needs no PAM password, and forwards signals to
# its child. We `exec` it so it replaces this shell (becomes PID 1's process)
# and the kernel delivers SIGTERM/SIGINT straight to it -> graceful shutdown.
set -e

# Ensure the mount point exists (anonymous-volume or first-run safety) and is
# owned by appuser so the DB file can be created/written. Best-effort: never
# abort startup if the chown can't run (e.g. read-only or already correct).
mkdir -p /app/data 2>/dev/null || true
chown -R appuser:appuser /app/data 2>/dev/null || true

# Drop root and exec the CMD (default: python -m job_hunter.serve) as appuser.
# `exec` => the serve process becomes PID 1's payload and receives signals.
exec runuser -u appuser -- "$@"
