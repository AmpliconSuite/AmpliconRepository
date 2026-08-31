source caper/config.sh

#echo "/srv/caper/manage.py runserver 0.0.0.0:8000 &1>/srv/logs/stdout.txt &2>/srv/logs/stderr.txt"
#docker run  --rm --name=amplicon-dev -p 8080:8000 -v /home/ubuntu/AmpliconRepository-dev/logs:/srv/logs  -v /home/ubuntu/AmpliconRepository-dev/caper:/srv/caper/ -w /srv/  --env GOOGLE_SECRET_KEY --env GLOBUS_SECRET_KEY --env DB_URI_SECRET --env DB_NAME -it genepattern/amplicon-repo:dev1 bash


#docker run -d --rm  --name=amplicon-prod -p 80:8000 -v /home/ubuntu/AmpliconRepository-prod/logs:/srv/logs -v /home/ubuntu/AmpliconRepository-prod/caper:/srv/caper/ -w /srv/  --env GOOGLE_SECRET_KEY --env GLOBUS_SECRET_KEY --env DB_URI_SECRET --env DB_NAME  --env S3_STATIC_FILES -t genepattern/amplicon-repo:dev /srv/run-manage-py.sh

#docker rm amplicon-prod

# --restart and --memory are set here because they are properties of the
# container, and a container only gets them at `docker run`. Both servers were
# given `unless-stopped` and an 8 GiB cap on 2026-08-25, after an unbounded
# container took the host down; that was applied to the containers already
# running and never written into this script. So the documented rebuild
# procedure -- stop, rm, rebuild the image, run this -- quietly handed back a
# container with no cap and no restart policy, and nothing would have said so
# until the next time it mattered. Measured 2026-08-31: dev and prod both
# running unless-stopped at 8589934592 bytes, this script setting neither.
#
# Overridable so a host with different memory does not need this file edited,
# defaulting to what both servers actually run today.
AMPLICON_RESTART_POLICY=${AMPLICON_RESTART_POLICY:-unless-stopped}
AMPLICON_MEMORY_LIMIT=${AMPLICON_MEMORY_LIMIT:-8g}

docker run -d --network="host" --restart ${AMPLICON_RESTART_POLICY} --memory ${AMPLICON_MEMORY_LIMIT}  --name=amplicon-${AMPLICON_ENV} -p ${AMPLICON_ENV_PORT}:8000 -v /home/ubuntu/.aws:/root/.aws  -v ${CAPER_ROOT}:${CAPER_ROOT}  -v ${CAPER_ROOT}/logs:/srv/logs -v ${CAPER_ROOT}:/srv/ -w /srv/caper  -v ${CAPER_ROOT}/.git:/srv/.git --env CAPER_ROOT --env NEO4J_PASSWORD_SECRET --env EMAIL_HOST_USER --env EMAIL_HOST_PASSWORD --env DJANGO_SECRET_KEY --env GOOGLE_SECRET_KEY --env GLOBUS_SECRET_KEY --env DB_URI_SECRET --env DB_NAME --env SITE_URL --env S3_STATIC_FILES --env S3_FILE_DOWNLOADS --env SECRET_KEY  -t genepattern/amplicon-repo:${AMPLICON_ENV} /srv/run-manage-py.sh
