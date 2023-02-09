kill -9 $(ps aux | grep '[p]ython /srv/caper/manage.py' | awk '{print $2}')
