"""Small public receipts distinguish stop requests from observed driver exit."""

FACTS = (
    "installed_candidate",
    "control_started_and_progressed",
    "control_alive_without_cancel",
    "control_completed_after_release",
    "control_driver_exited",
    "cancel_started_and_progressed",
    "cancellation_accepted",
    "durable_cancelled_with_requested_status",
    "same_request_attempt_generation",
    "ray_job_stopped",
    "original_driver_exited",
    "progress_stable_after_exit",
    "one_cancelled_attempt_without_result_or_retry",
    "manager_stopped",
    "ray_children_stopped",
)
PHASES = {
    "bootstrap",
    "runtime",
    "manager",
    "control_start",
    "control_release",
    "cancel_start",
    "cancel_request",
    "cancel_observe",
    "cleanup",
    "complete",
}
CHILD_SECONDS = 180
WORK_SECONDS = 150
OUTER_SECONDS = 300
MAX_RECEIPT_BYTES = 16384


def validate_public_receipt(value):
    """Accept only finite diagnostics, including incomplete failed observations."""
    if (
        not isinstance(value, dict)
        or set(value)
        != {"schema_version", "scope", "status", "failure_phase", "cleanup_failed", "facts"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["scope"] != "owned-ray-job-driver"
        or value["status"] not in {"passed", "failed"}
        or value["failure_phase"] not in PHASES | {"none"}
        or type(value["cleanup_failed"]) is not bool
        or not isinstance(value["facts"], dict)
        or set(value["facts"]) != set(FACTS)
        or any(type(flag) is not bool for flag in value["facts"].values())
    ):
        raise ValueError("Invalid public cancellation evidence")


def validate_receipt(value):
    validate_public_receipt(value)
    if (
        value["status"] != "passed"
        or value["failure_phase"] != "none"
        or value["cleanup_failed"] is not False
        or any(flag is not True for flag in value["facts"].values())
    ):
        raise ValueError("Incomplete cancellation driver evidence")


def empty_receipt():
    return {
        "schema_version": 1,
        "scope": "owned-ray-job-driver",
        "status": "failed",
        "failure_phase": "none",
        "cleanup_failed": False,
        "facts": dict.fromkeys(FACTS, False),
    }


def record_failure(receipt, phase, *, cleanup=False):
    if receipt["failure_phase"] == "none":
        receipt["failure_phase"] = phase if phase in PHASES else "bootstrap"
    if cleanup:
        receipt["cleanup_failed"] = True
