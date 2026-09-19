"""Admin observation must not leave its temporary authenticated session behind."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from qualification.application.workflow_session import qualification_admin_session


@pytest.fixture
def session_fixture(monkeypatch, settings):
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings_qualification")
    settings.SESSION_ENGINE = "django.contrib.sessions.backends.db"
    settings.AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend"]
    user = SimpleNamespace(
        is_active=True,
        is_staff=True,
        is_superuser=True,
        pk=1,
        get_session_auth_hash=lambda: "fixture-hash",
    )
    model = Mock()
    model.objects.get.return_value = user
    monkeypatch.setattr("django.contrib.auth.get_user_model", lambda: model)

    class Session(dict):
        session_key = None
        deleted = False
        expiry = None

        def set_expiry(self, value):
            self.expiry = value

        def save(self):
            self.session_key = "fixture-session"

        def delete(self, key):
            assert key == "fixture-session"
            self.deleted = True

        def exists(self, key):
            return not self.deleted

    session = Session()
    monkeypatch.setattr("django.contrib.sessions.backends.db.SessionStore", lambda: session)
    return session, user


@pytest.mark.parametrize("fail", [False, True])
def test_session_cleanup_on_success_and_observation_failure(session_fixture, fail):
    session, _ = session_fixture
    try:
        with qualification_admin_session() as cookie:
            assert cookie.endswith("=fixture-session")
            assert session.expiry == 900
            assert session["_auth_user_id"] == "1"
            if fail:
                raise RuntimeError("observation failed")
    except RuntimeError:
        assert fail
    assert session.deleted


def test_session_cleanup_must_be_observed(session_fixture):
    session, _ = session_fixture
    session.delete = lambda _key: None
    with pytest.raises(ValueError, match="cleanup was not observed"):
        with qualification_admin_session():
            pass


def test_inactive_administrator_cannot_create_session(session_fixture):
    session, user = session_fixture
    user.is_active = False
    with pytest.raises(ValueError, match="not active"):
        with qualification_admin_session():
            pass
    assert session.session_key is None


def test_nonqualification_environment_cannot_create_session(session_fixture, monkeypatch):
    session, _ = session_fixture
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "testproject.settings")
    with pytest.raises(ValueError, match="disposable qualification"):
        with qualification_admin_session():
            pass
    assert session.session_key is None
