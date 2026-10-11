"""Executed from this source verbatim inside the generic Ray head image."""

import json

import ray
from ray.dashboard.modules.metrics.grafana_dashboard_factory import (
    generate_default_grafana_dashboard,
)

definition, uid = generate_default_grafana_dashboard()
dashboard = json.loads(definition)
panels = list(dashboard["panels"])
panels.extend(panel for row in dashboard["panels"] for panel in row.get("panels", []))
panel = next(panel for panel in panels if panel.get("id") == 41)
assert uid == "rayDefaultDashboard"
assert (panel["title"], panel["type"]) == ("Cluster Utilization", "graph")
assert panel["targets"][0]["refId"] == "A"
assert (
    panel["targets"][0]["expr"]
    == 'avg(ray_node_cpu_utilization{instance=~"$Instance",SessionName=~"$SessionName",ray_io_cluster=~"$Cluster"})'
)
assert ray.__version__ == "2.58.0"
ray.init(address="auto", logging_level="ERROR", log_to_driver=False)
try:
    nodes = [node for node in ray.nodes() if node["Alive"]]
    assert len(nodes) == 2
    assert sum(node["Resources"].get("CPU", 0) for node in nodes) == 2
    print(
        json.dumps(
            {
                "alive_nodes": 2,
                "logical_cpus": 2,
                "version": ray.__version__,
                "default_dashboard_uid": uid,
                "panel_id": panel["id"],
                "source_panel_type": panel["type"],
            },
            sort_keys=True,
        )
    )
finally:
    ray.shutdown()
