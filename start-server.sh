source caper/config.sh

#echo "/srv/caper/manage.py runserver 0.0.0.0:8000 &1>/srv/logs/stdout.txt &2>/srv/logs/stderr.txt"
#docker run  --rm --name=amplicon-dev -p 8080:8000 -v /home/ubuntu/AmpliconRepository-dev/logs:/srv/logs  -v /home/ubuntu/AmpliconRepository-dev/caper:/srv/caper/ -w /srv/  --env GOOGLE_SECRET_KEY --env GLOBUS_SECRET_KEY --env DB_URI --env DB_NAME -it genepattern/amplicon-repo:dev1 bash


#docker run -d --rm  --name=amplicon-prod -p 80:8000 -v /home/ubuntu/AmpliconRepository-prod/logs:/srv/logs -v /home/ubuntu/AmpliconRepository-prod/caper:/srv/caper/ -w /srv/  --env GOOGLE_SECRET_KEY --env GLOBUS_SECRET_KEY --env DB_URI --env DB_NAME  --env S3_STATIC_FILES -t genepattern/amplicon-repo:dev /srv/run-manage-py.sh

docker rm amplicon-prod
docker run -d  --name=amplicon-prod -p 8000:8000 -v /home/ubuntu/AmpliconRepository-prod/logs:/srv/logs -v /home/ubuntu/AmpliconRepository-prod/caper:/srv/caper/ -w /srv/caper  --env GOOGLE_SECRET_KEY --env GLOBUS_SECRET_KEY --env DB_URI --env DB_NAME  --env S3_STATIC_FILES -t genepattern/amplicon-repo:dev /srv/run-manage-py.sh



