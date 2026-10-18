"""Finite maintenance and required-encryption proof in the owned application DB."""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import math
import os
import time
from dataclasses import dataclass
from io import StringIO
from uuid import uuid4

from qualification.application import runtime_env_refusal as refusal
from qualification.application.protected_runtime import MARKERS, assert_clear

SECONDS = 60
MAX_ROWS = 800
PAGE_ROWS = 100
MAX_PAGES = 8
MAX_ROW_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
OLD_KEY_ID = "qualification-maintenance-old"
FACTS = {
    "scan": (
        "bounded_inventory",
        "rewrite_range_complete",
        "check_range_complete",
        "expected_blockers_visible",
        "unrelated_rows_preserved",
        "command_output_clear",
    ),
    "plaintext_rewrite": (
        "active_key_authenticated",
        "identity_preserved",
        "worker_succeeded",
        "original_runtime",
    ),
    "old_key_rewrap": (
        "active_key_authenticated",
        "identity_preserved",
        "worker_succeeded",
        "original_runtime",
    ),
    "plaintext_refusal": refusal.FACTS,
    "surfaces": ("api_clear", "owned_terminal_rows_removed"),
}


def empty_evidence() -> dict:
    return {case: dict.fromkeys(facts, False) for case, facts in FACTS.items()}


def validate_evidence(value: object) -> None:
    if not isinstance(value, dict) or set(value) != set(FACTS):
        raise ValueError("RuntimeEnv maintenance evidence has invalid cases")
    for case, names in FACTS.items():
        facts = value[case]
        if (
            not isinstance(facts, dict)
            or set(facts) != set(names)
            or any(facts[name] is not True for name in names)
        ):
            raise ValueError("RuntimeEnv maintenance evidence is incomplete")


def remaining(deadline: float) -> float:
    value = deadline - time.monotonic()
    if not math.isfinite(value) or value <= 0:
        raise ValueError("RuntimeEnv maintenance observation deadline expired")
    return value


class BoundedOutput(StringIO):
    def __init__(self):
        super().__init__()
        self.bytes_written = 0

    def write(self, text):
        self.bytes_written += len(text.encode())
        if self.bytes_written > 32 * 1024:
            raise ValueError("RuntimeEnv maintenance command output exceeded its bound")
        return super().write(text)


@dataclass(frozen=True, repr=False)
class Snapshot:
    pk: int
    task_id: str
    profile: str
    digest: str
    serialized: str
    other_fields: str


def inventory() -> dict[int, Snapshot]:
    """Bound bytes before hydration; compare all other columns inside the DB."""
    from django.db.models import (
        Case,
        Count,
        F,
        Func,
        IntegerField,
        Max,
        Sum,
        TextField,
        Value,
        When,
    )
    from django.db.models.functions import MD5, Cast, Coalesce, Concat, JSONObject

    from django_ray.models import RayTaskExecution

    class ByteLength(Func):
        function = "OCTET_LENGTH"
        output_field = IntegerField()

        def as_sqlite(self, compiler, connection, **extra_context):
            return self.as_sql(
                compiler,
                connection,
                template="LENGTH(CAST(%(expressions)s AS BLOB))",
                **extra_context,
            )

    metadata = {"task_id": 255, "runtime_env_profile": 100, "runtime_env_hash": 64}
    rows = RayTaskExecution.objects.annotate(
        snapshot_bytes=ByteLength("runtime_env_json"),
        **{f"size_{name}": ByteLength(name) for name in metadata},
    )
    raw_bytes = Coalesce(F("snapshot_bytes"), 0) + 32
    for name in metadata:
        raw_bytes += Coalesce(F(f"size_{name}"), 0)
    limits = rows.aggregate(
        count=Count("pk"),
        total=Sum(raw_bytes),
        largest=Max("snapshot_bytes"),
        **{f"largest_{name}": Max(f"size_{name}") for name in metadata},
    )
    if (
        limits["count"] > MAX_ROWS
        or (limits["total"] or 0) > MAX_TOTAL_BYTES
        or (limits["largest"] or 0) > MAX_ROW_BYTES
        or any((limits[f"largest_{name}"] or 0) > cap for name, cap in metadata.items())
    ):
        raise ValueError("RuntimeEnv maintenance inventory exceeded its bound")
    names = [
        item.attname
        for item in RayTaskExecution._meta.concrete_fields
        if item.attname != "runtime_env_json"
    ]
    # PostgreSQL's jsonb_build_object has a finite argument count. Chunking also
    # keeps this fixed-width comparison portable to the resource-free SQLite seam.
    chunks = [
        Cast(JSONObject(**{name: F(name) for name in names[start : start + 20]}), TextField())
        for start in range(0, len(names), 20)
    ]
    rows = rows.annotate(
        other_fields=MD5(Concat(*chunks, output_field=TextField())),
        bounded_snapshot=Case(
            When(snapshot_bytes__lte=MAX_ROW_BYTES, then=F("runtime_env_json")),
            default=Value(None),
            output_field=TextField(),
        ),
        **{
            f"bounded_{name}": Case(
                When(**{f"size_{name}__lte": cap}, then=F(name)),
                default=Value(None),
                output_field=TextField(),
            )
            for name, cap in metadata.items()
        },
    )
    result = {}
    total = 0
    for row in (
        rows.order_by("pk")
        .values_list(
            "pk",
            "bounded_task_id",
            "bounded_runtime_env_profile",
            "bounded_runtime_env_hash",
            "bounded_snapshot",
            "other_fields",
        )[: MAX_ROWS + 1]
        .iterator(chunk_size=PAGE_ROWS)
    ):
        snapshot = Snapshot(*row)
        if not isinstance(snapshot.serialized, str):
            raise ValueError("RuntimeEnv maintenance inventory has no snapshot")
        size = len(snapshot.serialized.encode())
        total += (
            size
            + 32
            + sum(
                len(value.encode())
                for value in (snapshot.task_id, snapshot.profile, snapshot.digest)
                if isinstance(value, str)
            )
        )
        if size > MAX_ROW_BYTES or total > MAX_TOTAL_BYTES or len(result) >= MAX_ROWS:
            raise ValueError("RuntimeEnv maintenance inventory changed beyond its bound")
        result[snapshot.pk] = snapshot
    if len(result) != limits["count"]:
        raise ValueError("RuntimeEnv maintenance inventory changed during observation")
    return result


def expected_blockers(row_ids: list[int], before: dict, *, manager_prefix: str) -> dict[int, str]:
    if (
        len(row_ids) not in {2, 4}
        or len(set(row_ids)) != len(row_ids)
        or any(type(pk) is not int or pk <= 0 for pk in row_ids)
    ):
        raise ValueError("RuntimeEnv maintenance has invalid negative fixture identities")
    result = {}
    for index, pk in enumerate(row_ids):
        item = before.get(pk)
        if item is None:
            raise ValueError("RuntimeEnv maintenance lost a negative fixture")
        case = refusal.CASES[index % 2]
        fixture = refusal.Fixture(case, pk, item.task_id, item.serialized, item.digest)
        # Four IDs are assembled only from the validated before-core receipt
        # followed by this generation's two live-manager refusals. The original
        # owner may have stopped during the cold generation transition.
        if (
            refusal.inspect_failure(
                fixture,
                manager_prefix=manager_prefix,
                historical_owner=len(row_ids) == 4 and index < 2,
            )
            is None
        ):
            raise ValueError("RuntimeEnv maintenance negative fixture is not terminal")
        result[pk] = "invalid_snapshot" if case == refusal.CASES[0] else "unavailable_key"
    return result


def fixture_configuration() -> dict:
    from django.conf import settings

    from django_ray.conf.settings import get_settings

    config = copy.deepcopy(get_settings())
    if (
        settings.DEBUG
        or config["RUNTIME_ENV_STORAGE_MODE"] != "required"
        or config["RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY"] != "qualification"
        or set(config["RUNTIME_ENV_ENCRYPTION_KEYS"]) != {"qualification"}
        or config["RUNTIME_ENV_ENCRYPTION_DJANGO_SECRET_FALLBACK"] is not False
    ):
        raise ValueError("RuntimeEnv maintenance requires the fixed active-key configuration")
    key = base64.urlsafe_b64decode(config["RUNTIME_ENV_ENCRYPTION_KEYS"]["qualification"] + "=")
    old_key = hmac.digest(
        key, b"django-ray/qualification/runtime-env-maintenance/v1", hashlib.sha256
    )
    config["RUNTIME_ENV_ENCRYPTION_KEYS"][OLD_KEY_ID] = (
        base64.urlsafe_b64encode(old_key).rstrip(b"=").decode()
    )
    return config


def create_probes(config: dict) -> tuple[refusal.Fixture, ...]:
    from importlib.metadata import version

    from django.db import transaction

    from django_ray.models import RayTaskExecution
    from django_ray.runtime.runtime_env import resolve_runtime_env_profile, runtime_env_for_storage

    queue = "maintenance-" + uuid4().hex
    runtime = resolve_runtime_env_profile("thin")
    old_config = {**config, "RUNTIME_ENV_ENCRYPTION_ACTIVE_KEY": OLD_KEY_ID}
    fixtures = []
    with transaction.atomic():
        for case in ("plaintext_rewrite", "old_key_rewrap"):
            task_id = str(uuid4())
            serialized = (
                runtime.serialized
                if case == "plaintext_rewrite"
                else runtime_env_for_storage(
                    runtime,
                    task_id=task_id,
                    config=old_config,
                ).serialized
            )
            if len(serialized.encode()) > MAX_ROW_BYTES:
                raise ValueError("RuntimeEnv maintenance probe exceeded its snapshot bound")
            row = RayTaskExecution.objects.create(
                task_id=task_id,
                callable_path=refusal.CALLABLE,
                queue_name=queue,
                priority=100,
                state="QUEUED",
                created_with_django_ray_version=version("django-ray"),
                runtime_env_profile=runtime.profile,
                runtime_env_json=serialized,
                runtime_env_hash=runtime.digest,
                ray_target_address=config["RAY_ADDRESS"],
                kwargs_json='{"package":"django-ray"}',
                timeout_seconds=30,
            )
            fixtures.append(refusal.Fixture(case, row.pk, task_id, serialized, runtime.digest))
    return tuple(fixtures)


def scan(
    *, mode: str, config: dict, expected: dict[int, str], deadline: float, private: list[str]
) -> None:
    """Invoke only the public command and retain failures across all cursor pages."""
    from django.core.management import call_command
    from django.core.management.base import CommandError
    from django.test import override_settings

    found = {}
    cursor = None
    high_water = None
    total = 0
    previous_after = 0
    with override_settings(DJANGO_RAY=config):
        for _page in range(MAX_PAGES):
            output, error_output = BoundedOutput(), BoundedOutput()
            code = 0
            try:
                call_command(
                    "django_ray_runtime_env",
                    database="default",
                    format="json",
                    mode=mode,
                    confirm=mode == "rewrite",
                    cursor=cursor,
                    max_rows=PAGE_ROWS,
                    max_bytes=MAX_ROW_BYTES,
                    max_total_bytes=1024 * 1024,
                    max_seconds=min(5, remaining(deadline)),
                    max_details=PAGE_ROWS,
                    stdout=output,
                    stderr=error_output,
                )
            except CommandError as exc:
                code = exc.returncode
            remaining(deadline)
            assert_clear([output.getvalue().encode(), error_output.getvalue().encode()], private)
            report = json.loads(output.getvalue())
            counts = report.get("counts", {})
            details = report.get("details", [])
            if (
                report.get("schema") != "django-ray.runtime-env-maintenance"
                or report.get("schema_version") != 1
                or report.get("mode") != mode
                or report.get("exit_code") != code
                or report.get("cleanup_confirmed") is not True
                or report.get("stop_code") is not None
                or report.get("budget_exhausted") is not None
                or report.get("details_omitted") != 0
                or report.get("fresh_pass_required") is not True
                or report.get("current_table_only") is not True
                or report.get("backup_obligations") != "not_verified"
                or report.get("key_retirement_authorized") is not False
                or not isinstance(details, list)
                or len(details) > PAGE_ROWS
                or report.get("processed") != len(details)
                or not isinstance(counts, dict)
                or any(type(value) is not int or value < 0 for value in counts.values())
            ):
                raise ValueError("RuntimeEnv maintenance command did not complete its bounded page")
            observed = {}
            for detail in details:
                if not isinstance(detail, dict) or set(detail) != {"pk", "outcome"}:
                    raise ValueError("RuntimeEnv maintenance command returned invalid detail")
                pk, outcome = detail["pk"], detail["outcome"]
                if type(pk) is not int or pk in found or expected.get(pk) != outcome:
                    raise ValueError(
                        "RuntimeEnv maintenance command returned an unexpected outcome"
                    )
                found[pk] = outcome
                observed[outcome] = observed.get(outcome, 0) + 1
            if any(count != observed.get(name, 0) for name, count in counts.items()) or set(
                observed
            ) - set(counts):
                raise ValueError("RuntimeEnv maintenance command counters lost an outcome")
            if high_water is None:
                high_water = report.get("high_water")
            if (
                type(high_water) is not int
                or high_water != max(expected, default=0)
                or report.get("high_water") != high_water
                or type(report.get("after")) is not int
                or report["after"] <= previous_after
                or report["after"] > high_water
                or type(report.get("processed_bytes")) is not int
                or report["processed_bytes"] < 0
            ):
                raise ValueError("RuntimeEnv maintenance cursor changed its observed range")
            previous_after = report["after"]
            total += report["processed_bytes"]
            if total > MAX_TOTAL_BYTES:
                raise ValueError("RuntimeEnv maintenance scan exceeded its aggregate byte bound")
            blocked = any(observed.get(name, 0) for name in ("invalid_snapshot", "unavailable_key"))
            more = report.get("has_more")
            if type(more) is not bool or code != (1 if blocked else 2 if more else 0):
                raise ValueError("RuntimeEnv maintenance command hid a page failure")
            if not more:
                if (
                    report.get("range_complete") is not True
                    or report.get("next_cursor") is not None
                    or found != expected
                ):
                    raise ValueError("RuntimeEnv maintenance scan omitted an observed row")
                return
            cursor = report.get("next_cursor")
            if (
                report.get("range_complete") is not False
                or not isinstance(cursor, str)
                or not 1 <= len(cursor) <= 4096
            ):
                raise ValueError("RuntimeEnv maintenance scan has no bounded continuation")
    raise ValueError("RuntimeEnv maintenance scan exceeded its page bound")


def verify_maintenance(
    request, *, token: str, manager_prefix: str, refusal_ids: list[int], evidence: dict
) -> None:
    if (
        os.environ.get("DJANGO_SETTINGS_MODULE") != "testproject.settings_qualification"
        or evidence != empty_evidence()
    ):
        raise ValueError("RuntimeEnv maintenance requires the disposable qualification fixture")
    from importlib.metadata import version

    from django.db import transaction

    from django_ray.models import RayTaskExecution
    from django_ray.runtime.runtime_env import runtime_env_for_execution
    from qualification.application.run_core import observe_runtime_probe, verify_durable_task

    deadline = time.monotonic() + SECONDS
    config = fixture_configuration()
    before = inventory()
    if len(before) > MAX_ROWS - 3:
        raise ValueError("RuntimeEnv maintenance fixture has no inventory headroom")
    blockers = expected_blockers(refusal_ids, before, manager_prefix=manager_prefix)
    # The maintenance command has no row filter. Refuse unexpected legacy inputs
    # before invoking a mutating global pass on this disposable database.
    for pk, item in before.items():
        if pk not in blockers and json.loads(item.serialized).get("key_id") != "qualification":
            raise ValueError("RuntimeEnv maintenance found an unexpected preexisting snapshot")
    evidence["scan"]["bounded_inventory"] = True
    remaining(deadline)
    fixtures = create_probes(config)
    owned_before = inventory()
    if {
        pk: row for pk, row in owned_before.items() if pk not in {item.pk for item in fixtures}
    } != before:
        raise ValueError("RuntimeEnv maintenance inventory changed before the scan")
    private = [*MARKERS, config["RUNTIME_ENV_ENCRYPTION_KEYS"][OLD_KEY_ID]]
    private += [item.serialized for item in fixtures]
    old_envelope = json.loads(fixtures[1].serialized)
    private += [old_envelope[name] for name in ("nonce", "ciphertext")]
    expected = {pk: blockers.get(pk, "current_key") for pk in before}
    expected.update(
        {
            item.pk: "rewritten" if item.case == "plaintext_rewrite" else "rewrapped"
            for item in fixtures
        }
    )
    scan(mode="rewrite", config=config, expected=expected, deadline=deadline, private=private)
    evidence["scan"]["rewrite_range_complete"] = True
    after = inventory()
    for item in fixtures:
        old, new = owned_before[item.pk], after[item.pk]
        if old.other_fields != new.other_fields or old.serialized == new.serialized:
            raise ValueError("RuntimeEnv maintenance changed probe identity or failed to rewrite")
        row = RayTaskExecution.objects.get(pk=item.pk, task_id=item.task_id)
        if row.attempts.exists() or row.state != "QUEUED":
            raise ValueError("RuntimeEnv maintenance probe escaped its held queue")
        runtime = runtime_env_for_execution(row)
        if (
            runtime.digest != item.digest
            or json.loads(new.serialized).get("key_id") != "qualification"
        ):
            raise ValueError("RuntimeEnv maintenance did not authenticate the original snapshot")
        evidence[item.case].update(active_key_authenticated=True, identity_preserved=True)
        private += [
            new.serialized,
            *[json.loads(new.serialized)[name] for name in ("nonce", "ciphertext")],
        ]
    if {pk: row for pk, row in after.items() if pk in before} != before or len(after) != len(
        before
    ) + 2:
        raise ValueError("RuntimeEnv maintenance changed an unrelated row")
    evidence["scan"]["unrelated_rows_preserved"] = True
    expected.update({item.pk: "current_key" for item in fixtures})
    scan(mode="check", config=config, expected=expected, deadline=deadline, private=private)
    if inventory() != after:
        raise ValueError("RuntimeEnv maintenance check changed a row")
    evidence["scan"].update(
        check_range_complete=True, expected_blockers_visible=True, command_output_clear=True
    )
    remaining(deadline)
    # Publish both validated rows together, then let the real existing managers
    # claim them. The temporary old key is already out of process settings.
    with transaction.atomic():
        for item in fixtures:
            if (
                RayTaskExecution.objects.filter(
                    pk=item.pk, task_id=item.task_id, state="QUEUED", execution_generation=0
                ).update(queue_name="default")
                != 1
            ):
                raise ValueError("RuntimeEnv maintenance probe changed before queue release")
    for item in fixtures:
        observe_runtime_probe(request, task_id=item.task_id, token=token, deadline=deadline)
        verify_durable_task(item.task_id, profile="thin", manager_prefix=manager_prefix)
        remaining(deadline)
        evidence[item.case].update(worker_succeeded=True, original_runtime=True)
    # Create the refusal only after both global passes: it must remain plaintext
    # for the worker/retry proof and must never be silently repaired by that scan.
    source = fixtures[0]
    remaining(deadline)
    task_id = str(uuid4())
    row = RayTaskExecution.objects.create(
        task_id=task_id,
        callable_path=refusal.CALLABLE,
        queue_name="default",
        priority=100,
        state="QUEUED",
        runtime_env_profile="thin",
        runtime_env_json=source.serialized,
        runtime_env_hash=source.digest,
        created_with_django_ray_version=version("django-ray"),
        ray_target_address=config["RAY_ADDRESS"],
        timeout_seconds=30,
    )
    negative = refusal.Fixture(
        "required_plaintext", row.pk, task_id, source.serialized, source.digest
    )
    while True:
        wait = remaining(deadline)
        snapshot = refusal.inspect_failure(negative, manager_prefix=manager_prefix)
        if snapshot is not None:
            break
        time.sleep(min(1, wait))
    remaining(deadline)
    evidence["plaintext_refusal"].update(manager_refused=True, no_submission_identity=True)
    status, body = request(
        f"/api/executions/{negative.pk}/retry",
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
        response_limit=MAX_ROW_BYTES,
    )
    remaining(deadline)
    if status != 409 or json.loads(body) != {
        "detail": "Persisted RuntimeEnv snapshot failed validation"
    }:
        raise ValueError("Required RuntimeEnv plaintext retry did not return its fixed conflict")
    evidence["plaintext_refusal"]["retry_conflict"] = True
    if refusal.inspect_failure(negative, manager_prefix=manager_prefix) != snapshot:
        raise ValueError("Required RuntimeEnv plaintext retry changed its history")
    evidence["plaintext_refusal"]["snapshot_preserved"] = True
    assert_clear(request.bodies, private)
    evidence["surfaces"]["api_clear"] = True
    remaining(deadline)
    # Only known terminal owned rows are removable; failed/in-flight observations
    # stay in the disposable namespace for its existing outer cleanup owner.
    remove_terminal_fixtures(fixtures, negative)
    remaining(deadline)
    evidence["surfaces"]["owned_terminal_rows_removed"] = True
    validate_evidence(evidence)


def remove_terminal_fixtures(fixtures, negative) -> None:
    from django.db import transaction

    from django_ray.models import RayTaskExecution

    with transaction.atomic():
        for item, state in [*((item, "SUCCEEDED") for item in fixtures), (negative, "FAILED")]:
            owned = RayTaskExecution.objects.filter(
                pk=item.pk,
                task_id=item.task_id,
                state=state,
                attempt_number=1,
                execution_generation=1,
                runtime_env_hash=item.digest,
                callable_path=refusal.CALLABLE,
                runtime_env_profile="thin",
            )
            if list(owned.select_for_update().values_list("pk", flat=True)[:2]) != [item.pk]:
                raise ValueError("RuntimeEnv maintenance cleanup lost a terminal owned row")
            owned.delete()
    if RayTaskExecution.objects.filter(pk__in=[item.pk for item in (*fixtures, negative)]).exists():
        raise ValueError("RuntimeEnv maintenance cleanup did not remove its owned rows")
