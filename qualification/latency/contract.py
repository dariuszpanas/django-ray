"""Fail closed on incomplete manager, timing, identity or cleanup observations."""

from __future__ import annotations

import math
import re
from xml.etree import ElementTree

from qualification.docker.scenario import QualificationError

CASES = ("recovery-only", "capacity-one", "failure", "api-outage", "manager-replacement")
WORKLOAD = "ray-job-completion-latency"
DEFINITION_PATH = "qualification/latency/jobs.yaml"
MAX_PROBE_BYTES = 128 * 1024


def require(condition):
    if not condition:
        raise QualificationError("latency-proof-mismatch")


def number(value):
    require(type(value) in (int, float) and math.isfinite(value) and value >= 0)


def validate_probe(value, *, expected_module):
    require(
        isinstance(value, dict)
        and set(value)
        == {
            "schema_version",
            "module",
            "cases",
            "failure",
            "ray_shutdown",
            "managers_stopped",
        }
    )
    require(type(value["schema_version"]) is int and value["schema_version"] == 1)
    require(value["module"] == expected_module and value["failure"] is None)
    require(value["ray_shutdown"] is True and value["managers_stopped"] is True)
    require(isinstance(value["cases"], list) and len(value["cases"]) == len(CASES))
    identities = set()
    job_ids = set()
    for name, case in zip(CASES, value["cases"], strict=True):
        require(
            isinstance(case, dict)
            and set(case)
            == {
                "name",
                "tasks",
                "managers",
                "initial_reconciliation_ns",
                "capacity_claim_delays_seconds",
                "api_requests",
                "passed",
            }
        )
        require(case["name"] == name and case["passed"] is True)
        number(case["initial_reconciliation_ns"])
        count = 3 if name == "capacity-one" else 1
        require(isinstance(case["tasks"], list) and len(case["tasks"]) == count)
        for task in case["tasks"]:
            require(
                isinstance(task, dict)
                and set(task)
                == {
                    "task_pk",
                    "job_id",
                    "attempt",
                    "generation",
                    "module",
                    "state",
                    "job_started_ns",
                    "database_times",
                    "released_ns",
                    "committed_ns",
                    "terminal_ns",
                    "receipt_to_terminal_seconds",
                    "release_to_terminal_seconds",
                }
            )
            require(type(task["task_pk"]) is int and task["task_pk"] > 0)
            require(task["task_pk"] not in identities)
            identities.add(task["task_pk"])
            require(
                isinstance(task["job_id"], str)
                and re.fullmatch(r"raysubmit_django_ray_rq2_[0-9a-f]{64}", task["job_id"])
                is not None
            )
            require(task["job_id"] not in job_ids)
            job_ids.add(task["job_id"])
            require(type(task["attempt"]) is int and task["attempt"] == 1)
            require(type(task["generation"]) is int and task["generation"] >= 0)
            require(task["module"] == expected_module)
            require(task["state"] == ("FAILED" if name == "failure" else "SUCCEEDED"))
            for field in ("job_started_ns", "released_ns", "committed_ns", "terminal_ns"):
                require(type(task[field]) is int and task[field] > 0)
            require(
                task["terminal_ns"]
                >= task["committed_ns"]
                >= task["released_ns"]
                >= task["job_started_ns"]
            )
            times = task["database_times"]
            require(
                isinstance(times, dict)
                and set(times) == {"created_ns", "claimed_ns", "finished_ns"}
            )
            require(all(type(stamp) is int and stamp > 0 for stamp in times.values()))
            require(times["created_ns"] <= times["claimed_ns"] <= times["finished_ns"])
            for prefix, initial in (("receipt", "committed_ns"), ("release", "released_ns")):
                duration = task[f"{prefix}_to_terminal_seconds"]
                number(duration)
                require(duration == (task["terminal_ns"] - task[initial]) / 1e9)
            delay = task["receipt_to_terminal_seconds"]
            require(delay >= 10 if name == "recovery-only" else delay < 5)
        expected_delays = [
            (later["database_times"]["claimed_ns"] - earlier["database_times"]["finished_ns"]) / 1e9
            for earlier, later in zip(case["tasks"], case["tasks"][1:], strict=False)
        ]
        require(case["capacity_claim_delays_seconds"] == expected_delays)
        require(all(0 <= delay < 5 for delay in expected_delays))
        require(isinstance(case["managers"], list))
        require(len(case["managers"]) == (2 if name == "manager-replacement" else 1))
        for index, manager in enumerate(case["managers"]):
            require(
                isinstance(manager, dict)
                and set(manager)
                == {
                    "counters",
                    "recovery_only",
                    "module",
                    "elapsed_seconds",
                    "shutdown_exit_code",
                }
            )
            require(manager["module"] == expected_module and manager["shutdown_exit_code"] == 143)
            require(manager["recovery_only"] is (name == "recovery-only"))
            number(manager["elapsed_seconds"])
            counters = manager["counters"]
            require(
                isinstance(counters, dict)
                and set(counters)
                == {
                    "queries",
                    "query_seconds",
                    "fast_polls",
                    "fast_queries",
                    "fast_completed",
                    "reconciliations",
                    "peak_active",
                }
            )
            for key, counter in counters.items():
                number(counter)
                require(key == "query_seconds" or type(counter) is int)
            require(counters["queries"] >= counters["fast_queries"])
            require(counters["queries"] > 0 and counters["reconciliations"] > 0)
            require(counters["peak_active"] <= 1)
            expected = (
                0
                if name == "recovery-only" or (name == "manager-replacement" and index == 0)
                else count
            )
            require(counters["fast_completed"] == expected)
            require(counters["fast_polls"] >= expected and counters["fast_queries"] >= expected)
        requests = case["api_requests"]
        require(isinstance(requests, list) and 0 < len(requests) <= 512)
        for request in requests:
            require(
                isinstance(request, dict) and set(request) == {"method", "path", "status", "at_ns"}
            )
            require(request["method"] in ("GET", "POST", "DELETE"))
            require(isinstance(request["path"], str) and len(request["path"]) <= 512)
            require(type(request["status"]) is int and 100 <= request["status"] <= 599)
            require(type(request["at_ns"]) is int and request["at_ns"] > 0)
        require(
            sum(r["method"] == "POST" and r["path"].rstrip("/") == "/api/jobs" for r in requests)
            == count
        )
        if name == "api-outage":
            require(sum(r["status"] == 503 for r in requests) == 1)
    return value


def junit(receipt, *, expected_module, failure):
    suite = ElementTree.Element(
        "testsuite",
        name=WORKLOAD,
        tests="1" if failure else str(len(CASES)),
        failures="1" if failure else "0",
        errors="0",
        skipped="0",
    )
    if failure is not None:
        case = ElementTree.SubElement(suite, "testcase", classname=WORKLOAD, name="contract")
        ElementTree.SubElement(case, "failure", message=failure)
    else:
        validate_probe(receipt, expected_module=expected_module)
        for name in CASES:
            ElementTree.SubElement(suite, "testcase", classname=WORKLOAD, name=name)
    return ElementTree.tostring(suite, encoding="utf-8", xml_declaration=True) + b"\n"
