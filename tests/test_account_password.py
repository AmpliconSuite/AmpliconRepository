import uuid
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from allauth.account.signals import email_changed
from django.contrib.auth import get_user_model
from django.test import Client
from django.template.loader import get_template
from django.urls import reverse

from caper.forms import MySignUpForm
from caper.utils import CustomAccountAdapter


pytestmark = pytest.mark.integration


@pytest.fixture
def django_client():
    return Client(HTTP_HOST='localhost')


@pytest.fixture
def password_user():
    user_model = get_user_model()
    suffix = uuid.uuid4().hex
    user = user_model.objects.create_user(
        username=f'password_test_{suffix}',
        email=f'password_test_{suffix}@example.com',
        password='CurrentPassword!123',
    )
    try:
        yield user
    finally:
        user.delete()


def test_account_menu_links_to_password_change(django_client, password_user):
    django_client.force_login(password_user)

    response = django_client.get(reverse('account_change_password'))

    assert response.status_code == 200
    assert (
        f'href="{reverse("account_change_password")}"'.encode()
        in response.content
    )
    assert response.content.count(b'type="password"') == 3
    assert b'id="id_oldpassword"' in response.content
    assert b'id="id_password1"' in response.content
    assert b'id="id_password2"' in response.content


def test_password_change_uses_repository_template():
    template = get_template('account/password_change.html')

    assert template.origin.name.endswith(
        'caper/templates/account/password_change.html'
    )


def test_password_change_updates_password_and_keeps_session(
        django_client, password_user):
    django_client.force_login(password_user)

    response = django_client.post(
        reverse('account_change_password'),
        {
            'oldpassword': 'CurrentPassword!123',
            'password1': 'UpdatedPassword!456',
            'password2': 'UpdatedPassword!456',
        },
    )

    assert response.status_code == 302
    assert response.url == reverse('account_change_password')
    password_user.refresh_from_db()
    assert password_user.check_password('UpdatedPassword!456')
    assert django_client.session.get('_auth_user_id') == str(password_user.pk)


def test_password_change_rejects_incorrect_current_password(
        django_client, password_user):
    django_client.force_login(password_user)

    response = django_client.post(
        reverse('account_change_password'),
        {
            'oldpassword': 'WrongPassword!123',
            'password1': 'UpdatedPassword!456',
            'password2': 'UpdatedPassword!456',
        },
    )

    assert response.status_code == 200
    assert b'Please type your current password.' in response.content
    password_user.refresh_from_db()
    assert password_user.check_password('CurrentPassword!123')


@pytest.mark.parametrize(
    ('password1', 'password2'),
    [
        ('Short7!', 'Short7!'),
        ('UpdatedPassword!456', 'DifferentPassword!789'),
    ],
)
def test_password_change_rejects_invalid_new_password(
        django_client, password_user, password1, password2):
    django_client.force_login(password_user)

    response = django_client.post(
        reverse('account_change_password'),
        {
            'oldpassword': 'CurrentPassword!123',
            'password1': password1,
            'password2': password2,
        },
    )

    assert response.status_code == 200
    password_user.refresh_from_db()
    assert password_user.check_password('CurrentPassword!123')


def test_registration_rejects_password_shorter_than_eight_characters():
    suffix = uuid.uuid4().hex
    form = MySignUpForm(
        data={
            'username': f'short_password_{suffix}',
            'email': f'short_password_{suffix}@example.com',
            'password1': 'Short7!',
            'password2': 'Short7!',
        }
    )
    form.fields.pop('captcha')

    assert not form.is_valid()
    assert 'password1' in form.errors
    assert 'at least 8 characters' in str(form.errors['password1'])


def test_user_without_password_cannot_open_password_change(
        django_client, password_user):
    password_user.set_unusable_password()
    password_user.save(update_fields=['password'])
    django_client.force_login(password_user)

    response = django_client.get(reverse('account_change_password'))

    assert response.status_code == 302
    assert response.url == reverse('profile')


def test_user_without_password_has_no_password_menu_or_set_password(
        django_client, password_user):
    password_user.set_unusable_password()
    password_user.save(update_fields=['password'])
    django_client.force_login(password_user)

    email_response = django_client.get(reverse('account_email'))
    set_response = django_client.get(reverse('account_set_password'))

    assert email_response.status_code == 200
    assert (
        f'href="{reverse("account_change_password")}"'.encode()
        not in email_response.content
    )
    assert set_response.status_code == 302
    assert set_response.url == reverse('profile')


def test_user_without_password_cannot_request_password_reset(
        django_client, password_user):
    password_user.set_unusable_password()
    password_user.save(update_fields=['password'])

    with patch.object(CustomAccountAdapter, 'send_mail') as send_mail:
        response = django_client.post(
            reverse('account_reset_password'),
            {'email': password_user.email},
        )

    assert response.status_code == 200
    assert b'does not have a password to reset' in response.content
    send_mail.assert_not_called()


def test_primary_email_change_migrates_mongo_references():
    from caper.account_signals import migrate_email_references

    projects = Mock()
    preferences = Mock()

    migrate_email_references(
        'old@example.com',
        'new@example.com',
        projects_collection=projects,
        preferences_collection=preferences,
    )

    assert projects.update_many.call_args_list == [
        (
            (
                {'project_members': 'old@example.com'},
                {'$addToSet': {'project_members': 'new@example.com'}},
            ),
            {},
        ),
        (
            (
                {'project_members': 'old@example.com'},
                {'$pull': {'project_members': 'old@example.com'}},
            ),
            {},
        ),
        (
            (
                {'subscribers': 'old@example.com'},
                {'$addToSet': {'subscribers': 'new@example.com'}},
            ),
            {},
        ),
        (
            (
                {'subscribers': 'old@example.com'},
                {'$pull': {'subscribers': 'old@example.com'}},
            ),
            {},
        ),
    ]
    preferences.update_many.assert_called_once_with(
        {'email': 'old@example.com'},
        {'$set': {'email': 'new@example.com'}},
    )


def test_primary_email_change_signal_runs_migration(password_user):
    from caper import account_signals

    with patch.object(account_signals, 'migrate_email_references') as migrate:
        email_changed.send(
            sender=type(password_user),
            request=None,
            user=password_user,
            from_email_address=SimpleNamespace(email='old@example.com'),
            to_email_address=SimpleNamespace(email='new@example.com'),
        )

    migrate.assert_called_once_with('old@example.com', 'new@example.com')
