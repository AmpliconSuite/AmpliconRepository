source caper/config.sh
docker run -d --rm --name=amplicon -p 80:8000 -v /home/ubuntu/caper/caper:/srv/caper/ --env GOOGLE_SECRET_KEY --env GLOBUS_SECRET_KEY --env DB_URI amplicon:latest
#docker run -d --rm --name=amplicon -p 80:8000 -v /home/ubuntu/caper/caper/:/srv/caper/ amplicon:latest
