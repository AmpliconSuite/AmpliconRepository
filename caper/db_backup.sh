#!/bin/bash
DATE=$(date +%Y%m%d)
BACKUP_DIR="../backups"
DB_PATH="./caper.sqlite3"

# Create backup with timestamp

# Compress the backup
gzip $BACKUP_DIR/backup_$DATE.sqlite3

# Copy to off-site storage (example using AWS S3)
aws s3 cp $BACKUP_DIR/backup_$DATE.sqlite3.gz s3://amprepo-backups/prod/sqlite/
