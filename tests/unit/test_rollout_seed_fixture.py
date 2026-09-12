"""The shared seed fixture also runs with the package-only qualification apps."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("sample_installed", [False, True])
def test_rollout_seed_respects_installed_apps_in_fresh_registry(sample_installed):
    root = Path(__file__).resolve().parents[2]
    program = """
import contextlib
import sys
from types import SimpleNamespace
from django.conf import settings
sample_installed = sys.argv[1] == 'True'
settings.configure(
    SECRET_KEY='fixture-only', USE_TZ=True,
    INSTALLED_APPS=['django_ray'] + (['testproject'] if sample_installed else []),
    DATABASES={'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}},
    TASKS={'default': {'BACKEND': 'django_ray.backends.RayTaskBackend'}},
    DJANGO_RAY={'RAY_ADDRESS': 'auto'},
)
import django
django.setup()
from django.core.management import call_command
call_command('migrate', verbosity=0, interactive=False)
call_command('flush', verbosity=0, interactive=False)
from tests.conftest import _restore_execution_protocol_rollout_seed
values = {
    'django_db_setup': None,
    'django_db_blocker': SimpleNamespace(unblock=contextlib.nullcontext),
}
request = SimpleNamespace(
    node=SimpleNamespace(get_closest_marker=lambda name: object()),
    fixturenames=['transactional_db'], getfixturevalue=values.__getitem__,
)
_restore_execution_protocol_rollout_seed.__wrapped__(request)
_restore_execution_protocol_rollout_seed.__wrapped__(request)
from django_ray.models import TaskExecutionProtocolPolicy, LegacyWorkerAdmissionToken
assert TaskExecutionProtocolPolicy.objects.count() == 1
assert TaskExecutionProtocolPolicy.objects.get().active_write_protocol_version == 1
assert LegacyWorkerAdmissionToken.objects.count() == 1
if sample_installed:
    from testproject.models import SampleAdmissionBudget
    assert SampleAdmissionBudget.objects.count() == 1
else:
    assert 'testproject.models' not in sys.modules
print('rollout seed verified')
"""
    environment = dict(os.environ)
    environment.pop("DJANGO_SETTINGS_MODULE", None)
    result = subprocess.run(
        [sys.executable, "-c", program, str(sample_installed)],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "rollout seed verified" in result.stdout
