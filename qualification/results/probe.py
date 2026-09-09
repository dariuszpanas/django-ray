"""A fresh Django process proves historical result reads have no application effects."""

if __name__ == "__main__":
    import copy
    import json
    import pickle
    import sys
    import types
    from pathlib import Path

    from asgiref.sync import async_to_sync
    from django.conf import settings

    import django_ray
    from qualification.results.contract import CASES, assertions_for

    if len(sys.argv) != 4 or sys.argv[2] not in CASES:
        raise SystemExit("expected an owned fixture directory, fixed case and candidate module")
    if not __debug__:
        raise SystemExit("result-read qualification requires assertions enabled")
    root, kind = Path(sys.argv[1]), sys.argv[2]
    module_location = str(Path(django_ray.__file__).resolve())
    assert module_location == str(Path(sys.argv[3]).resolve()), "probe imported another candidate"
    assert root.is_dir() and not root.is_symlink() and not any(root.iterdir())
    marker = root / "side-effect"
    sys.path.insert(0, str(root))
    settings.configure(
        SECRET_KEY="isolated-inert-result-probe",
        USE_TZ=True,
        INSTALLED_APPS=["django_ray"],
        DATABASES={
            "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(root / "db.sqlite3")}
        },
        TASKS={"default": {"BACKEND": "django_ray.backends.RayTaskBackend"}},
        DJANGO_RAY={"RAY_ADDRESS": "auto"},
    )
    import django

    django.setup()
    from django.core.management import call_command
    from django.db import connections
    from django.tasks import TaskResultStatus, task, task_backends
    from django.tasks.base import Task
    from django.tasks.exceptions import TaskResultMismatch

    from django_ray.models import RayTaskExecution, TaskState

    call_command("migrate", verbosity=0)

    @task
    def preserved_task(value):
        raise AssertionError("result read executed application code")

    @task
    def other_task(value):
        raise AssertionError("result read executed another task")

    kept_task = preserved_task
    if kind == "unimported":
        (root / "row_selected_module.py").write_text(
            "from pathlib import Path\n"
            + "Path("
            + repr(str(marker))
            + ").write_text('imported')\n"
            + "raise RuntimeError('row selected an import')\n"
        )
        path = "row_selected_module.missing"
    elif kind == "module_attribute":
        module = types.ModuleType("row_selected_module")

        def unexpected_attribute(name):
            marker.write_text("attribute access")
            raise AttributeError(name)

        module.__getattr__ = unexpected_attribute
        sys.modules[module.__name__] = module
        path = "row_selected_module.missing"
    else:
        path = kept_task.module_path
        del preserved_task

    row = RayTaskExecution.objects.create(
        task_id="historical-result",
        callable_path=path,
        # A removed queue must not make a terminal result unreadable either.
        queue_name="removed-queue",
        state=TaskState.SUCCEEDED,
        args_json="[42]",
        kwargs_json="{}",
        result_data='"execution-failed"',
    )
    backend = task_backends["default"]
    refused_operations = 0
    for result in (
        backend.get_result(row.task_id),
        async_to_sync(backend.aget_result)(row.task_id),
    ):
        assert result.status == TaskResultStatus.SUCCESSFUL
        assert result.return_value == "execution-failed"
        assert result.args == [42]
        assert result.task.module_path == path
        result.refresh()
        async_to_sync(result.arefresh)()
        assert result.return_value == "execution-failed"
        assert not marker.exists()
        operations = (
            lambda result=result: result.task.call(42),
            lambda result=result: async_to_sync(result.task.acall)(42),
            lambda result=result: result.task.enqueue(42),
            lambda result=result: async_to_sync(result.task.aenqueue)(42),
            lambda result=result: result.task.using(),
            lambda result=result: backend.enqueue(result.task, (42,), {}),
            lambda result=result: async_to_sync(backend.aenqueue)(result.task, (42,), {}),
            lambda result=result: Task.enqueue(result.task, 42),
            lambda result=result: async_to_sync(Task.aenqueue)(result.task, 42),
            lambda result=result: copy.copy(result.task),
            lambda result=result: copy.deepcopy(result.task),
            lambda result=result: pickle.dumps(result.task),
            lambda result=result: pickle.dumps(result),
            lambda result=result: copy.deepcopy(result),
        )
        for operation in operations:
            try:
                operation()
            except TypeError as error:
                assert "read-only" in str(error)
                refused_operations += 1
            else:
                raise AssertionError("historical result became executable or reconstructible")

    if kind == "removed_task":
        for get in (kept_task.get_result, async_to_sync(kept_task.aget_result)):
            assert get(row.task_id).task.func is kept_task.func
        for get in (other_task.get_result, async_to_sync(other_task.aget_result)):
            try:
                get(row.task_id)
            except TaskResultMismatch:
                pass
            else:
                raise AssertionError("mismatched task accepted the result")
    if kind == "unimported":
        assert "row_selected_module" not in sys.modules
    assert not marker.exists()
    assert refused_operations == 28
    assert RayTaskExecution.objects.count() == 1
    current = other_task.enqueue(7)
    assert current.task is other_task
    assert other_task.get_result(current.id).task.func is other_task.func
    assert async_to_sync(other_task.aget_result)(current.id).task.func is other_task.func
    assert RayTaskExecution.objects.count() == 2
    connections.close_all()
    print(
        json.dumps(
            {
                "schema_version": 1,
                "kind": kind,
                "status": "passed",
                "module_location": module_location,
                "assertions": assertions_for(kind),
                "refused_operations": refused_operations,
            }
        )
    )
