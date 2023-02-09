#!/usr/bin/env python3.8

import argparse
import subprocess

##########################################
# Get the arguments passed to the script #
##########################################

# Handle the --data and --port options
parser = argparse.ArgumentParser(description='Start the Docker container for the Amplicon Repository')
#parser.add_argument('-c', '--config', type=str, default='/caper', help='Set the config directory to be mounted in the container')
parser.add_argument('-p', '--port', type=int, default=80, help='Set the port the repository will be available at')
parser.add_argument('-d', '--database', type=str, default='/home/ubuntu/caper/caper/', help='Set the path of the application directory')

# Parse the arguments
args = parser.parse_args()

##########################################
# Start the Notebook Library             #
##########################################

try:
    subprocess.Popen(f'docker run --rm \
                                  --name=amplicon \
                                  -p {args.port}:8000 \
                                  -v {args.database}:/srv/caper/ \
                                  amplicon:latest \
                                  >> /srv/caper/caperout.txt 2>&1'.split())
    
except KeyboardInterrupt:
    print('Shutting down Amplicon Repository')
