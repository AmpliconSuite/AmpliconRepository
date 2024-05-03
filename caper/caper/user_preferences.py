from bson.objectid import ObjectId
from .utils import get_collection_handle, db_handle
from .forms import UserPreferencesForm
from django.contrib.auth import get_user_model
from django.conf import settings
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.core.mail import EmailMessage

user_preferences_handle = get_collection_handle(db_handle,'user_preferences')

def get_user_preferences(user):
    latest = user_preferences_handle.find_one({'email': user.email})
    if latest == None:
        return set_default_user_preferences(user)
    return latest

def set_default_user_preferences(user):
    prefs = {'onRemovedFromProjectTeam': True, 'onAddedToProjectTeam': True, 'email': user.email, 'welcomeMessage': True}
    user_preferences_handle.insert_one(prefs)
    return prefs

def update_user_preferences(user, prefs_dict):
    old_prefs = get_user_preferences(user)
    prefs_dict['email'] = user.email
    if (old_prefs == None):
        user_preferences_handle.insert_one(prefs_dict)
    else:
        query = {'_id': ObjectId(old_prefs['_id'])}
        new_val = {"$set":  prefs_dict}
        res = user_preferences_handle.update_one(query, new_val)
        print (f"Updated { res } --  {  prefs_dict }")


def notify_users_of_project_membership_change(user, old_membership, new_membership, project_name, project_id):

    print(' user is ' + user.email  + "   " + user.username)
    # we ignore the user making the change and do not email them
    ignore_set = {user.email, user.username}

    old_set = set(old_membership) - ignore_set
    new_set = set(new_membership) - ignore_set

    if old_set == new_set:
        print("Membership unchanged")
        return

    removed = old_set - new_set
    added = new_set - old_set
    print(' removed is ')
    print(*removed, sep=", ")
    print(' added is ')
    print(*added, sep=", ")

    # now get the prefs for each and send an email if they elcted to get them
    for add_user_id in added:
        user_obj = get_user_obj(add_user_id)
        # make sure we have email, not username since thats what the prefs are keyed on
        if user_obj is not None:
            added_user_prefs = get_user_preferences(user_obj)
            emailOK = True # default to sending email

            if added_user_prefs is not None:
                emailOK = added_user_prefs['onAddedToProjectTeam']

            if emailOK:
                print("send project add email to " + user_obj.email)
                send_added_to_project_membership_email( user_obj.email, user.email, project_name, project_id)



    for remove_user_id in removed:
        user_obj = get_user_obj(remove_user_id)
        # make sure we have email, not username since thats what the prefs are keyed on
        if user_obj is not None:
            removed_user_prefs = get_user_preferences(user_obj)
            emailOK = True # default to sending email
            if removed_user_prefs is not None:
                emailOK = remove_user_prefs['onRemovedFromProjectTeam']

            if emailOK:
                print("send project remove email to " + user_obj.email)
                send_removed_from_project_membership_email( user_obj.email, user.email, project_name, project_id)



def get_user_obj(usernameOrEmail):
    User = get_user_model()
    try:
        user_obj = User.objects.get(username=usernameOrEmail)
        return user_obj
    except User.DoesNotExist:
        try:
            user_obj = User.objects.get(email=usernameOrEmail)
            return user_obj
        except User.DoesNotExist:
            return None


def send_removed_from_project_membership_email( to_email, sharing_user_email, project_name, project_id):
    subject = f"You have been removed from project { project_name } on {settings.SITE_TITLE}"
    send_project_membership_changed_email(subject, 'contacts/project_unshared_mail_template.html', to_email, sharing_user_email,
                                          project_name, project_id)


def send_added_to_project_membership_email(to_email, sharing_user_email, project_name, project_id):
    subject = f"Project { project_name } on {settings.SITE_TITLE} has been shared to you"
    send_project_membership_changed_email(subject, 'contacts/project_shared_mail_template.html', to_email, sharing_user_email,
                                          project_name, project_id)


def send_project_membership_changed_email(subject, template, to_email, sharing_user_email, project_name, project_id):
    form_dict = {}
    # add details for the template
    form_dict['SITE_TITLE'] = settings.SITE_TITLE
    form_dict['SITE_URL'] = settings.SITE_URL
    form_dict['sharing_user_email'] = sharing_user_email
    form_dict['project_name'] = project_name
    form_dict['project_id'] = project_id

    html_message = render_to_string(template, form_dict)
    plain_message = strip_tags(html_message)

    # send_mail(subject = subject, message = body, from_email = settings.EMAIL_HOST_USER_SECRET, recipient_list = [settings.RECIPIENT_ADDRESS])
    email = EmailMessage(
        subject,
        html_message,
        settings.EMAIL_HOST_USER_SECRET,
        [to_email],
        reply_to=[settings.EMAIL_HOST_USER_SECRET]
    )
    email.content_subtype = "html"
    email.send(fail_silently=False)
