"""Bounded ordered fold protocol for coordinator-free workflow map results."""

from __future__ import annotations

from collections.abc import Generator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, nullcontext
from typing import Any

from django_ray.runtime import import_utils
from django_ray.runtime.result_buffer import (
    RESULT_BUFFER_ACTOR_MAX_CONCURRENCY,
    RESULT_BUFFER_CODEC,
    RESULT_BUFFER_CODEC_VERSION,
    RESULT_BUFFER_PICKLE_PROTOCOL,
    normalize_result_buffer_actor_options,
    result_buffer_ray_actor_options,
)

RESULT_FOLD_PROTOCOL = "django-ray.workflow-map-result-fold"
RESULT_FOLD_PROTOCOL_VERSION = 1
RESULT_FOLD_CODEC = RESULT_BUFFER_CODEC
RESULT_FOLD_CODEC_VERSION = RESULT_BUFFER_CODEC_VERSION
RESULT_FOLD_PICKLE_PROTOCOL = RESULT_BUFFER_PICKLE_PROTOCOL
# Keep actor execution serial while leaving one per-handle bookkeeping slot for
# Ray to retire a completed prior call before the next protocol transition.
RESULT_FOLD_ACTOR_MAX_PENDING_CALLS = 2
RESULT_FOLD_ACTOR_MAX_CONCURRENCY = RESULT_BUFFER_ACTOR_MAX_CONCURRENCY


class ResultFoldError(RuntimeError):
    """Base exception for the bounded ordered-fold protocol."""


class ResultFoldOverflowError(ResultFoldError):
    """Raised before fold state would exceed its serialized-retention limit."""


class ResultFoldProtocolError(ResultFoldError):
    """Raised when an ordered-fold actor violates its bounded protocol."""


def clone_result_fold_initial(value: Any) -> Any:
    """Validate and clone invocation data with the protocol codec."""
    import ray.cloudpickle as cloudpickle

    serialized = cloudpickle.dumps(value, protocol=RESULT_FOLD_PICKLE_PROTOCOL)
    return cloudpickle.loads(serialized)


def validate_result_fold_value(value: Any) -> Any:
    """Reject deferred reducer results that the serial actor cannot execute."""
    import inspect

    if inspect.isawaitable(value) or inspect.isgenerator(value) or inspect.isasyncgen(value):
        close = getattr(value, "close", None)
        if close is not None:
            close()
        raise ResultFoldProtocolError(
            "Result-fold reducer must return a concrete synchronous value"
        )
    return value


def normalize_result_fold_actor_options(
    actor_options: Mapping[str, Any],
    *,
    max_serialized_bytes: int,
) -> dict[str, Any]:
    """Return the shared canonical v1 actor scheduling contract."""
    normalized = normalize_result_buffer_actor_options(
        actor_options,
        max_serialized_bytes=max_serialized_bytes,
    )
    normalized["max_pending_calls"] = RESULT_FOLD_ACTOR_MAX_PENDING_CALLS
    return normalized


def result_fold_ray_actor_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """Translate canonical fold scheduling options to Ray actor options."""
    return result_buffer_ray_actor_options(options)


def result_fold_plan_contract(
    *,
    max_items: int,
    max_concurrency: int,
    max_serialized_bytes: int,
    actor_options: Mapping[str, Any],
    reducer: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the complete fingerprinted ordered-fold v1 contract."""
    return {
        "protocol": RESULT_FOLD_PROTOCOL,
        "protocol_version": RESULT_FOLD_PROTOCOL_VERSION,
        "codec": {
            "name": RESULT_FOLD_CODEC,
            "version": RESULT_FOLD_CODEC_VERSION,
            "pickle_protocol": RESULT_FOLD_PICKLE_PROTOCOL,
            "measurement": "retained_serialized_bytes",
        },
        "ordering": {
            "kind": "strict_input_order_left_fold",
            "associative_required": False,
            "commutative_required": False,
        },
        "bounds": {
            "maximum_items": max_items,
            "maximum_in_flight_leaves": max_concurrency,
            "maximum_out_of_order_items": min(max_items - 1, max_concurrency - 1),
            "maximum_retained_state_objects": min(max_items, max_concurrency),
            "maximum_serialized_bytes": max_serialized_bytes,
            "maximum_pending_actor_calls": RESULT_FOLD_ACTOR_MAX_PENDING_CALLS,
        },
        "admission": {
            "credit_source": "incorporated_items",
            "initial_credits": max_concurrency,
            "replenishment": "strict_order_fold",
        },
        "initial": {
            "binding": "invocation_data",
            "required": True,
            "persisted_value": False,
            "validated_before_leaf_admission": True,
        },
        "reducer": dict(reducer),
        "actor": dict(actor_options),
        "placement": {
            "scheduling_strategy": actor_options["scheduling_strategy"],
            "custom_resources": dict(actor_options["resources"]),
        },
        "lifetime": {
            "kind": "non_detached",
            "owner": "workflow_coordinator",
            "cleanup": "best_effort",
            "node_loss_recovery": False,
        },
        "restart": {
            "max_restarts": actor_options["max_restarts"],
            "max_task_retries": actor_options["max_task_retries"],
        },
        "result": {
            "cardinality": "one_accumulator",
            "finalize_returns": 2,
            "payload_ref_resolved_by_coordinator": False,
        },
    }


def _ack_base(state: str) -> dict[str, Any]:
    return {
        "protocol": RESULT_FOLD_PROTOCOL,
        "protocol_version": RESULT_FOLD_PROTOCOL_VERSION,
        "codec": RESULT_FOLD_CODEC,
        "codec_version": RESULT_FOLD_CODEC_VERSION,
        "state": state,
    }


def validate_result_fold_ack(
    value: Any,
    *,
    state: str,
    expected_index: int | None = None,
    expected_items: int | None = None,
) -> dict[str, Any]:
    """Validate the small acknowledgement that the coordinator may decode."""
    if not isinstance(value, dict):
        raise ResultFoldProtocolError("Result-fold acknowledgement must be a mapping")
    expected_keys = {
        "protocol",
        "protocol_version",
        "codec",
        "codec_version",
        "state",
        "folded_items",
        "out_of_order_items",
        "retained_bytes",
    }
    if state == "folded":
        expected_keys.update({"index", "released_credits"})
    if set(value) != expected_keys:
        raise ResultFoldProtocolError("Result-fold acknowledgement contains an unexpected payload")
    expected_base = _ack_base(state)
    if any(value.get(key) != item for key, item in expected_base.items()):
        raise ResultFoldProtocolError("Result-fold acknowledgement protocol mismatch")
    for key in (
        "index",
        "folded_items",
        "out_of_order_items",
        "retained_bytes",
        "released_credits",
    ):
        if key in value and (
            isinstance(value[key], bool) or not isinstance(value[key], int) or value[key] < 0
        ):
            raise ResultFoldProtocolError(
                f"Result-fold acknowledgement {key} must be a non-negative integer"
            )
    if expected_index is not None and value.get("index") != expected_index:
        raise ResultFoldProtocolError("Result-fold acknowledgement index mismatch")
    if expected_items is not None and value.get("folded_items") != expected_items:
        raise ResultFoldProtocolError("Result-fold acknowledgement item count mismatch")
    if state == "finalized" and value["out_of_order_items"] != 0:
        raise ResultFoldProtocolError(
            "Result-fold final acknowledgement retained out-of-order items"
        )
    return value


def _decode_result_fold_request(
    serialized: str | None,
    *,
    callable_path: str,
    expected_outer_task_execution_pk: int | None,
    expected_outer_task_id: str | None,
    expected_outer_attempt_number: int | None,
    expected_outer_execution_generation: int | None,
    expected_execution_protocol_version: int | None,
    expected_workflow_run_id: str | None,
    expected_node_id: str | None,
    expected_runtime_env_plan_digest: str | None,
    expected_runtime_env_transport_digest: str | None,
) -> Any | None:
    """Decode a fold boundary before bootstrap, import, or initialization."""
    from django_ray.execution_codec import (
        ExecutionIdentity,
        NestedCallableBindingKind,
        NestedExecutionBoundaryKind,
        NestedExecutionRequestRejected,
        NestedExecutionRequestRejection,
        NestedWorkflowBoundaryIdentity,
        assert_nested_callable_binding,
        decode_nested_execution_request,
    )

    expectations = (
        expected_outer_task_execution_pk,
        expected_outer_task_id,
        expected_outer_attempt_number,
        expected_outer_execution_generation,
        expected_execution_protocol_version,
        expected_workflow_run_id,
        expected_node_id,
        expected_runtime_env_plan_digest,
        expected_runtime_env_transport_digest,
    )
    if serialized is None:
        if any(value is not None for value in expectations):
            raise NestedExecutionRequestRejected(
                NestedExecutionRequestRejection.MISSING_CONTEXT
            ) from None
        return None
    if any(value is None for value in expectations):
        raise NestedExecutionRequestRejected(
            NestedExecutionRequestRejection.MISSING_CONTEXT
        ) from None

    request = decode_nested_execution_request(
        serialized,
        expected_outer_identity=ExecutionIdentity(
            task_execution_pk=expected_outer_task_execution_pk,
            task_id=expected_outer_task_id,
            attempt_number=expected_outer_attempt_number,
            execution_generation=expected_outer_execution_generation,
        ),
        expected_execution_protocol_version=expected_execution_protocol_version,
        expected_boundary_kind=NestedExecutionBoundaryKind.RESULT_FOLD,
        expected_boundary_identity=NestedWorkflowBoundaryIdentity(
            workflow_run_id=expected_workflow_run_id,
            node_id=expected_node_id,
        ),
        expected_callable_binding_kind=NestedCallableBindingKind.PATH,
        expected_callable_binding=callable_path,
        expected_runtime_env_plan_digest=expected_runtime_env_plan_digest,
        expected_runtime_env_transport_digest=expected_runtime_env_transport_digest,
    )
    assert_nested_callable_binding(request, callable_path=callable_path)
    return request


_IMMUTABLE_ACCUMULATOR_TYPES = frozenset(
    {
        type(None),
        bool,
        int,
        float,
        str,
        bytes,
    }
)
_FAST_BOUND_MAX_NODES = 2048


def _inspect_serialized_upper_bound(
    value: Any,
    *,
    max_nodes: int = _FAST_BOUND_MAX_NODES,
    max_bytes: int | None = None,
) -> tuple[int | None, bool]:
    """Return ``(pickle_upper_bound_or_none, elements_deeply_immutable)`` in one scan.

    Always inspects every element without prefix-immutability assumptions so
    in-place element overwrites cannot bypass ``max_serialized_bytes`` or
    shallow-copy rollback safety. Returns ``(None, False)`` when ``value``
    contains custom types, cycles/shared containers, exceeds ``max_nodes``, or
    exceeds ``max_bytes``, signaling callers to run exact ``cloudpickle.dumps``.
    """
    if max_bytes is not None and max_bytes < 16:
        return None, False

    value_type = type(value)
    remaining = max_nodes
    total = 16  # framing overhead (PROTO + STOP + memo headroom)
    elements_immutable = value_type in _IMMUTABLE_ACCUMULATOR_TYPES or value_type in (
        list,
        dict,
        tuple,
        frozenset,
    )
    stack: list[Any] = [value]
    seen_containers: set[int] = set()
    is_root = True

    while stack:
        current = stack.pop()
        remaining -= 1
        if remaining < 0:
            return None, False
        current_type = type(current)
        if current_type is type(None) or current_type is bool:
            total += 8
        elif current_type is int:
            total += 16 + (current.bit_length() + 7) // 8
        elif current_type is float:
            total += 16
        elif current_type is str:
            total += 16 + (len(current) if current.isascii() else 4 * len(current))
        elif current_type is bytes:
            total += 16 + len(current)
        elif current_type is bytearray:
            elements_immutable = False
            total += 16 + len(current)
        elif current_type in (list, tuple, set, frozenset):
            if not is_root and current_type in (list, set):
                elements_immutable = False
            obj_id = id(current)
            if obj_id in seen_containers:
                return None, False
            seen_containers.add(obj_id)
            length = len(current)
            if length > remaining:
                return None, False
            total += 16 + 4 * length
            stack.extend(current)
        elif current_type is dict:
            if not is_root:
                elements_immutable = False
            obj_id = id(current)
            if obj_id in seen_containers:
                return None, False
            seen_containers.add(obj_id)
            length = len(current)
            if 2 * length > remaining:
                return None, False
            total += 16 + 8 * length
            for key, item in current.items():
                stack.append(key)
                stack.append(item)
        else:
            return None, False
        is_root = False
        if max_bytes is not None and total > max_bytes:
            return None, False
    return total, elements_immutable


def _estimate_serialized_upper_bound(
    value: Any,
    *,
    max_nodes: int = _FAST_BOUND_MAX_NODES,
) -> int | None:
    """Return a strict upper bound on pickle-protocol-5 byte length in bounded work."""
    bound, _ = _inspect_serialized_upper_bound(value, max_nodes=max_nodes)
    return bound


class WorkflowMapResultFold:
    """One non-detached actor retaining a bounded ordered-fold accumulator."""

    def __init__(
        self,
        max_items: int,
        max_concurrency: int,
        max_serialized_bytes: int,
        reducer_callable_path: str,
        reducer_bootstrap_django: bool,
        reducer_bound_args: tuple[Any, ...],
        reducer_bound_kwargs: Mapping[str, Any],
        initial: Any,
        *,
        nested_execution_request: str | None = None,
        expected_outer_task_execution_pk: int | None = None,
        expected_outer_task_id: str | None = None,
        expected_outer_attempt_number: int | None = None,
        expected_outer_execution_generation: int | None = None,
        expected_execution_protocol_version: int | None = None,
        expected_workflow_run_id: str | None = None,
        expected_node_id: str | None = None,
        expected_runtime_env_plan_digest: str | None = None,
        expected_runtime_env_transport_digest: str | None = None,
        allowed_callable_modules: Sequence[str] | None = None,
    ) -> None:
        self._nested_request = None
        self._nested_rejection = None
        self._allowed_callable_modules: tuple[str, ...] | None = None
        try:
            self._nested_request = _decode_result_fold_request(
                nested_execution_request,
                callable_path=reducer_callable_path,
                expected_outer_task_execution_pk=expected_outer_task_execution_pk,
                expected_outer_task_id=expected_outer_task_id,
                expected_outer_attempt_number=expected_outer_attempt_number,
                expected_outer_execution_generation=expected_outer_execution_generation,
                expected_execution_protocol_version=expected_execution_protocol_version,
                expected_workflow_run_id=expected_workflow_run_id,
                expected_node_id=expected_node_id,
                expected_runtime_env_plan_digest=expected_runtime_env_plan_digest,
                expected_runtime_env_transport_digest=(expected_runtime_env_transport_digest),
            )
        except Exception as error:
            from django_ray.execution_codec import NestedExecutionRequestRejected

            if not isinstance(error, NestedExecutionRequestRejected):
                raise
            # Actor construction succeeds so the coordinator receives one fixed,
            # trusted rejection from ready() before it admits any mapper effects.
            self._nested_rejection = error
            self._finalized = True
            self._accumulator = None
            self._accumulator_bytes = 0
            self._accumulator_exact = True
            self._accumulator_measured_bytes = 0
            self._accumulator_unverified_bytes = 0
            self._serialized_accumulator = b""
            self._out_of_order = {}
            self._out_of_order_bytes = 0
            self._next_index = 0
            return

        if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1:
            raise ValueError("max_items must be a positive integer")
        if (
            isinstance(max_concurrency, bool)
            or not isinstance(max_concurrency, int)
            or max_concurrency < 1
        ):
            raise ValueError("max_concurrency must be a positive integer")
        if (
            isinstance(max_serialized_bytes, bool)
            or not isinstance(max_serialized_bytes, int)
            or max_serialized_bytes < 1
        ):
            raise ValueError("max_serialized_bytes must be a positive integer")
        if not isinstance(reducer_callable_path, str) or not reducer_callable_path:
            raise TypeError("reducer_callable_path must be a non-empty string")
        if not isinstance(reducer_bootstrap_django, bool):
            raise TypeError("reducer_bootstrap_django must be a boolean")
        if not isinstance(reducer_bound_args, tuple):
            raise TypeError("reducer_bound_args must be a tuple")
        if not isinstance(reducer_bound_kwargs, Mapping):
            raise TypeError("reducer_bound_kwargs must be a mapping")

        normalized_allowed: tuple[str, ...] | None = None
        if allowed_callable_modules is not None:
            if isinstance(allowed_callable_modules, str | bytes) or not isinstance(
                allowed_callable_modules, Sequence
            ):
                raise TypeError("allowed_callable_modules must be a sequence of strings")
            normalized_allowed = tuple(str(entry) for entry in allowed_callable_modules)
        self._allowed_callable_modules = normalized_allowed

        with self._execution_context():
            if reducer_bootstrap_django:
                from django_ray.runtime.entrypoint import bootstrap_django

                bootstrap_django()

            import ray.cloudpickle as cloudpickle

            serialized_initial = cloudpickle.dumps(initial, protocol=RESULT_FOLD_PICKLE_PROTOCOL)
            if len(serialized_initial) > max_serialized_bytes:
                raise ResultFoldOverflowError(
                    "Result-fold initial accumulator serialization exceeds "
                    f"max_serialized_bytes={max_serialized_bytes}"
                )
            cloned_initial = cloudpickle.loads(serialized_initial)

            resolver: Any = getattr(
                import_utils,
                "resolve_workflow_callable",
                import_utils.import_callable,
            )
            if normalized_allowed is not None:
                import inspect as _inspect

                if "allowed_modules" in _inspect.signature(resolver).parameters:
                    reducer = resolver(
                        reducer_callable_path,
                        allowed_modules=normalized_allowed,
                    )
                else:
                    reducer = resolver(reducer_callable_path)
            else:
                reducer = resolver(reducer_callable_path)
            import inspect

            if (
                inspect.iscoroutinefunction(reducer)
                or inspect.isgeneratorfunction(reducer)
                or inspect.isasyncgenfunction(reducer)
            ):
                raise ResultFoldProtocolError(
                    "Result-fold reducer must be synchronous and non-generator"
                )

        self.max_items = max_items
        self.max_concurrency = max_concurrency
        self.max_serialized_bytes = max_serialized_bytes
        self._reducer = reducer
        self._reducer_bound_args = reducer_bound_args
        self._reducer_bound_kwargs = dict(reducer_bound_kwargs)
        self._accumulator: Any = cloned_initial
        self._accumulator_bytes = len(serialized_initial)
        self._accumulator_exact = True
        self._accumulator_measured_bytes = len(serialized_initial)
        self._accumulator_unverified_bytes = 0
        _, self._accumulator_elements_immutable = _inspect_serialized_upper_bound(cloned_initial)
        self._serialized_accumulator: bytes | None = serialized_initial
        self._out_of_order: dict[int, bytes] = {}
        self._out_of_order_bytes = 0
        self._next_index = 0
        self._finalized = False
        self.peak_retained_bytes = len(serialized_initial)
        self.peak_out_of_order_items = 0

    @contextmanager
    def _execution_context(self) -> Generator[None, None, None]:
        request = self._nested_request
        allowed_modules = self._allowed_callable_modules
        with ExitStack() as stack:
            if allowed_modules is not None:
                stack.enter_context(import_utils.active_allowed_callable_modules(allowed_modules))
            if request is not None:
                from django_ray.runtime.compiled_graph import CompiledGraphSubmissionTransport
                from django_ray.runtime.context import durable_task_execution

                profile = request.runtime_env_plan_identity.get("profile")
                stack.enter_context(
                    durable_task_execution(
                        request.outer_identity.task_execution_pk,
                        task_id=request.outer_identity.task_id,
                        execution_protocol_version=request.execution_protocol_version,
                        attempt_number=request.outer_identity.attempt_number,
                        execution_generation=request.outer_identity.execution_generation,
                        runtime_env_profile=profile if isinstance(profile, str) else None,
                        runtime_env_hash="",
                        runtime_env_plan_identity=request.runtime_env_plan_identity,
                        compiled_graph_submission_transport=(
                            CompiledGraphSubmissionTransport.DIRECT_RAY_CORE.value
                        ),
                        strict_execution_request=True,
                    )
                )
            else:
                stack.enter_context(nullcontext())
            yield

    @property
    def retained_bytes(self) -> int:
        return self._accumulator_bytes + self._out_of_order_bytes

    @property
    def folded_items(self) -> int:
        return self._next_index

    def _measure_accumulator_exact_in_context(self, value: Any) -> bytes:
        import ray.cloudpickle as cloudpickle

        return cloudpickle.dumps(value, protocol=RESULT_FOLD_PICKLE_PROTOCOL)

    def _ensure_exact_accumulator_bytes(self) -> int:
        if self._accumulator_exact:
            return self._accumulator_bytes
        with self._execution_context():
            serialized = self._measure_accumulator_exact_in_context(self._accumulator)
        measured = len(serialized)
        self._accumulator_bytes = measured
        self._accumulator_measured_bytes = measured
        self._accumulator_unverified_bytes = 0
        self._accumulator_exact = True
        self._serialized_accumulator = serialized
        return measured

    def ready(self) -> dict[str, Any]:
        """Confirm initial serialization and actor scheduling before leaf effects."""
        if self._nested_rejection is not None:
            raise self._nested_rejection
        return self._ack("ready")

    def append(self, index: int, value: Any) -> dict[str, Any]:
        """Retain or incorporate one mapped item in strict input order."""
        if self._finalized:
            raise ResultFoldProtocolError("Cannot append after result-fold finalization")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ResultFoldProtocolError("Result-fold index must be a non-negative integer")
        if index >= self.max_items:
            raise ResultFoldOverflowError(
                f"Result-fold item index {index} exceeds max_items={self.max_items}"
            )
        if index < self._next_index or index in self._out_of_order:
            raise ResultFoldProtocolError(f"Result-fold index {index} was appended twice")

        import ray.cloudpickle as cloudpickle

        previous_folded = self._next_index
        if index > self._next_index:
            with self._execution_context():
                serialized_item = cloudpickle.dumps(value, protocol=RESULT_FOLD_PICKLE_PROTOCOL)
            if len(serialized_item) > self.max_serialized_bytes:
                raise ResultFoldOverflowError(
                    f"Result-fold item serialization at index {index} exceeds "
                    f"max_serialized_bytes={self.max_serialized_bytes}"
                )
            if len(self._out_of_order) >= self.max_concurrency - 1:
                raise ResultFoldProtocolError(
                    "Result-fold out-of-order retention exceeded the admission window"
                )
            next_bytes = self.retained_bytes + len(serialized_item)
            if next_bytes > self.max_serialized_bytes and not self._accumulator_exact:
                self._ensure_exact_accumulator_bytes()
                next_bytes = self.retained_bytes + len(serialized_item)
            if next_bytes > self.max_serialized_bytes:
                raise ResultFoldOverflowError(
                    "Result-fold combined retained serialization exceeds "
                    f"max_serialized_bytes={self.max_serialized_bytes}"
                )
            self._out_of_order[index] = serialized_item
            self._out_of_order_bytes += len(serialized_item)
        else:
            item_bound = _estimate_serialized_upper_bound(value)
            if item_bound is None or item_bound > self.max_serialized_bytes:
                with self._execution_context():
                    serialized_item = cloudpickle.dumps(value, protocol=RESULT_FOLD_PICKLE_PROTOCOL)
                item_size = len(serialized_item)
                if item_size > self.max_serialized_bytes:
                    raise ResultFoldOverflowError(
                        f"Result-fold item serialization at index {index} exceeds "
                        f"max_serialized_bytes={self.max_serialized_bytes}"
                    )
            else:
                item_size = item_bound
            self._fold_contiguous(index, value, item_size)

        self.peak_retained_bytes = max(self.peak_retained_bytes, self.retained_bytes)
        self.peak_out_of_order_items = max(
            self.peak_out_of_order_items,
            len(self._out_of_order),
        )
        return {
            **self._ack("folded"),
            "index": index,
            "released_credits": self._next_index - previous_folded,
        }

    def append_batch(self, items: Sequence[tuple[int, Any]]) -> list[dict[str, Any]]:
        """Fold or buffer a batch of ``(index, value)`` pairs in one actor call."""
        if self._finalized:
            raise ResultFoldProtocolError("Cannot append after result-fold finalization")
        if isinstance(items, str | bytes | bytearray) or not isinstance(items, Sequence):
            raise ResultFoldProtocolError("Result-fold batch must be a sequence of pairs")
        acks: list[dict[str, Any]] = []
        for entry in items:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise ResultFoldProtocolError(
                    "Result-fold batch entries must be (index, value) tuples"
                )
            index, value = entry
            acks.append(self.append(index, value))
        return acks

    def append_batch_values(
        self,
        indices: Sequence[int],
        *values: Any,
    ) -> list[dict[str, Any]]:
        """Fold or buffer items passed as top-level Ray arguments.

        Receiving ``*values`` as top-level positional arguments ensures Ray
        auto-dereferences leaf ``ObjectRef`` arguments directly on the fold
        actor instead of passing unresolved ``ObjectRef`` objects inside a list.
        """
        if isinstance(indices, str | bytes | bytearray) or not isinstance(indices, Sequence):
            raise ResultFoldProtocolError("Result-fold batch indices must be a sequence")
        if len(indices) != len(values):
            raise ResultFoldProtocolError("Result-fold batch index and value counts do not match")
        return self.append_batch(list(zip(indices, values, strict=True)))

    def _fold_contiguous(self, index: int, value: Any, item_size: int) -> None:
        with self._execution_context():
            self._fold_contiguous_in_context(index, value, item_size)

    def _size_reduced_in_context(
        self,
        *,
        reduced: Any,
        last_measured_bytes: int,
        remaining_out_of_order_bytes: int,
    ) -> tuple[int, bool, int, int, bytes | None, bool]:
        """Return ``(acc_bytes, exact, measured_bytes, unverified_bytes, serialized, elems_immut)``.

        Uses a single full-container upper-bound scan (with no prefix skipping)
        up to ``_FAST_BOUND_MAX_NODES`` and falls back to exact
        ``cloudpickle.dumps`` measurement whenever the bound exceeds the
        remaining byte budget, the container exceeds ``_FAST_BOUND_MAX_NODES``,
        or custom/aliased objects are present.
        """
        remaining_budget = self.max_serialized_bytes - remaining_out_of_order_bytes
        reduced_bound, elements_immutable = _inspect_serialized_upper_bound(
            reduced,
            max_bytes=remaining_budget,
        )
        if reduced_bound is not None:
            return reduced_bound, False, last_measured_bytes, 0, None, elements_immutable

        serialized = self._measure_accumulator_exact_in_context(reduced)
        exact_len = len(serialized)
        if exact_len > self.max_serialized_bytes:
            raise ResultFoldOverflowError(
                "Result-fold accumulator serialization exceeds "
                f"max_serialized_bytes={self.max_serialized_bytes}"
            )
        if exact_len + remaining_out_of_order_bytes > self.max_serialized_bytes:
            raise ResultFoldOverflowError(
                "Result-fold combined retained serialization exceeds "
                f"max_serialized_bytes={self.max_serialized_bytes}"
            )
        return exact_len, True, exact_len, 0, serialized, elements_immutable

    def _fold_contiguous_in_context(
        self,
        index: int,
        first_item: Any,
        first_item_size: int,
    ) -> None:
        import ray.cloudpickle as cloudpickle

        candidate_out_of_order = dict(self._out_of_order) if self._out_of_order else {}
        candidate_out_of_order_bytes = self._out_of_order_bytes
        candidate_index = index
        accumulator = self._accumulator
        candidate_acc_bytes = self._accumulator_bytes
        candidate_exact = self._accumulator_exact
        candidate_measured_bytes = self._accumulator_measured_bytes
        candidate_unverified_bytes = self._accumulator_unverified_bytes
        candidate_serialized = self._serialized_accumulator

        # Preserve transactional rollback for all mutable accumulators (including
        # in-place dict mutations, list element overwrites/pops, and nested
        # mutable containers) without sacrificing O(1) amortized append speed.
        rollback_snapshot: bytes | None = None
        rollback_shallow_list: list[Any] | None = None
        rollback_shallow_dict: dict[Any, Any] | None = None
        acc_type = type(accumulator)
        is_mutable_acc = acc_type not in _IMMUTABLE_ACCUMULATOR_TYPES
        elements_immutable = getattr(self, "_accumulator_elements_immutable", False)
        if is_mutable_acc:
            needs_full_snapshot = (
                bool(candidate_out_of_order)
                or not elements_immutable
                or acc_type not in {list, dict}
                or candidate_acc_bytes + first_item_size + 256 > self.max_serialized_bytes
            )
            if needs_full_snapshot:
                rollback_snapshot = (
                    candidate_serialized
                    if candidate_serialized is not None
                    else self._measure_accumulator_exact_in_context(accumulator)
                )
            elif acc_type is list:
                rollback_shallow_list = list(accumulator)
            elif acc_type is dict:
                rollback_shallow_dict = dict(accumulator)

        candidate_elements_immutable = elements_immutable
        current_item = first_item
        try:
            while True:
                reduced = self._reducer(
                    accumulator,
                    current_item,
                    *self._reducer_bound_args,
                    **self._reducer_bound_kwargs,
                )
                reduced = validate_result_fold_value(reduced)

                candidate_index += 1
                next_serialized = candidate_out_of_order.pop(candidate_index, None)
                if next_serialized is not None:
                    candidate_out_of_order_bytes -= len(next_serialized)

                (
                    candidate_acc_bytes,
                    candidate_exact,
                    candidate_measured_bytes,
                    candidate_unverified_bytes,
                    candidate_serialized,
                    candidate_elements_immutable,
                ) = self._size_reduced_in_context(
                    reduced=reduced,
                    last_measured_bytes=candidate_measured_bytes,
                    remaining_out_of_order_bytes=candidate_out_of_order_bytes,
                )
                accumulator = reduced
                if next_serialized is None:
                    break
                current_item = cloudpickle.loads(next_serialized)
        except Exception:
            if rollback_snapshot is not None:
                self._accumulator = cloudpickle.loads(rollback_snapshot)
            elif rollback_shallow_list is not None:
                self._accumulator = rollback_shallow_list
            elif rollback_shallow_dict is not None:
                self._accumulator = rollback_shallow_dict
            raise

        self._accumulator = accumulator
        self._accumulator_bytes = candidate_acc_bytes
        self._accumulator_exact = candidate_exact
        self._accumulator_measured_bytes = candidate_measured_bytes
        self._accumulator_unverified_bytes = candidate_unverified_bytes
        self._accumulator_elements_immutable = candidate_elements_immutable
        self._serialized_accumulator = candidate_serialized
        self._out_of_order = candidate_out_of_order
        self._out_of_order_bytes = candidate_out_of_order_bytes
        self._next_index = candidate_index

    def finalize(self, expected_items: int) -> tuple[Any, dict[str, Any]]:
        """Return one direct accumulator object plus a small acknowledgement."""
        if self._finalized:
            raise ResultFoldProtocolError("Result fold was already finalized")
        if (
            isinstance(expected_items, bool)
            or not isinstance(expected_items, int)
            or expected_items < 0
            or expected_items > self.max_items
        ):
            raise ResultFoldProtocolError("Invalid result-fold final item count")
        if self._next_index != expected_items or self._out_of_order:
            raise ResultFoldProtocolError(
                "Result-fold finalization found missing or unexpected item indices"
            )

        if not self._accumulator_exact:
            measured = self._ensure_exact_accumulator_bytes()
            if measured > self.max_serialized_bytes:
                raise ResultFoldOverflowError(
                    "Result-fold accumulator serialization exceeds "
                    f"max_serialized_bytes={self.max_serialized_bytes}"
                )
            self.peak_retained_bytes = max(self.peak_retained_bytes, self.retained_bytes)

        retained_bytes = self.retained_bytes
        accumulator = self._accumulator
        self._accumulator = None
        self._accumulator_bytes = 0
        self._accumulator_exact = True
        self._accumulator_measured_bytes = 0
        self._accumulator_unverified_bytes = 0
        self._serialized_accumulator = None
        self._out_of_order.clear()
        self._out_of_order_bytes = 0
        self._finalized = True
        return accumulator, {
            **_ack_base("finalized"),
            "folded_items": expected_items,
            "out_of_order_items": 0,
            "retained_bytes": retained_bytes,
        }

    def discard(self) -> dict[str, Any]:
        """Drop accumulator and out-of-order state during best-effort cleanup."""
        folded_items = self._next_index
        out_of_order_items = len(self._out_of_order)
        retained_bytes = self.retained_bytes
        self._accumulator = None
        self._accumulator_bytes = 0
        self._accumulator_exact = True
        self._accumulator_measured_bytes = 0
        self._accumulator_unverified_bytes = 0
        self._serialized_accumulator = None
        self._out_of_order.clear()
        self._out_of_order_bytes = 0
        self._finalized = True
        return {
            **_ack_base("discarded"),
            "folded_items": folded_items,
            "out_of_order_items": out_of_order_items,
            "retained_bytes": retained_bytes,
        }

    def _ack(self, state: str) -> dict[str, Any]:
        return {
            **_ack_base(state),
            "folded_items": self._next_index,
            "out_of_order_items": len(self._out_of_order),
            "retained_bytes": self.retained_bytes,
        }


__all__ = [
    "RESULT_FOLD_ACTOR_MAX_CONCURRENCY",
    "RESULT_FOLD_ACTOR_MAX_PENDING_CALLS",
    "RESULT_FOLD_CODEC",
    "RESULT_FOLD_CODEC_VERSION",
    "RESULT_FOLD_PICKLE_PROTOCOL",
    "RESULT_FOLD_PROTOCOL",
    "RESULT_FOLD_PROTOCOL_VERSION",
    "ResultFoldError",
    "ResultFoldOverflowError",
    "ResultFoldProtocolError",
    "WorkflowMapResultFold",
    "clone_result_fold_initial",
    "normalize_result_fold_actor_options",
    "result_fold_plan_contract",
    "result_fold_ray_actor_options",
    "validate_result_fold_ack",
    "validate_result_fold_value",
]
