FROM ubuntu:20.04

MAINTAINER Forrest Kim <f1kim@health.ucsd.edu>

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

#############################################
##      Start the webapp                   ##
#############################################
RUN mkdir /srv/logs/
COPY ./run-manage-py.sh /srv/run-manage-py.sh
RUN apt-get update && apt-get install vim --yes

CMD ["/srv/run-manage-py.sh","&"]