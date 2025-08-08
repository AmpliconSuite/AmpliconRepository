#!/bin/bash
source ./config.sh
DATE=$(date +%Y%m%d)
BACKUP_DIR="../backups"
DB_PATH="./caper.sqlite3"
BACKUP_FILE="$BACKUP_DIR/backup_$DATE.sqlite3"

# Create backup with timestamp
#docker exec -it amplicon-${AMPLICON_ENV} sqlite3 $DB_PATH ".backup $BACKUP_DIR/backup_$DATE.sqlite3"


# Check if a backup for today already exists (either compressed or uncompressed)
if [ -f "$BACKUP_FILE" ] || [ -f "$BACKUP_FILE.gz" ]; then
    echo "Backup for today already exists, incrementing version numbers"
    
    # Determine the highest version number
    highest_version=0
    for existing in $(find $BACKUP_DIR -name "backup_${DATE}.sqlite3*" | sort); do
        if [[ "$existing" =~ \.([0-9]+)(\.gz)?$ ]] && [[ ! "$existing" =~ \.gz\.[0-9]+$ ]]; then
            version="${BASH_REMATCH[1]}"
            if (( version > highest_version )); then
                highest_version=$version
            fi
        fi
    done
    
    # Rename files in reverse order (highest version first to avoid conflicts)
    for current_version in $(seq $highest_version -1 1); do
        # Handle compressed files
        if [ -f "$BACKUP_DIR/backup_${DATE}.sqlite3.$current_version.gz" ]; then
            new_version=$((current_version + 1))
            mv "$BACKUP_DIR/backup_${DATE}.sqlite3.$current_version.gz" "$BACKUP_DIR/backup_${DATE}.sqlite3.$new_version.gz"
        fi
        
        # Handle uncompressed files
        if [ -f "$BACKUP_DIR/backup_${DATE}.sqlite3.$current_version" ]; then
            new_version=$((current_version + 1))
            mv "$BACKUP_DIR/backup_${DATE}.sqlite3.$current_version" "$BACKUP_DIR/backup_${DATE}.sqlite3.$new_version"
        fi
    done
    
    # Move the unversioned file to version 1
    if [ -f "$BACKUP_FILE.gz" ]; then
        mv "$BACKUP_FILE.gz" "$BACKUP_FILE.1.gz"
    fi
    if [ -f "$BACKUP_FILE" ]; then
        mv "$BACKUP_FILE" "$BACKUP_FILE.1"
    fi
fi

# Create the new backup
docker exec -it amplicon-${AMPLICON_ENV} sqlite3 $DB_PATH ".backup $BACKUP_FILE"

# Compress the backup
gzip $BACKUP_DIR/backup_$DATE.sqlite3

# Copy to off-site storage (example using AWS S3)
aws s3 cp $BACKUP_DIR/backup_$DATE.sqlite3.gz s3://amprepo-backups/prod/sqlite/

# Setup retention periods (in days)
DAILY_RETENTION=7
WEEKLY_RETENTION=28  # 4 weeks
MONTHLY_RETENTION=180  # ~6 months
QUARTERLY_RETENTION=730  # ~2 years

echo "Starting backup retention policy enforcement..."

# Ensure backup directory exists
mkdir -p $BACKUP_DIR

# Function to safely remove a file from both local and S3
remove_backup() {
    local file=$1
    local s3_path=$2
    local basename=$(basename "$file")
    
    echo "Removing old backup: $basename"
    
    # Remove from local storage
    rm "$file" 2>/dev/null || echo "Warning: Could not remove local file $basename (may already be removed)"
    
    # Remove from S3
    aws s3 rm "$s3_path/$basename" 2>/dev/null || echo "Warning: Could not remove S3 file $basename (may already be removed)"
}

# Process backups based on retention policy
process_backups() {
    echo "Processing backups for retention policy..."
    
    # Get list of backup files sorted by date
    local backups=($(find $BACKUP_DIR -name "backup_*.sqlite3.gz" | sort))
    
    # Loop through backups from oldest to newest
    for backup in "${backups[@]}"; do
        # Extract date from filename (format: backup_YYYYMMDD.sqlite3.gz)
        filename=$(basename "$backup")
        backup_date=$(echo "$filename" | grep -o "[0-9]\{8\}")
        
        # Calculate age in days
        backup_timestamp=$(date -d "${backup_date:0:4}-${backup_date:4:2}-${backup_date:6:2}" +%s 2>/dev/null || date -j -f "%Y%m%d" "$backup_date" +%s)
        current_timestamp=$(date +%s)
        age_days=$(( (current_timestamp - backup_timestamp) / 86400 ))
        
        # Apply retention policy
        keep=false
        
        # Keep if daily backup and less than DAILY_RETENTION days old
        if [ $age_days -lt $DAILY_RETENTION ]; then
            keep=true
        # Keep if weekly backup (first backup of the week) and less than WEEKLY_RETENTION days old
        elif [ $age_days -lt $WEEKLY_RETENTION ] && [[ "$backup_date" == *"Mon"* ]]; then
            keep=true
        # Keep if monthly backup (first backup of the month) and less than MONTHLY_RETENTION days old
        elif [ $age_days -lt $MONTHLY_RETENTION ] && [[ "${backup_date:6:2}" == "01" ]]; then
            keep=true
        # Keep if quarterly backup (first backup of quarter) and less than QUARTERLY_RETENTION days old
        elif [ $age_days -lt $QUARTERLY_RETENTION ] && [[ "${backup_date:4:4}" == "0101" || "${backup_date:4:4}" == "0401" || "${backup_date:4:4}" == "0701" || "${backup_date:4:4}" == "1001" ]]; then
            keep=true
        fi
        
        # Remove if not marked to keep
        if [ "$keep" = false ]; then
            remove_backup "$backup" "s3://amprepo-backups/prod/sqlite"
        fi
    done
}

# Run the retention policy process
process_backups

echo "Backup and retention policy enforcement completed."