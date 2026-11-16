#!/usr/bin/env bash
# Regenerate requirements.lock: every dependency, transitive ones included,
# pinned to an exact version with sha256 hashes, resolved on the same base
# image the service runs on (python:3.12-slim, linux/amd64 like Cloud Run).
#
# Needs Docker and network access to PyPI. Run from anywhere:
#   sahayakai-main/services/voice-server/scripts/lock.sh
# then review and commit requirements.lock.
#
# Without Docker, uv resolves the same target from PyPI metadata:
#   uv pip compile requirements.in --python-version 3.12 \
#     --python-platform x86_64-manylinux_2_28 --generate-hashes --no-annotate \
#     [--exclude-newer <ISO time>] -o requirements.lock
# The first lock (2026-11-16) used --exclude-newer 2026-10-03T06:40:00Z, the build time of
# revision 00037, to match what production was running as closely as can be recovered. The Dockerfile installs ONLY from
# it (--require-hashes), so an image is reproducible and an upgrade is a diff.
set -euo pipefail
cd "$(dirname "$0")/.."
docker run --rm --platform linux/amd64 -v "$PWD":/src -w /src python:3.12-slim bash -ceu '
  pip install --quiet --no-cache-dir pip-tools==7.4.1
  pip-compile --quiet --generate-hashes --allow-unsafe --strip-extras \
    --output-file requirements.lock requirements.in
'
echo "Wrote $(pwd)/requirements.lock ($(grep -c '==' requirements.lock) pinned packages)."
