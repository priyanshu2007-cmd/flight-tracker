#!/bin/sh
# Nightly consistent SQLite backup to Cloud Storage. Usage: backup.sh gs://bucket
set -eu
cd "$HOME/flight-tracker"
sqlite3 data/tracker.db ".backup data/backup.db"
gcloud storage cp data/backup.db "$1/tracker-$(date -u +%Y%m%dT%H%M).db" --quiet
