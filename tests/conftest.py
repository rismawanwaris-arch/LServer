import django
import pytest


def pytest_configure():
    django.setup()


@pytest.fixture
def user(db, django_user_model):
    return django_user_model.objects.create_user("op", password="x")
