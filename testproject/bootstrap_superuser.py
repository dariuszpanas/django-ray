"""Explicit create-only bootstrap for the local evaluation setup Job."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping


def bootstrap_superuser(environment: Mapping[str, str]) -> bool:
    """Create a new administrator only when requested; never rotate implicitly."""
    enabled = environment.get("DJANGO_BOOTSTRAP_SUPERUSER", "false")
    if enabled == "false":
        return False
    if enabled != "true":
        raise ValueError("DJANGO_BOOTSTRAP_SUPERUSER must be true or false")
    required = (
        "DJANGO_SUPERUSER_USERNAME",
        "DJANGO_SUPERUSER_EMAIL",
        "DJANGO_SUPERUSER_PASSWORD",
    )
    if any(not environment.get(key) for key in required):
        raise ValueError("bootstrap requires explicit username, email, and password")
    username, email, password = (environment[key] for key in required)
    if (
        not re.fullmatch(r"[\w.@+-]{1,150}", username)
        or not re.fullmatch(r"[^\s@]+@[^\s@]+", email)
        or len(email) > 254
    ):
        raise ValueError("bootstrap requires a valid explicit username and email")
    if (
        not 32 <= len(password) <= 512
        or len(set(password)) < 5
        or any(ord(char) < 32 for char in password)
    ):
        raise ValueError(
            "bootstrap password must contain 32-512 characters with sufficient variety"
        )

    from django.contrib.auth import get_user_model
    from django.db import transaction

    user_model = get_user_model()
    with transaction.atomic():
        if user_model.objects.filter(username=username).exists():
            raise ValueError(
                "bootstrap account already exists; disable bootstrap for reapply; "
                "use manage.py changepassword for explicit rotation"
            )
        user_model.objects.create_superuser(username=username, email=email, password=password)
    return True


def main() -> None:
    import django

    django.setup()
    created = bootstrap_superuser(os.environ)
    print("Bootstrap account created" if created else "Superuser bootstrap disabled")


if __name__ == "__main__":
    main()
