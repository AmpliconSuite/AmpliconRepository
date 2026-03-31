#!/usr/bin/env python

import os
import shutil
import smtplib
from email.message import EmailMessage

##################################################################################
# Set this script up to run via cron. It is designed to use the same environment #
# variables already found in config.sh. The cron should look like:               #
#                                                                                #
# */5 * * * * . /path/to/config.sh && /path/to/check_disk.py > /dev/null 2>&1    #
##################################################################################

# Disk Configuration
THRESHOLD = float(os.environ.get('DISK_THRESHOLD', 90.0))
CHECK_PATH = os.environ.get('CHECK_PATH', '/')

# Email Configuration
EMAIL_HOST = os.environ.get('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', 587))
EMAIL_FROM = os.environ.get('EMAIL_FROM', 'dev@ampliconrepository.org')

# Handle boolean conversion for TLS (defaults to True)
tls_env = str(os.environ.get('EMAIL_USE_TLS', 'True')).lower()
EMAIL_USE_TLS = tls_env in ('true', '1', 't', 'yes', 'y')

# Handle list of emails (expects a comma-separated string if overridden via environment)
default_to_emails = 'gp-dev@broadinstitute.org,jensluebeck@ucsd.edu'
email_to_raw = os.environ.get('EMAIL_TO', default_to_emails)
EMAIL_TO = [email.strip() for email in email_to_raw.split(',') if email.strip()]


def check_disk_usage(path):
    """Returns the percentage of disk used for the given path."""
    total, used, free = shutil.disk_usage(path)
    percent_used = (used / total) * 100
    return percent_used


def send_alert_email(percent_used):
    """Constructs and sends the alert email via SMTP."""
    msg = EmailMessage()
    msg.set_content(
        f"Warning: Disk space on partition '{CHECK_PATH}' has reached {percent_used:.2f}% capacity.\n\n"
    )

    msg['Subject'] = f"CRITICAL: High Amplicon Disk Usage ({percent_used:.2f}%) on {CHECK_PATH}"
    msg['From'] = EMAIL_FROM
    msg['To'] = ', '.join(EMAIL_TO)

    try:
        # Connect to the SMTP server
        with smtplib.SMTP(EMAIL_HOST, EMAIL_PORT) as server:
            server.ehlo()

            if EMAIL_USE_TLS:
                server.starttls()
                server.ehlo()

            if EMAIL_HOST_USER and EMAIL_HOST_PASSWORD:
                server.login(EMAIL_HOST_USER, EMAIL_HOST_PASSWORD)

            server.send_message(msg)
            print(f"Alert email sent to: {', '.join(EMAIL_TO)}")

    except Exception as e:
        print(f"Error: Failed to send email. Details: {e}")


if __name__ == '__main__':
    current_usage = check_disk_usage(CHECK_PATH)

    if current_usage >= THRESHOLD:
        send_alert_email(current_usage)
    else:
        print(f"Disk usage is {current_usage:.2f}%. Safely below the {THRESHOLD}% threshold.")
