#!/bin/bash
# Nightly DB backup for INNAIT — both venues.
# Run via cron at 02:30 UTC. Keeps last 14 daily snapshots.
# Critical: settlement_log + fill_ledger + pm_snapshots are irreplaceable
# without re-querying Kalshi/PM API for months of history.

set -euo pipefail

BACKUP_DIR=/root/backups
mkdir -p "$BACKUP_DIR"
TS=$(date -u +%Y%m%d_%H%M%S)

# Sources
LIP_DB=/root/lip-maker/data/lip_maker.db
PM_DB=/root/polymarket-maker/data/polymarket_maker.db

# Use sqlite3 .backup (atomic, no lock contention with running services)
for src in "$LIP_DB" "$PM_DB"; do
    if [ -f "$src" ]; then
        name=$(basename "$src" .db)
        dest="$BACKUP_DIR/${name}_${TS}.db"
        sqlite3 "$src" ".backup '$dest'"
        # Compress to save disk
        gzip -9 "$dest"
        echo "$(date -u +%FT%TZ) backed up $src -> ${dest}.gz ($(du -h ${dest}.gz | cut -f1))"
    else
        echo "$(date -u +%FT%TZ) WARNING: $src not found"
    fi
done

# Retention: keep last 14 daily snapshots (delete older)
find "$BACKUP_DIR" -name "*.db.gz" -mtime +14 -delete

# Disk usage check
du -sh "$BACKUP_DIR"
