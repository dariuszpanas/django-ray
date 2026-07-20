"""Minimal Ray-only reproducer for the Windows Compiled Graph native crash.

Run each topology in a disposable process under an external hard timeout. The direct
case is expected to terminate Python on affected versions.
"""

from __future__ import annotations

import argparse
import platform

PAYLOAD = "compiled-graph-windows-reproduction"


def _execute_once() -> str:
    import ray
    from ray.dag import InputNode

    class EchoActor:
        def echo(self, value: str) -> str:
            return value

    actor = ray.remote(EchoActor).remote()
    compiled = None
    try:
        with InputNode() as graph_input:
            graph = actor.echo.bind(graph_input)
        compiled = graph.experimental_compile()
        return ray.get(compiled.execute(PAYLOAD), timeout=20)
    finally:
        if compiled is not None:
            compiled.teardown(kill_actors=True)
        else:
            ray.kill(actor, no_restart=True)


def _nested_owner() -> str:
    return _execute_once()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--topology",
        choices=("direct-driver", "nested-ray-task"),
        required=True,
    )
    arguments = parser.parse_args()

    import ray

    print(
        f"ray={ray.__version__} python={platform.python_version()} "
        f"platform={platform.platform()} machine={platform.machine()}",
        flush=True,
    )
    ray.init(num_cpus=2, include_dashboard=False, logging_level="ERROR")
    try:
        if arguments.topology == "direct-driver":
            result = _execute_once()
        else:
            owner = ray.remote(max_retries=0)(_nested_owner)
            result = ray.get(owner.remote(), timeout=30)
        if result != PAYLOAD:
            raise AssertionError(f"Unexpected result: {result!r}")
        print("Compiled Graph execution succeeded.", flush=True)
    finally:
        ray.shutdown()


if __name__ == "__main__":
    main()
