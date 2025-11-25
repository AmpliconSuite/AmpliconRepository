FROM ubuntu:20.04

MAINTAINER Forrest Kim <f1kim@health.ucsd.edu>

#############################################
##      Arguments                          ##
#############################################

ARG AA_USER
ARG AA_GROUP
ARG UID
ARG GID
# ARG ACCOUNT_AUTHENTICATED_LOGIN_REDIRECTS
# ARG GOOGLE_SECRET_KEY
# ARG GLOBUS_SECRET_KEY
# ARG ACCOUNT_DEFAULT_HTTP_PROTOCOL
# ARG SECURE_SSL_REDIRECT
# ARG DB_URI
# ARG S3_FILE_DOWNLOADS
# ARG AWS_PROFILE_NAME
# ARG S3_DOWNLOADS_BUCKET_PATH
# ARG S3_STATIC_FILES

#############################################
##      System updates                     ##
#############################################

RUN apt-get -y update && \
    apt-get -y upgrade && \
    apt-get -y install wget git bzip2 libcurl4-gnutls-dev gcc python3-dev libmysqlclient-dev && \
    apt-get purge && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*
ENV LANG C.UTF-8

#############################################
##      Create the config directory        ##
#############################################

RUN mkdir /config

#############################################
##      Install Python                     ##
#############################################

RUN apt-get update && apt-get install -y \
    python3.8 \
    python3-pip \
    python3-venv

#############################################
##      Create the webapp environment     ##
#############################################

COPY ./requirements.txt /src/requirements.txt
RUN python3 -m venv /opt/venv
RUN /bin/bash -c "source /opt/venv/bin/activate && \
    pip install -r /src/requirements.txt"

#############################################
##      Set-up working directory           ##
#############################################

WORKDIR /srv/caper/
COPY ./caper/ /srv/caper/
# RUN git clone git@github.com:mesirovlab/caper.git /srv/caper/

#############################################
##      Configure the repository           ##
#############################################

# # Add settings.py to config dir
# RUN cp /caper/settings.py /config/settings.py
# RUN rm /caper/settings.py
# RUN ln -s /srv/config/settings.py /srv/caper/caper/settings.py

# # Add the templates to the config dir
# RUN cp -r /srv/caper/templates /config/
# RUN rm -r /srv/caper/templates
# RUN ln -s /config/templates /srv/caper/templates

# # Add the static files to the config dir
# RUN cp -r /srv/caper/static /config/
# RUN rm -r /srv/caper/static
# RUN ln -s /config/static /srv/caper/static

RUN /bin/bash -c "source /opt/venv/bin/activate && \
	source /srv/caper/config.sh && \
	/srv/caper/manage.py makemigrations"
RUN /bin/bash -c "source /opt/venv/bin/activate && \
	source /srv/caper/config.sh && \
	/srv/caper/manage.py migrate --run-syncdb"
RUN /bin/bash -c "source /opt/venv/bin/activate && \
	source /srv/caper/config.sh && \
	/srv/caper/manage.py collectstatic --noinput"


# COPY ./start-server.sh /srv/caper/start-server.sh
# install aws cli
RUN cd /srv  && \
    apt update && \
    apt install -y unzip curl && \
    curl "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o "awscliv2.zip" && \
    unzip awscliv2.zip && \
    ./aws/install
#############################################
##      Start the webapp                   ##
#############################################
RUN mkdir -p /srv/logs/
COPY ./run-manage-py.sh /srv/run-manage-py.sh
RUN apt-get update && apt-get install vim --yes

# Create user if specified
RUN /bin/bash -c "if [[ -z '${UID}' || -z '${AA_USER}' || -z '${GID}' ]] ; \
    then echo 'Running as root'; \
        export AA_USER=root; \
    else echo 'Running as ${UID} ${AA_USER}';  \
        addgroup --gid ${GID} ${AA_GROUP}; \
        useradd -ms /bin/bash -u ${UID} ${AA_USER}; \
        chown ${AA_USER}:${GID} -R /srv; \
        su - ${AA_USER}; \
    fi"

# Check which user is set
RUN whoami
RUN echo ${AA_USER}
USER ${AA_USER}
RUN whoami

EXPOSE 8000
CMD ["/srv/run-manage-py.sh","&"]
