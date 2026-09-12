"""Actual SQLite historical reads; synthetic rows do not prove native execution."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from qualification.upgrade import runtime_history as history
from qualification.upgrade import runtime_steps as steps
from tests.unit.test_upgrade_runtime_settings import environment as environment
from tests.unit.test_upgrade_runtime_settings import module as module

# The caller supplies an already installed, isolated released interpreter. This
# test never installs dependencies implicitly or substitutes current package
# code for released APIs. Only the qualifier overlay is copied into its path.
_INSTALLED_BASELINE_PROBE = r"""
import importlib.metadata
import json
import os
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, sys.argv[1])
import django_ray
assert django_ray.__version__ == importlib.metadata.version('django-ray') == '0.4.0'
assert importlib.metadata.version('ray') == '2.56.0'
assert Path(django_ray.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
assert not (Path(django_ray.__file__).parent / 'workflow' / 'progress').exists()
import ray
def forbidden(*args, **kwargs):
    raise AssertionError('history probe must not initialize or shut down Ray')
ray.init = ray.shutdown = forbidden

from django.db.backends.base.base import BaseDatabaseWrapper
original_connect = BaseDatabaseWrapper.connect
def sqlite_only(self):
    assert self.vendor == 'sqlite', 'history probe must not connect to PostgreSQL'
    return original_connect(self)
BaseDatabaseWrapper.connect = sqlite_only
from qualification.upgrade import runtime_history_settings as loader
assert loader.DATABASES['default']['ENGINE'] == 'django.db.backends.postgresql'
assert loader.INSTALLED_APPS[-1] == 'django_ray'
from django.conf import settings
values = {name: value for name, value in vars(loader).items() if name.isupper()}
values['DATABASES'] = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
settings.configure(**values)
import django
django.setup()
from django.core.management import call_command
call_command('migrate', verbosity=0, interactive=False, skip_checks=True)
from django.db import transaction
from django.utils import timezone
from django_ray.input_storage import prepare_task_input, register_task_input
from django_ray.models import RayTaskExecution, TaskAttempt
from django_ray.result_storage import get_result_storage_backend
from django_ray.runtime.runtime_env import normalize_runtime_env, runtime_env_for_storage
from qualification.upgrade import runtime_history as history
from qualification.upgrade import runtime_steps as steps
assert 'qualification.upgrade.runtime_tasks' not in sys.modules
assert 'execution_protocol_version' not in {f.name for f in RayTaskExecution._meta.fields}
root = steps._root()
(root / 'observations').mkdir()
states = ('SUCCEEDED', 'FAILED', 'CANCELLED', 'SUCCEEDED', 'SUCCEEDED')
for case, state in zip(steps.OLD_CASES, states, strict=True):
    args = {'old-success': [case, steps.PRESERVED_PAYLOAD], 'old-failure': [],
            'old-cancel': [case], 'old-retry': [], 'old-gated': [case]}[case]
    prepared = prepare_task_input(args, {})
    with transaction.atomic():
        register_task_input(prepared)
    task_id = str(uuid4())
    stored = runtime_env_for_storage(normalize_runtime_env(
        settings.DJANGO_RAY['RUNTIME_ENV_PROFILES']['upgrade'], profile='upgrade'), task_id=task_id)
    suffix = {'old-success': 'value', 'old-failure': 'failed', 'old-retry': 'retried'}.get(case, 'gated_effect')
    row = RayTaskExecution.objects.create(
        task_id=task_id, callable_path='qualification.upgrade.runtime_tasks.' + suffix,
        queue_name='upgrade-jobs', state=state, attempt_number=2 if case == 'old-retry' else 1,
        args_json=prepared.args_json, kwargs_json=prepared.kwargs_json,
        input_reference=prepared.input_reference, runtime_env_profile=stored.profile,
        runtime_env_json=stored.serialized, runtime_env_hash=stored.digest,
        started_at=timezone.now(), finished_at=timezone.now(),
        error_message='synthetic failed case' if state == 'FAILED' else None)
    identity = dict(task_pk=row.pk, task_id=row.task_id, attempt=row.attempt_number,
                    generation=row.execution_generation, package_version='0.4.0', ray_version='2.56.0',
                    native_job_id='synthetic-native-job', context_protocol=None)
    if state == 'SUCCEEDED':
        value = {'identity': identity, 'payload': steps.PRESERVED_PAYLOAD, 'workflow_result': 42} if case == 'old-success' else identity
        serialized = json.dumps(value, sort_keys=True, separators=(',', ':'))
        if case == 'old-success':
            row.result_reference = get_result_storage_backend().store(serialized_result=serialized)
        else:
            row.result_data = serialized
        steps._write_once(root / 'runtime-effects' / f'{case}.{row.attempt_number}.committed.json',
                          dict(schema=1, case=case, phase='committed', observed_at=timezone.now().isoformat(), identity=identity))
    if case == 'old-success':
        row.workflow_run_id = uuid4()
        row.progress_data = json.dumps(dict(
            schema_version=2, run_identity=dict(schema_version=1, run_id=str(row.workflow_run_id),
                task_execution_pk=row.pk, attempt_number=row.attempt_number, execution_generation=row.execution_generation),
            revision=3, state='SUCCEEDED', total_nodes=2, completed_nodes=2, failed_nodes=0,
            running_nodes=0, pending_nodes=0, progress_percent=100.0,
            graph=dict(nodes=[dict(id='a'), dict(id='b')], edges=[dict(source='a', target='b')])) )
    row.save()
    if case == 'old-retry':
        TaskAttempt.objects.create(execution=row, attempt_number=1, state='FAILED')
    TaskAttempt.objects.create(execution=row, attempt_number=row.attempt_number, state=state,
                               result_data=row.result_data, result_reference=row.result_reference)
    steps._write_once(steps._case_file(case), dict(case=case, task_pk=row.pk, task_id=row.task_id, callable_path=row.callable_path))
steps.history()
before = steps._rows_snapshot()
observed = history.observe_runtime_history()
assert steps._rows_snapshot() == before
assert TaskAttempt.objects.count() == 6
assert observed['cases_read'] == observed['encrypted_environments_read'] == 5
assert observed['external_input_read'] is observed['external_result_read'] is True
assert observed['public_results_read'] == observed['inert_reenqueue_refusals'] == 0
assert observed['workflow']['schema'] == 2
assert observed['workflow']['succeeded'] == observed['workflow']['nodes'] == 2
assert observed['workflow']['edges'] == 1
assert observed['rendered_workflow_verified'] is False
assert observed['artifact_negative_checks'] == 'not_run'
assert 'qualification.upgrade.runtime_tasks' not in sys.modules
assert 'django_ray.workflow_progress' in sys.modules
assert 'django_ray.workflow.progress.runs' not in sys.modules
assert not ray.is_initialized()
print(json.dumps(dict(package='0.4.0', ray='2.56.0', cases_read=5, attempts_preserved=6,
                     synthetic_rows=True, native_execution_verified=False)))
"""


@pytest.fixture
def fresh_callable_namespace(monkeypatch):
    # Only isolate pytest's imported fixture module; production refuses cached
    # modules instead of deleting somebody else's application import state.
    monkeypatch.delitem(sys.modules, history._TASK_MODULE, raising=False)
    monkeypatch.delattr(
        importlib.import_module("qualification.upgrade"), "runtime_tasks", raising=False
    )


@pytest.fixture
def retained_history(module, settings, monkeypatch, fresh_callable_namespace):
    from django_ray.input_storage import prepare_task_input
    from django_ray.result_storage import get_result_storage_backend
    from django_ray.runtime.runtime_env import normalize_runtime_env, runtime_env_for_storage

    settings.TASKS = module["TASKS"]
    settings.DJANGO_RAY = module["DJANGO_RAY"]
    root = steps._root()
    (root / "observations").mkdir()

    def migrate(name):
        executor = MigrationExecutor(connection)
        target = [("django_ray", name)]
        executor.migrate(target)
        return executor.loader.project_state(target).apps

    old_apps = migrate("0018_workflow_run_allocation")
    old_model = old_apps.get_model("django_ray", "RayTaskExecution")
    attempts_model = old_apps.get_model("django_ray", "TaskAttempt")
    input_model = old_apps.get_model("django_ray", "TaskInputPayload")
    rows = []
    try:
        for case, state in zip(
            steps.OLD_CASES,
            ("SUCCEEDED", "FAILED", "CANCELLED", "SUCCEEDED", "SUCCEEDED"),
            strict=True,
        ):
            args = {
                "old-success": [case, steps.PRESERVED_PAYLOAD],
                "old-failure": [],
                "old-cancel": [case],
                "old-retry": [],
                "old-gated": [case],
            }[case]
            prepared = prepare_task_input(args, {})
            if prepared.input_reference:
                input_model.objects.create(
                    reference=prepared.input_reference,
                    digest=prepared.digest,
                    size_bytes=prepared.size_bytes,
                    envelope_version=prepared.envelope_version,
                    backend=prepared.backend,
                )
            task_id = "history-" + case
            spec = json.loads(json.dumps(module["DJANGO_RAY"]["RUNTIME_ENV_PROFILES"]["upgrade"]))
            spec["env_vars"]["DJANGO_RAY_UPGRADE_BUILD"] = "baseline"
            environment = runtime_env_for_storage(
                normalize_runtime_env(spec, profile="upgrade"), task_id=task_id
            )
            row = old_model.objects.create(
                task_id=task_id,
                callable_path=history._TASK_MODULE
                + "."
                + {
                    "old-success": "value",
                    "old-failure": "failed",
                    "old-cancel": "gated_effect",
                    "old-retry": "retried",
                    "old-gated": "gated_effect",
                }[case],
                queue_name="upgrade-jobs",
                state=state,
                args_json=prepared.args_json,
                kwargs_json=prepared.kwargs_json,
                input_reference=prepared.input_reference,
                runtime_env_json=environment.serialized,
                runtime_env_hash=environment.digest,
                runtime_env_profile=environment.profile,
                attempt_number=2 if case == "old-retry" else 1,
                started_at=timezone.now(),
                finished_at=timezone.now(),
                error_message="deliberate-upgrade-application-failure"
                if state == "FAILED"
                else None,
                error_traceback="qualification.upgrade.runtime_tasks.FixtureTerminalError: historical"
                if state == "FAILED"
                else None,
            )
            identity = {
                "task_pk": row.pk,
                "task_id": row.task_id,
                "attempt": row.attempt_number,
                "generation": row.execution_generation,
                "package_version": "0.4.0",
                "ray_version": "2.56.0",
                "python": "3.12.14",
                "implementation": "cpython",
                "native_job_id": "01000000",
                "context_protocol": None,
            }
            if state == "SUCCEEDED":
                result = (
                    {
                        "identity": identity,
                        "payload": steps.PRESERVED_PAYLOAD,
                        "workflow_result": 42,
                    }
                    if case == "old-success"
                    else identity
                )
                serialized = json.dumps(result, sort_keys=True, separators=(",", ":"))
                if case == "old-success":
                    row.result_reference = get_result_storage_backend().store(
                        serialized_result=serialized
                    )
                else:
                    row.result_data = serialized
                steps._write_once(
                    root / "runtime-effects" / f"{case}.{row.attempt_number}.committed.json",
                    {
                        "schema": 1,
                        "case": case,
                        "phase": "committed",
                        "observed_at": timezone.now().isoformat(),
                        "identity": identity,
                    },
                )
            if case == "old-success":
                row.workflow_run_id = uuid4()
                row.progress_data = json.dumps(
                    {
                        "schema_version": 2,
                        "run_identity": {
                            "schema_version": 1,
                            "run_id": str(row.workflow_run_id),
                            "task_execution_pk": row.pk,
                            "attempt_number": row.attempt_number,
                            "execution_generation": row.execution_generation,
                        },
                        "revision": 3,
                        "state": "SUCCEEDED",
                        "total_nodes": 2,
                        "completed_nodes": 2,
                        "failed_nodes": 0,
                        "running_nodes": 0,
                        "pending_nodes": 0,
                        "progress_percent": 100.0,
                        "graph": {
                            "nodes": [{"id": "increment"}, {"id": "double"}],
                            "edges": [{"source": "increment", "target": "double"}],
                        },
                    }
                )
            row.save()
            if case == "old-retry":
                attempts_model.objects.create(
                    execution=row, attempt_number=1, state="FAILED", error_message="first attempt"
                )
            attempts_model.objects.create(
                execution=row,
                attempt_number=row.attempt_number,
                state=state,
                result_data=row.result_data,
                result_reference=row.result_reference,
                error_message=row.error_message,
                workflow_progress_summary_json=None,
            )
            steps._write_once(
                steps._case_file(case),
                {
                    "case": case,
                    "task_pk": row.pk,
                    "task_id": row.task_id,
                    "callable_path": row.callable_path,
                },
            )
            rows.append(row)
        snapshot = {}
        for model in (old_model, attempts_model, input_model):
            fields = [field.attname for field in model._meta.concrete_fields]
            snapshot[model.__name__] = {
                "fields": fields,
                "rows": list(model.objects.order_by(model._meta.pk.attname).values(*fields)),
            }
        steps._write_once(root / "observations" / "historical-rows.json", snapshot)
        migrate("0035_activate_current_cohort")
        yield SimpleNamespace(root=root, rows=rows, old_apps=old_apps)
    finally:
        old_model.objects.all().delete()
        migrate("0035_activate_current_cohort")


@pytest.mark.django_db(transaction=True)
def test_current_observer_reads_migrated_released_fields_and_keeps_history(
    retained_history, monkeypatch
):
    import ray

    from django_ray.models import TaskAttempt

    monkeypatch.setattr(
        ray, "init", lambda *args, **kwargs: pytest.fail("historical read started Ray")
    )
    before = steps._rows_snapshot()
    artifact_before = {
        path: path.read_bytes() for path in retained_history.root.rglob("*") if path.is_file()
    }
    result = history.observe_runtime_history()
    assert result["cases_read"] == result["public_results_read"] == 5
    assert result["inert_reenqueue_refusals"] == result["unsupported_retry_refusals"] == 5
    assert result["encrypted_environments_read"] == 5
    assert result["external_input_read"] is result["external_result_read"] is True
    assert result["workflow"] == {
        "schema": 2,
        "nodes": 2,
        "edges": 1,
        "succeeded": 2,
        "presentation": "LEGACY_ONLY",
        "bounded_graph_available": False,
    }
    assert result["workflow_presentation_verified"] is True
    assert result["rendered_workflow_verified"] is False
    assert result["workflow_html_render"] == result["artifact_negative_checks"] == "not_run"
    assert steps._rows_snapshot() == before
    assert {path: path.read_bytes() for path in artifact_before} == artifact_before
    assert TaskAttempt.objects.count() == 6
    assert not TaskAttempt.objects.exclude(workflow_progress_summary_json=None).exists()
    serialized = json.dumps(result)
    assert steps.PRESERVED_PAYLOAD not in serialized
    assert "fixture-only" not in serialized
    assert str(retained_history.root) not in serialized
    assert history._TASK_MODULE not in sys.modules


def test_cached_callable_requires_fresh_process_before_any_history_read(monkeypatch):
    monkeypatch.setenv("DJANGO_RAY_UPGRADE_BUILD", "candidate")
    monkeypatch.setenv("DJANGO_RAY_UPGRADE_DATABASE", "primary")
    sentinel = object()
    monkeypatch.setitem(sys.modules, history._TASK_MODULE, sentinel)
    monkeypatch.setattr(steps, "history", lambda **kwargs: pytest.fail("read before cache refusal"))
    with pytest.raises(history.HistoryReadError, match="fresh-observer"):
        history.observe_runtime_history()
    assert sys.modules[history._TASK_MODULE] is sentinel


def test_poison_detects_even_a_swallowed_import_attempt(fresh_callable_namespace):
    before = list(sys.meta_path)
    with (
        pytest.raises(history.HistoryReadError, match="callable-imported"),
        history._without_callable_import(),
    ):
        try:
            importlib.import_module(history._TASK_MODULE)
        except history.HistoryReadError:
            pass
    assert sys.meta_path == before


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", ["inputs", "results"])
@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_damaged_owned_fixture_bytes_cannot_be_reported_as_success(retained_history, kind, damage):
    paths = [path for path in (retained_history.root / kind).rglob("*") if path.is_file()]
    assert len(paths) == 1
    # This independently owned test root is never a deployed artifact root.
    if damage == "missing":
        paths[0].unlink()
    else:
        paths[0].write_bytes(b"corrupt fixture bytes")
    with pytest.raises(history.HistoryReadError, match="read-failed|unavailable"):
        history.observe_runtime_history()


@pytest.mark.django_db(transaction=True)
def test_hidden_current_field_mutation_during_public_read_is_detected(
    retained_history, monkeypatch
):
    from django_ray.backends import RayTaskBackend
    from django_ray.models import RayTaskExecution

    original = RayTaskBackend.get_result
    calls = []

    def mutate(self, result_id):
        result = original(self, result_id)
        calls.append(result_id)
        if len(calls) == 5:
            # Original 0.4 snapshot lacks this newer diagnostic field. The
            # additional all-field before/after observation must still catch it.
            RayTaskExecution.objects.filter(task_id=result_id).update(
                managed_with_django_ray_version="0.5.0"
            )
        return result

    monkeypatch.setattr(RayTaskBackend, "get_result", mutate)
    with pytest.raises(history.HistoryReadError, match="mutated-rows"):
        history.observe_runtime_history()


@pytest.mark.django_db(transaction=True)
def test_summary_does_not_accept_fabricated_v3_graph_presentation(retained_history, monkeypatch):
    from django_ray.admin import RayTaskExecutionAdmin

    monkeypatch.setattr(
        RayTaskExecutionAdmin,
        "_lazy_workflow_progress_presentation",
        lambda *args, **kwargs: {"state": "COMPLETE", "complete": True},
    )
    with pytest.raises(history.HistoryReadError, match="presentation-mismatch"):
        history.observe_runtime_history()


def test_history_settings_fresh_import_defers_admin_and_never_imports_ray(environment):
    pytest.importorskip("psycopg")
    source = """
import os, sys
os.environ['DJANGO_SETTINGS_MODULE'] = 'qualification.upgrade.runtime_history_settings'
from django.db.backends.base.base import BaseDatabaseWrapper
def forbidden(*args, **kwargs):
    raise AssertionError('observer import connected to a database')
BaseDatabaseWrapper.connect = forbidden
import django
django.setup()
assert 'qualification.upgrade.runtime_tasks' not in sys.modules
assert 'django_ray.admin' not in sys.modules
from qualification.upgrade.runtime_history import _without_callable_import
with _without_callable_import():
    import django_ray.admin
assert 'ray' not in sys.modules
assert 'qualification.upgrade.runtime_tasks' not in sys.modules
print('fresh-history-observer-imported')
"""
    completed = subprocess.run(
        [sys.executable, "-c", source],
        env=os.environ | environment,
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "fresh-history-observer-imported"


def test_published_baseline_history_in_isolated_interpreter(environment, tmp_path):
    interpreter = os.environ.get("DJANGO_RAY_UPGRADE_BASELINE_PYTHON")
    if not interpreter:
        pytest.skip("requires an explicitly installed isolated django-ray0.4/Ray2.56 interpreter")
    assert Path(interpreter).is_absolute() and Path(interpreter).is_file()
    overlay = tmp_path / "qualifier-overlay"
    upgrade = overlay / "qualification" / "upgrade"
    upgrade.mkdir(parents=True)
    source_root = Path(__file__).resolve().parents[2]
    for name in (
        "runtime_history.py",
        "runtime_history_settings.py",
        "runtime_settings.py",
        "runtime_steps.py",
        "runtime_tasks.py",
    ):
        shutil.copyfile(source_root / "qualification" / "upgrade" / name, upgrade / name)
    completed = subprocess.run(
        [interpreter, "-I", "-c", _INSTALLED_BASELINE_PROBE, str(overlay)],
        env=os.environ | environment | {"DJANGO_RAY_UPGRADE_BUILD": "baseline"},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "package": "0.4.0",
        "ray": "2.56.0",
        "cases_read": 5,
        "attempts_preserved": 6,
        "synthetic_rows": True,
        "native_execution_verified": False,
    }
