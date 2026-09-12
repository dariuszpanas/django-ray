"""Observer-only settings for the actual bounded admin presentation method.

SimpleAdminConfig postpones admin imports until the observer installs its task
import poison. No manager or Ray process uses these additional applications.
No HTML rendering or public HTTP service is configured by this module.
"""

from qualification.upgrade import runtime_settings as _runtime_settings

globals().update({name: value for name, value in vars(_runtime_settings).items() if name.isupper()})

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.admin.apps.SimpleAdminConfig",
    "django_ray",
]
