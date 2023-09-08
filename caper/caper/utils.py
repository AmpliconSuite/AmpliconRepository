from pymongo import MongoClient
from allauth.account.adapter import DefaultAccountAdapter
from django import forms
from django.contrib.auth import get_user_model
from allauth.account.models import EmailAddress
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter

from django.conf import settings

def get_db_handle(db_name, host):
    client = MongoClient(host
                        )
    db_handle = client[db_name]
    return db_handle, client

def get_collection_handle(db_handle,collection_name):
    return db_handle[collection_name]

def create_run_display(project):
    runs = project['runs']
    run_list = []
    for run in runs:
        for sample in runs[run]:
            for key in list(sample.keys()):
                newkey = key.replace(" ", "_")
                sample[newkey] = sample.pop(key)
            run_list.append(sample)
    return run_list

# since we use email and/or username to control project visibility,
# we don't want a new, unknown user to come in and register an account
# where the 'username' matches an existing account's email address.
# We also don't want an email address that matches an existing username
class CustomAccountAdapter(DefaultAccountAdapter):
    def clean_username(self, username, *args, **kwargs):
        User = get_user_model()
        users = User.objects.filter(email=username)

        if len(users) >= 1 :
            raise forms.ValidationError(f"{username} has already been registered to an account.")
        return super().clean_username(username)

    def clean_email(self, email):
        User = get_user_model()
        users = User.objects.filter(username=email)

        if len(users) >= 1:
            raise forms.ValidationError(f"{email} has already been registered to an account.")
        return super().clean_email(email)


class SocialAccountAdapter(DefaultSocialAccountAdapter):
    def pre_social_login(self, request, sociallogin):
        """
        Invoked just after a user successfully authenticates via a
        social provider, but before the login is actually processed
        (and before the pre_social_login signal is emitted).

        We're trying to solve different use cases:
        - social account already exists, just go on
        - social account has no email or email is unknown, just go on
        - social account's email exists, link social account to existing user
        """

        # Ignore existing social accounts, just do this stuff for new ones
        if sociallogin.is_existing:
            return

        # some social logins don't have an email address, e.g. facebook accounts
        # with mobile numbers only, but allauth takes care of this case so just
        # ignore it
        if 'email' not in sociallogin.account.extra_data:
            return

        # check if given email address already exists.
        # Note: __iexact is used to ignore cases
        try:
            email = sociallogin.account.extra_data['email'].lower()
            email_address = EmailAddress.objects.get(email__iexact=email)

        # if it does not, let allauth take care of this new social account
        except EmailAddress.DoesNotExist:
            return

        # if it does, connect this new social login to the existing user
        user = email_address.user
        sociallogin.connect(request, user)