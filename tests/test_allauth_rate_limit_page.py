"""allauth's rate limits must answer 429, not 500.

allauth renders a top-level ``429.html`` when a limit fires. The template was
missing, so a client that hit the signup limit got ``TemplateDoesNotExist`` and
a 500 (311 of them on prod on 2026-10-14, all from one scanner).
"""
import uuid

import pytest
from allauth.account import app_settings
from allauth import ratelimit
from django.test import Client
from django.urls import reverse


pytestmark = pytest.mark.integration


def test_signup_rate_limit_returns_429_page():
    amount = ratelimit.parse(app_settings.RATE_LIMITS['signup']).amount
    # A fresh address per run, so counters left in the shared file cache by an
    # earlier run cannot change the count.
    n = uuid.uuid4().int
    client = Client(HTTP_HOST='localhost',
                    REMOTE_ADDR=f'10.{n % 250}.{(n >> 8) % 250}.{(n >> 16) % 250}')
    url = reverse('account_signup')

    for _ in range(amount):
        assert client.post(url, {}).status_code != 429

    response = client.post(url, {})

    assert response.status_code == 429
    assert b'Too many requests' in response.content
