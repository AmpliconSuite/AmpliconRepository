#!/usr/bin/env python
import os
import sys
import signal

def sighandler(signum, frame):
    sys.exit(1)

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, sighandler)
    signal.signal(signal.SIGINT, sighandler)
    
    from mezzanine.utils.conf import real_project_name

    settings_module = "%s.settings" % real_project_name("caper")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", settings_module)

    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)
