#!/bin/bash

# This script is used INSIDE the Dockerfile to start the notebook portal server
source /opt/venv/bin/activate

source /srv/caper/config.sh

# Run Django with Gunicorn for production
# The application module is caper.wsgi:application (Django project name is 'caper')
cd /srv/caper
gunicorn caper.wsgi:application \
    --config /srv/gunicorn_config.py \
    --log-file /srv/logs/gunicorn.log \
    2>&1 | tee -a /srv/logs/stdout.txt

# Old development server command (kept for reference):
# python /srv/caper/manage.py runserver 0.0.0.0:8000 >>/srv/logs/stdout.txt 2>&1


