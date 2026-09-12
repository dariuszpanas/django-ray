"""Require every transaction case and actual independent-connection observations."""

from qualification.docker.scenario import QualificationError

WORKLOAD = "transactional-enqueue-receipts"
DEFINITION_PATH = "qualification/transactions/receipts.yaml"
TEST_PATH = "tests/integration/test_transactional_enqueue.py"
MAX_PROBE_BYTES = 64 * 1024
VISIBILITY = "test_postgresql_observer_sees_task_and_receipt_only_after_outer_commit"
CASES = tuple(
    sorted(
        [
            f"test_{name}[postgresql]"
            for name in (
                "commit_persists_generated_id_and_receipt_on_one_connection",
                "outer_rollback_removes_task_receipt_and_commit_callback",
                "nested_savepoint_failure_preserves_outer_receipts",
                "released_inner_savepoint_does_not_survive_outer_rollback",
                "enqueue_failure_rolls_back_application_receipt_work",
                "receipt_constraint_failure_rolls_back_its_task_only",
                "task_backend_alias_does_not_select_a_database",
                "external_input_object_survives_database_rollback",
                "router_errors_are_contained_before_enqueue",
                "validated_connection_pins_registry_updates_and_task_inserts",
            )
        ]
        + [
            "test_unsupported_routes_fail_before_payload_publication"
            f"[postgresql-{operation}-{model}]"
            for operation in ("db_for_read", "db_for_write")
            for model in ("RayTaskExecution", "TaskInputPayload")
        ]
        + [f"{VISIBILITY}[{commit}]" for commit in (False, True)]
    )
)


def require(condition):
    if not condition:
        raise QualificationError("transaction-proof-mismatch")


def case_nodeids(execution_protocol_version):
    """Select exactly the original categories for one source-declared epoch."""
    require(type(execution_protocol_version) is int and execution_protocol_version in (1, 3))
    return tuple(
        f"{TEST_PATH}::{name.replace('[', f'[{execution_protocol_version}-', 1)}" for name in CASES
    )


def validate_probe(value, *, expected_module):
    require(
        isinstance(value, dict)
        and set(value)
        == {
            "module",
            "execution_protocol_version",
            "server_version",
            "cases",
            "socket_only",
            "server_stopped",
        }
    )
    case_nodeids(value["execution_protocol_version"])
    require(value["socket_only"] is True and value["server_stopped"] is True)
    require(value["module"] == expected_module)
    require(type(value["server_version"]) is int and 170000 <= value["server_version"] < 180000)
    require(isinstance(value["cases"], dict) and sorted(value["cases"]) == list(CASES))
    for name, case in value["cases"].items():
        require(isinstance(case, dict) and set(case) == {"phases", "observations"})
        require(case["phases"] == ["setup:passed", "call:passed", "teardown:passed"])
        observations = case["observations"]
        if name.startswith(VISIBILITY):
            require(
                isinstance(observations, dict)
                and set(observations) == {"writer_pid", "observer_pid", "before", "after"}
            )
            require(
                all(
                    type(observations[key]) is int and observations[key] > 0
                    for key in ("writer_pid", "observer_pid")
                )
            )
            require(observations["writer_pid"] != observations["observer_pid"])
            # The fixture observes execution, application receipt and cohort
            # intent on the same independent connection, in that order.
            for phase in ("before", "after"):
                counts = observations[phase]
                require(
                    type(counts) is list
                    and len(counts) == 3
                    and all(type(count) is int for count in counts)
                )
            require(observations["before"] == [0, 0, 0])
            committed = [1, 1, int(value["execution_protocol_version"] == 3)]
            require(observations["after"] == (committed if name.endswith("[True]") else [0, 0, 0]))
        else:
            require(observations == {})
    return value
