"""Own one short-lived Admin session in the disposable qualification database."""

import os
from contextlib import contextmanager


@contextmanager
def qualification_admin_session():
    """Supply a cookie for real HTTP reads and prove its deletion on every exit.

    This trusted fixture creates a database session; it does not test password
    login or CSRF. The caller must also test anonymous denial over HTTP.
    """
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.contrib.sessions.backends.db import SessionStore

    if (
        os.environ.get("DJANGO_SETTINGS_MODULE") != "testproject.settings_qualification"
        or settings.SESSION_ENGINE != "django.contrib.sessions.backends.db"
        or settings.AUTHENTICATION_BACKENDS != ["django.contrib.auth.backends.ModelBackend"]
    ):
        raise ValueError("Admin session requires the disposable qualification configuration")
    user = get_user_model().objects.get(username="qualification")
    if not (user.is_active and user.is_staff and user.is_superuser):
        raise ValueError("The disposable qualification administrator is not active")
    session = SessionStore()
    try:
        session.set_expiry(900)
        session["_auth_user_id"] = str(user.pk)
        session["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
        session["_auth_user_hash"] = user.get_session_auth_hash()
        session.save()
        if not session.session_key:
            raise ValueError("Qualification Admin session was not created")
        yield f"{settings.SESSION_COOKIE_NAME}={session.session_key}"
    finally:
        key = session.session_key
        if key:
            session.delete(key)
            if session.exists(key):
                raise ValueError("Qualification Admin session cleanup was not observed")
