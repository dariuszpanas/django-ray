"""Pure route policy shared by API authentication and the local load client."""

DEMO_ROUTE_PREFIXES = ("/stress/", "/enqueue/cpu/", "/local/workload")
DEMO_ROUTES = frozenset(
    {
        "/cluster/cpu-benchmark",
        "/cluster/workflow-benchmark",
        "/cluster/complex-workflow",
        "/cluster/runtime-env/benchmark",
        "/ml/hyperparam-search",
    }
)


def is_demo_route(path: str) -> bool:
    path = path.removeprefix("/api")
    return path in DEMO_ROUTES or path.startswith(DEMO_ROUTE_PREFIXES)


# Every POST is classified explicitly; a regression test rejects unclassified routes.
POST_ROUTE_CAPABILITIES = {
    "/enqueue/add/{a}/{b}": "ordinary",
    "/enqueue/multiply/{a}/{b}": "ordinary",
    "/enqueue/slow/{seconds}": "ordinary",
    "/enqueue/fail": "ordinary",
    "/enqueue/fail-no-retry": "ordinary",
    "/enqueue/intermittent": "ordinary",
    "/enqueue/cpu/{n}": "demo",
    "/enqueue/echo": "ordinary",
    "/executions/{execution_id}/cancel": "lifecycle",
    "/executions/{execution_id}/retry": "lifecycle",
    "/sync/calculate": "ordinary",
    "/sync/validate-email": "ordinary",
    "/local/fibonacci/{n}": "ordinary",
    "/local/workload": "demo",
    "/local/urgent": "ordinary",
    "/stress/cpu": "demo",
    "/stress/memory": "demo",
    "/stress/compute": "demo",
    "/stress/primes": "demo",
    "/stress/json": "demo",
    "/stress/throughput": "demo",
    "/cluster/process-chunk": "fanout",
    "/cluster/batch-http": "fanout",
    "/cluster/search": "fanout",
    "/cluster/cpu-benchmark": "demo",
    "/cluster/workflow-benchmark": "demo",
    "/cluster/complex-workflow": "demo",
    "/cluster/workflow-showcase": "fanout",
    "/cluster/workflow-recovery-showcase": "fanout",
    "/cluster/runtime-env/probe": "ordinary",
    "/cluster/runtime-env/benchmark": "demo",
    "/ml/train": "ordinary",
    "/ml/inference": "ordinary",
    "/ml/hyperparam-search": "demo",
}
