"""One owned Ray Core refusal with a positively verified shared sentinel mount."""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import ExitStack
from pathlib import Path


def mount_probe():
    """Cloudpickle carries this closure to the same hard-pinned generic worker."""

    def observe(root_name, control, module_digest, *, first):
        import hashlib
        from pathlib import Path

        import ray

        from testproject import qualification_protocol_tasks

        root = Path(root_name)
        if root.is_symlink() or not root.is_dir() or root.resolve() != root:
            raise ValueError("Owned sentinel mount is unavailable")
        with (root / "control").open("rb") as stream:
            if stream.read(65) != control:
                raise ValueError("Owned sentinel mount is not shared")
        with open(qualification_protocol_tasks.__file__, "rb") as stream:
            source = stream.read(16385)
        if len(source) > 16384 or hashlib.sha256(source).hexdigest() != module_digest:
            raise ValueError("Sentinel callable source differs from the candidate")
        if first:
            with (root / "observed").open("xb") as stream:
                stream.write(control)
        return {
            "node": ray.get_runtime_context().get_node_id(),
            "marker_absent": not (root / "invoked").exists(),
        }

    return observe


class Remote:
    """Keep owned references, including uncertainty after a failed submission."""

    def __init__(self):
        self.ray = None
        self.stack = ExitStack()
        self.refs = []
        self.uncertain = False
        self.options = None
        self.node = None
        self.root = None
        self.control = None
        self.source_digest = None
        self.profile = None
        self.plan = None

    def get(self, function, args, kwargs, deadline):
        if time.monotonic() >= deadline:
            raise TimeoutError("Owned remote work deadline expired")
        self.uncertain = True
        ref = function.remote(*args, **kwargs)
        self.refs.append(ref)
        self.uncertain = False
        result = self.ray.get(ref, timeout=max(0.001, deadline - time.monotonic()))
        self.refs.remove(ref)
        if time.monotonic() >= deadline:
            raise TimeoutError("Owned remote result arrived after its deadline")
        return result

    def prepare(self, root, control, deadline):
        import ray
        from ray.util.scheduling_strategies import NodeAffinitySchedulingStrategy

        from django_ray.conf.settings import get_settings
        from django_ray.runtime.runtime_env import (
            prepare_runtime_env_for_ray_core,
            resolve_runtime_env_profile,
            snapshot_local_runtime_env,
        )
        from django_ray.workflow.plans import runtime_env_plan_identity
        from testproject import qualification_protocol_tasks

        if ray.is_initialized() or get_settings()["RAY_ADDRESS"] != "ray://ray-head:10001":
            raise ValueError("Protocol observer requires its own fixed Ray Client connection")
        self.ray = ray
        ray.init(address="ray://ray-head:10001")
        nodes = [n for n in ray.nodes() if n["Alive"]]
        workers = [n for n in nodes if "node:__internal_head__" not in n["Resources"]]
        if len(nodes) != 2 or len(workers) != 1:
            raise ValueError("Protocol observer requires the owned two-node topology")
        self.node = workers[0]["NodeID"]
        self.profile = resolve_runtime_env_profile("project")
        snapshot = self.stack.enter_context(snapshot_local_runtime_env(self.profile))
        self.plan = runtime_env_plan_identity(
            snapshot,
            trust_identity=get_settings().get("WORKFLOW_PLAN_TRUST_IDENTITY", {}),
        )
        self.options = {
            "runtime_env": prepare_runtime_env_for_ray_core(snapshot),
            "scheduling_strategy": NodeAffinitySchedulingStrategy(self.node, soft=False),
            "max_retries": 0,
            "retry_exceptions": False,
        }
        self.root, self.control = root, control
        source = Path(qualification_protocol_tasks.__file__).read_bytes()
        if len(source) > 16384:
            raise ValueError("Sentinel source is oversized")
        self.source_digest = hashlib.sha256(source).hexdigest()
        self.observe(deadline, first=True)
        with (root / "observed").open("rb") as stream:
            if stream.read(65) != control:
                raise ValueError("Actual selected worker did not write the shared control")

    def observe(self, deadline, *, first=False):
        function = self.ray.remote(mount_probe()).options(**self.options)
        value = self.get(
            function, (str(self.root), self.control, self.source_digest), {"first": first}, deadline
        )
        if value != {"node": self.node, "marker_absent": True}:
            raise ValueError("Actual selected worker observed a poison invocation")

    def reject(self, fixture, deadline):
        import django_ray.runtime.remote as remote_module
        from django_ray.execution_codec import (
            ExecutionIdentity,
            ExecutionRequest,
            decode_execution_completion,
            encode_execution_request,
        )
        from django_ray.execution_protocol import ExecutionProtocolRange
        from django_ray.runner.ray_core import (
            _compiled_graph_submission_transport,
            _get_remote_execute_django_task,
        )
        from qualification.application.protocol_fixture import CALLABLE

        if self.root is None:
            raise ValueError("Owned sentinel mount has not been verified")
        identity = ExecutionIdentity(
            task_execution_pk=fixture.pk,
            task_id=fixture.task_id,
            attempt_number=1,
            execution_generation=0,
        )
        request = json.loads(
            encode_execution_request(
                ExecutionRequest(
                    identity=identity,
                    execution_protocol_version=1,
                    callable_path=CALLABLE,
                    transport_version=1,
                    serialized_args=json.dumps(
                        [fixture.generation, fixture.task_id], separators=(",", ":")
                    ),
                    serialized_kwargs="{}",
                    input_reference=None,
                    runtime_env_profile=self.profile.profile,
                    runtime_env_hash=self.profile.digest,
                    runtime_env_plan_identity=self.plan.as_transport_dict(),
                    compiled_graph_submission_transport=_compiled_graph_submission_transport(
                        self.ray
                    ),
                )
            )
        )
        # Exercise ordinary strict-family refusal; never set the target/private
        # protocol-2 transport switch or activate a protocol policy.
        request["execution_protocol_version"] = 2
        # Match the production Core runner: Ray Client deserializes this
        # definition on its generic head before the task RuntimeEnv exists.
        self.ray.cloudpickle.register_pickle_by_value(remote_module)
        raw = self.get(
            _get_remote_execute_django_task().options(**self.options),
            (json.dumps(request, sort_keys=True, separators=(",", ":")),),
            {
                "expected_task_execution_pk": identity.task_execution_pk,
                "expected_task_id": identity.task_id,
                "expected_attempt_number": 1,
                "expected_execution_generation": 0,
                "expected_execution_protocol_version": 2,
            },
            deadline,
        )
        if not isinstance(raw, str) or len(raw.encode()) > 65536:
            raise ValueError("Strict refusal completion exceeded its bound")
        value = decode_execution_completion(
            raw,
            expected_identity=identity,
            expected_execution_protocol_version=2,
            supported_protocols=ExecutionProtocolRange(minimum=1, maximum=2),
        ).completion
        if (
            value.success is not False
            or value.retryable is not False
            or value.error != "execution request rejected: unsupported_protocol"
            or value.exception_type != "RayExecutionRequestIncompatible"
            or value.traceback is not None
            or value.result is not None
            or value.result_reference is not None
        ):
            raise ValueError("Remote worker did not return the fixed strict refusal")
        self.observe(deadline)
        if (self.root / "invoked").exists():
            raise ValueError("Unsupported request invoked its poison callable")

    def cleanup(self, deadline):
        clean = not self.uncertain
        if self.ray is not None:
            from ray import exceptions as errors

            for ref in self.refs:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Remote cleanup deadline expired")
                try:
                    self.ray.cancel(ref, force=True)
                    self.ray.get(ref, timeout=min(5, max(0.001, deadline - time.monotonic())))
                except TimeoutError:
                    # Do not swallow the owned SIGALRM interruption: the
                    # caller must retain time for database restoration.
                    raise
                except Exception as error:
                    # A failed transport does not establish task termination.
                    # Ray may wrap task cancellation in RayTaskError; accept
                    # that explicit terminal cause, but no other RayError.
                    terminal = isinstance(error, errors.TaskCancelledError)
                    if isinstance(error, errors.RayTaskError):
                        cause = error.cause
                        terminal = isinstance(cause, errors.TaskCancelledError) or (
                            isinstance(cause, Exception) and not isinstance(cause, errors.RayError)
                        )
                    clean = clean and terminal
            if time.monotonic() >= deadline:
                raise TimeoutError("Remote cleanup deadline expired")
            try:
                self.ray.shutdown()
            finally:
                self.stack.close()
        else:
            self.stack.close()
        if not clean or time.monotonic() >= deadline:
            raise ValueError("Remote cleanup remains unconfirmed")
