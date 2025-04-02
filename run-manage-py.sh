#!/bin/bash

# This script is used INSIDE the Dockerfile to start the notebook portal server
source /opt/venv/bin/activate

source /srv/caper/config.sh

#python3 /srv/caper/manage.py runserver 0.0.0.0:8000 >>/srv/logs/stdout.txt 2>>/srv/logs/stderr.txt
python /srv/caper/manage.py runserver 0.0.0.0:8000 >>/srv/logs/stdout.txt 2>&1

