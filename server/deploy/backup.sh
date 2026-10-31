#!/bin/sh
# Daily consistent copy of the SQLite database, 14 days kept.
# /etc/cron.d/we-weekends-backup: 30 3 * * * root /opt/we-weekends/repo/server/deploy/backup.sh
set -eu
DIR=/var/backups/we-weekends
mkdir -p "$DIR"
chmod 700 "$DIR"
STAMP=$(TZ=Europe/Bratislava date +%Y-%m-%d)
docker exec we-weekends python -c "import sqlite3; s=sqlite3.connect('/data/weekends.sqlite'); d=sqlite3.connect('/tmp/backup.sqlite'); s.backup(d); d.close()"
# /tmp is a tmpfs inside the container, so stream the file out.
docker exec we-weekends cat /tmp/backup.sqlite > "$DIR/weekends-$STAMP.sqlite"
docker exec we-weekends rm -f /tmp/backup.sqlite
chmod 600 "$DIR/weekends-$STAMP.sqlite"
find "$DIR" -name 'weekends-*.sqlite' -mtime +14 -delete
