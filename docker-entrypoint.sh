#!/bin/sh
# Container entrypoint: start as ROOT, then DROP to the non-root appuser
# (uid 10001) before exec'ing the serve process.
#
# The DB now lives in a separate PostgreSQL service (see docker-compose.yml), so
# there is no host-mounted data dir to chown anymore — this script only performs
# the privilege drop.
#
# Privilege-drop tool: `runuser` (util-linux, present at /usr/sbin/runuser in
# python:3.9-slim / debian bookworm-slim — verified). The slim base has NO
# `su-exec` and no `gosu`; installing either would add an apt layer. `runuser`
# is already in the base image, needs no PAM password, and forwards signals to
# its child. We `exec` it so it replaces this shell (becomes PID 1's process)
# and the kernel delivers SIGTERM/SIGINT straight to it -> graceful shutdown.
set -e

# Drop root and exec the CMD (default: python -m job_hunter.serve) as appuser.
# `exec` => the serve process becomes PID 1's payload and receives signals.
exec runuser -u appuser -- "$@"
