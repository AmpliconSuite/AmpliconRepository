#!/bin/bash

cd "$(dirname "${BASH_SOURCE[0]}")/../.." || exit 1   # run from the repository root, wherever this is invoked from

source ./caper/config.sh

# Define variables
S3_BUCKET="s3://amprepobucket/${AMPLICON_ENV}/static/"
LOCAL_DIR="./caper/static/"

# Sync local directory to S3 bucket
aws s3 sync $LOCAL_DIR $S3_BUCKET
