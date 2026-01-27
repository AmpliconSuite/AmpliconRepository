#!/bin/bash

# This script is for LOCAL DEVELOPMENT ONLY
# It starts Django with the development server instead of Gunicorn
source /opt/venv/bin/activate

source /srv/caper/config.sh

cd /srv/caper
python /srv/caper/manage.py runserver 0.0.0.0:8000 >>/srv/logs/stdout.txt 2>&1

