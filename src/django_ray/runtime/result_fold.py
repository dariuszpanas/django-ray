"""Bounded ordered fold protocol for coordinator-free workflow map results."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from django_ray.runtime.import_utils import import_callable
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


class WorkflowMapResultFold:
    """One non-detached actor retaining a serialized ordered-fold accumulator."""

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
    ) -> None:
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

        reducer = import_callable(reducer_callable_path)
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
        self._serialized_accumulator = serialized_initial
        self._out_of_order: dict[int, bytes] = {}
        self._out_of_order_bytes = 0
        self._next_index = 0
        self._finalized = False
        self.peak_retained_bytes = len(serialized_initial)
        self.peak_out_of_order_items = 0

    @property
    def retained_bytes(self) -> int:
        return len(self._serialized_accumulator) + self._out_of_order_bytes

    @property
    def folded_items(self) -> int:
        return self._next_index

    def ready(self) -> dict[str, Any]:
        """Confirm initial serialization and actor scheduling before leaf effects."""
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

        serialized_item = cloudpickle.dumps(value, protocol=RESULT_FOLD_PICKLE_PROTOCOL)
        if len(serialized_item) > self.max_serialized_bytes:
            raise ResultFoldOverflowError(
                f"Result-fold item serialization at index {index} exceeds "
                f"max_serialized_bytes={self.max_serialized_bytes}"
            )

        previous_folded = self._next_index
        if index > self._next_index:
            if len(self._out_of_order) >= self.max_concurrency - 1:
                raise ResultFoldProtocolError(
                    "Result-fold out-of-order retention exceeded the admission window"
                )
            next_bytes = self.retained_bytes + len(serialized_item)
            if next_bytes > self.max_serialized_bytes:
                raise ResultFoldOverflowError(
                    "Result-fold combined retained serialization exceeds "
                    f"max_serialized_bytes={self.max_serialized_bytes}"
                )
            self._out_of_order[index] = serialized_item
            self._out_of_order_bytes += len(serialized_item)
        else:
            self._fold_contiguous(index, serialized_item)

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

    def _fold_contiguous(self, index: int, serialized_item: bytes) -> None:
        import ray.cloudpickle as cloudpickle

        candidate_accumulator = self._serialized_accumulator
        candidate_out_of_order = dict(self._out_of_order)
        candidate_out_of_order_bytes = self._out_of_order_bytes
        candidate_index = index
        current_item = serialized_item

        while True:
            accumulator = cloudpickle.loads(candidate_accumulator)
            item = cloudpickle.loads(current_item)
            reduced = self._reducer(
                accumulator,
                item,
                *self._reducer_bound_args,
                **self._reducer_bound_kwargs,
            )
            reduced = validate_result_fold_value(reduced)
            next_accumulator = cloudpickle.dumps(
                reduced,
                protocol=RESULT_FOLD_PICKLE_PROTOCOL,
            )
            if len(next_accumulator) > self.max_serialized_bytes:
                raise ResultFoldOverflowError(
                    "Result-fold accumulator serialization exceeds "
                    f"max_serialized_bytes={self.max_serialized_bytes}"
                )

            candidate_accumulator = next_accumulator
            candidate_index += 1
            next_item = candidate_out_of_order.pop(candidate_index, None)
            if next_item is not None:
                candidate_out_of_order_bytes -= len(next_item)
            next_retained_bytes = len(candidate_accumulator) + candidate_out_of_order_bytes
            if next_retained_bytes > self.max_serialized_bytes:
                raise ResultFoldOverflowError(
                    "Result-fold combined retained serialization exceeds "
                    f"max_serialized_bytes={self.max_serialized_bytes}"
                )
            if next_item is None:
                break
            current_item = next_item

        self._serialized_accumulator = candidate_accumulator
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

        import ray.cloudpickle as cloudpickle

        retained_bytes = self.retained_bytes
        accumulator = cloudpickle.loads(self._serialized_accumulator)
        self._serialized_accumulator = b""
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
        self._serialized_accumulator = b""
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
