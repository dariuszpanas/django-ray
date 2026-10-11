"""Resolve source-selected images and verify the committed candidate ancestry."""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

from qualification.monitoring.run import command


def main() -> None:
    documents = list(
        yaml.safe_load_all(
            command(["kubectl", "kustomize", "k8s/overlays/kuberay-kind"], seconds=30)
        )
    )
    deployments = {doc["metadata"]["name"]: doc for doc in documents if doc["kind"] == "Deployment"}
    ray = next(doc for doc in documents if doc["kind"] == "RayCluster")
    refs = {
        name: deployments[name]["spec"]["template"]["spec"]["containers"][0]["image"]
        for name in ("prometheus", "grafana", "postgres")
    }
    refs["ray"] = ray["spec"]["headGroupSpec"]["template"]["spec"]["containers"][0]["image"]
    refs["importer"] = ray["spec"]["headGroupSpec"]["template"]["spec"]["containers"][1]["image"]
    images = {"application": os.environ["MONITORING_IMAGE"]}
    for name, ref in refs.items():
        command(["docker", "pull", "--platform", "linux/amd64", ref], seconds=120)
        image = json.loads(command(["docker", "image", "inspect", ref], seconds=10))[0]
        images[name] = image["RepoDigests"][0]
    base, candidate = json.loads(
        command(
            [
                "docker",
                "image",
                "inspect",
                os.environ["MONITORING_BASE_IMAGE"],
                images["application"],
            ],
            seconds=10,
        )
    )
    layers = base["RootFS"]["Layers"]
    if not layers or candidate["RootFS"]["Layers"][: len(layers)] != layers:
        raise ValueError("Monitoring candidate lacks source base ancestry")
    destination = Path(os.environ["RUNNER_TEMP"]) / "monitoring-build"
    (destination / "images.json").write_text(json.dumps(images, sort_keys=True) + "\n")
    (destination / "image-sources.json").write_text(json.dumps(refs, sort_keys=True) + "\n")
    (destination / "ancestry.json").write_text(
        json.dumps(
            {
                "base": os.environ["MONITORING_BASE_IMAGE"],
                "candidate": images["application"],
                "base_layers_verified": True,
            },
            sort_keys=True,
        )
        + "\n"
    )
    print("Verified source-selected immutable monitoring images and candidate ancestry")


if __name__ == "__main__":
    main()
