#!/usr/bin/env python
"""Test script to validate connection to a Ray cluster."""

import argparse
import sys


def test_connection(address: str) -> bool:
    """Test basic connection to Ray cluster."""
    import ray

    print(f"Connecting to Ray cluster at {address}...")
    try:
        ray.init(address=address)
        print("✓ Connected successfully!")
        print(f"  Cluster resources: {ray.cluster_resources()}")
        print(f"  Available resources: {ray.available_resources()}")
        nodes = ray.nodes()
        print(f"  Nodes: {len(nodes)}")
        for node in nodes:
            print(
                f"    - {node.get('NodeManagerAddress', 'unknown')} ({node.get('NodeName', 'unknown')})"
            )
        return True
    except Exception as e:
        print(f"✗ Connection failed: {e}")
        return False


def test_remote_function(address: str) -> bool:
    """Test executing a remote function on the cluster."""
    import ray

    if not ray.is_initialized():
        ray.init(address=address)

    print("\nTesting remote function execution...")

    @ray.remote
    def simple_task(x: int, y: int) -> int:
        import socket

        hostname = socket.gethostname()
        print(f"Running on {hostname}: {x} + {y}")
        return x + y

    try:
        # Submit a simple task
        ref = simple_task.remote(10, 20)
        result = ray.get(ref)
        print("✓ Remote function executed successfully!")
        print(f"  Result: {result}")
        return True
    except Exception as e:
        print(f"✗ Remote function failed: {e}")
        return False


def test_parallel_execution(address: str, num_tasks: int = 5) -> bool:
    """Test parallel task execution on the cluster."""
    import time

    import ray

    if not ray.is_initialized():
        ray.init(address=address)

    print(f"\nTesting parallel execution with {num_tasks} tasks...")

    @ray.remote
    def slow_task(task_id: int, sleep_seconds: float = 1.0) -> dict:
        import socket
        import time as t

        hostname = socket.gethostname()
        print(f"Task {task_id} starting on {hostname}")
        t.sleep(sleep_seconds)
        print(f"Task {task_id} completed on {hostname}")
        return {"task_id": task_id, "hostname": hostname}

    try:
        start = time.time()
        # Submit tasks in parallel
        refs = [slow_task.remote(i) for i in range(num_tasks)]
        results = ray.get(refs)
        elapsed = time.time() - start

        print(f"✓ All {num_tasks} tasks completed in {elapsed:.2f}s")
        hosts = set(r["hostname"] for r in results)
        print(f"  Tasks distributed across {len(hosts)} host(s): {hosts}")
        return True
    except Exception as e:
        print(f"✗ Parallel execution failed: {e}")
        return False


def test_django_ray_task(address: str) -> bool:
    """Test executing a django-ray task on the cluster."""
    import ray

    if not ray.is_initialized():
        ray.init(address=address)

    print("\nTesting django-ray task execution...")

    # This simulates what the django-ray worker does
    @ray.remote
    def run_task(callable_path: str, args_json: str, kwargs_json: str) -> str:
        import json

        # Add src to path if needed
        import os
        import sys

        src_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
        if src_path not in sys.path:
            sys.path.insert(0, src_path)

        print(f"Executing: {callable_path}")

        try:
            # Import the module and get the function
            module_path, func_name = callable_path.rsplit(".", 1)
            import importlib

            module = importlib.import_module(module_path)
            func = getattr(module, func_name)

            # Parse args
            args = json.loads(args_json) if args_json else []
            kwargs = json.loads(kwargs_json) if kwargs_json else {}

            # Execute
            result = func(*args, **kwargs)
            return json.dumps({"success": True, "result": result})
        except Exception as e:
            import traceback

            return json.dumps(
                {"success": False, "error": str(e), "traceback": traceback.format_exc()}
            )

    try:
        # Test with a simple built-in function
        ref = run_task.remote("builtins.sum", "[[1, 2, 3, 4, 5]]", "{}")
        result_json = ray.get(ref)

        import json

        result = json.loads(result_json)
        if result["success"]:
            print("✓ django-ray style task executed successfully!")
            print(f"  Result: {result['result']}")
            return True
        else:
            print(f"✗ Task failed: {result['error']}")
            return False
    except Exception as e:
        print(f"✗ django-ray task failed: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Test connection to a Ray cluster")
    parser.add_argument(
        "--address",
        type=str,
        default="ray://localhost:10001",
        help="Ray cluster address (default: ray://localhost:10001)",
    )
    parser.add_argument(
        "--parallel",
        type=int,
        default=5,
        help="Number of parallel tasks to run (default: 5)",
    )
    parser.add_argument("--skip-parallel", action="store_true", help="Skip parallel execution test")
    args = parser.parse_args()

    print("=" * 60)
    print("Ray Cluster Test")
    print("=" * 60)

    # Run tests
    all_passed = True

    if not test_connection(args.address):
        print("\n⚠ Connection failed. Please check:")
        print("  1. Is the Ray cluster running?")
        print("  2. Is port forwarding active? (kubectl port-forward ...)")
        print("  3. Are Ray versions compatible?")
        sys.exit(1)

    all_passed &= test_remote_function(args.address)

    if not args.skip_parallel:
        all_passed &= test_parallel_execution(args.address, args.parallel)

    all_passed &= test_django_ray_task(args.address)

    # Cleanup
    import ray

    ray.shutdown()

    print("\n" + "=" * 60)
    if all_passed:
        print("✓ All tests passed! Your Ray cluster is ready for django-ray.")
    else:
        print("✗ Some tests failed. Please review the output above.")
    print("=" * 60)

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()
