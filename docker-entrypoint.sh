#!/bin/sh
set -e

# Ensure folder exists and open permissions for all users on the host
REC_DIR=${RECORDINGS_DIR:-/app/recordings}
mkdir -p "$REC_DIR"
chmod 0777 "$REC_DIR" || true

# Ensure newly created files get 0666 (umask 000)
umask 000

exec python -m app.main


