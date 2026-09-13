"""Tiny real workflow and version-specific readers for its retained graph."""

import importlib
import json


def start(payload):
    return {"value": 41, "payload": payload}


def finish(value):
    return {"value": value["value"] + 1, "payload": value["payload"]}


def run(payload):
    from django_ray.workflows import chain, step

    return chain(
        step(start, ray_options={"num_cpus": 0.25}),
        step(finish, ray_options={"num_cpus": 0.25}),
    ).run(payload, use_ray=True)


def read_graph(execution):
    from django.db import InternalError, connection, transaction

    import django_ray

    # 0.5 reorganized these modules; invoke each installed version's own reader.
    released = django_ray.__version__ == "0.4.0"
    reads = importlib.import_module(
        "django_ray.workflow_progress_reads" if released else "django_ray.workflow.progress.reads"
    )
    graphs = importlib.import_module(
        "django_ray.admin_workflow_graph" if released else "django_ray.workflow.admin_graph"
    )

    def authorize(row):
        return row.pk == execution.pk

    envelope = reads.get_workflow_progress_summary(execution, authorize=authorize)
    if released and connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SHOW default_transaction_read_only")
            read_only = cursor.fetchone() == ("on",)
        if read_only:
            # Released topology readers acquire row locks even for reads.
            # Keep the database fence and verify its precise refusal.
            try:
                with transaction.atomic():
                    reads.list_workflow_topology_nodes(execution, authorize=authorize, limit=10)
            except InternalError as error:
                assert getattr(error.__cause__, "sqlstate", None) == "25006"
            else:
                raise AssertionError("released graph reader unexpectedly accepted read-only mode")
            return
    try:
        expectation = graphs.inspect_admin_workflow_graph_summary(envelope)
    except graphs.AdminWorkflowGraphError:
        print(
            json.dumps(
                {
                    "workflow_availability": envelope.get("availability"),
                    "source_schema": envelope.get("source_schema_version"),
                    "stored_summary_bytes": len(execution.workflow_progress_summary_json or ""),
                    "summary": envelope.get("summary"),
                }
            ),
            flush=True,
        )
        raise
    graph = graphs.build_admin_workflow_graph(
        expectation,
        topology_nodes=reads.list_workflow_topology_nodes(execution, authorize=authorize, limit=10),
        topology_edges=reads.list_workflow_topology_edges(execution, authorize=authorize, limit=10),
        node_details=reads.list_workflow_node_details(execution, authorize=authorize, limit=10),
    )
    assert graph["status"] == "AVAILABLE" and graph["complete"] is True
    assert graph["counts"] == {"nodes": 2, "edges": 1}
    assert {node["state"] for node in graph["nodes"]} == {"SUCCEEDED"}
