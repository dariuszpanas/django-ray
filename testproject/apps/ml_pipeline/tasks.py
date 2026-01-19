"""Machine learning tasks demonstrating Ray + Django patterns.

These tasks show common ML workflow patterns that benefit from
Ray's distributed execution capabilities.

Note: This uses mock ML operations. In production, replace with
actual ML library calls (scikit-learn, PyTorch, etc.).
"""

from __future__ import annotations

import hashlib
import math
import random
import time
from typing import Any

from django.tasks import task


@task(queue_name="ml")
def preprocess_data(
    raw_data: list[dict[str, Any]],
    operations: list[str] | None = None,
) -> dict[str, Any]:
    """Preprocess raw data for ML training.

    Args:
        raw_data: List of raw data records
        operations: Preprocessing operations to apply
            - "normalize": Normalize numeric values
            - "encode": One-hot encode categorical
            - "fill_na": Fill missing values

    Returns:
        Preprocessed data and statistics
    """
    if operations is None:
        operations = ["normalize", "fill_na"]

    processed = []
    stats = {"null_filled": 0, "normalized": 0}

    for record in raw_data:
        result = record.copy()

        if "fill_na" in operations:
            for key, value in result.items():
                if value is None:
                    result[key] = 0
                    stats["null_filled"] += 1

        if "normalize" in operations:
            for key, value in result.items():
                if isinstance(value, (int, float)) and value != 0:
                    # Simple normalization (mock)
                    result[key] = value / 100.0
                    stats["normalized"] += 1

        processed.append(result)

    return {
        "record_count": len(processed),
        "operations_applied": operations,
        "statistics": stats,
        "sample": processed[:3] if processed else [],
    }


@task(queue_name="ml")
def train_model(
    dataset_id: str,
    hyperparams: dict[str, Any] | None = None,
    epochs: int = 10,
) -> dict[str, Any]:
    """Train a machine learning model.

    Simulates a model training process with progress tracking.

    Args:
        dataset_id: Identifier for the training dataset
        hyperparams: Training hyperparameters
        epochs: Number of training epochs

    Returns:
        Training results and model metadata
    """
    if hyperparams is None:
        hyperparams = {"learning_rate": 0.01, "batch_size": 32}

    start = time.time()

    # Simulate training progress
    history = []
    loss = 1.0

    for epoch in range(epochs):
        # Simulate epoch training time
        time.sleep(0.1)

        # Mock decreasing loss
        loss = loss * (0.9 + random.uniform(-0.05, 0.05))
        accuracy = 1.0 - loss + random.uniform(-0.02, 0.02)

        history.append(
            {
                "epoch": epoch + 1,
                "loss": round(loss, 4),
                "accuracy": round(max(0, min(1, accuracy)), 4),
            }
        )

    # Generate mock model ID
    model_id = hashlib.md5(f"{dataset_id}-{epochs}-{time.time()}".encode()).hexdigest()[:12]

    elapsed = time.time() - start

    return {
        "model_id": f"model_{model_id}",
        "dataset_id": dataset_id,
        "hyperparams": hyperparams,
        "epochs": epochs,
        "training_time_seconds": round(elapsed, 2),
        "final_loss": history[-1]["loss"] if history else None,
        "final_accuracy": history[-1]["accuracy"] if history else None,
        "history": history,
    }


@task(queue_name="ml")
def batch_inference(
    model_id: str,
    samples: list[dict[str, Any]],
) -> dict[str, Any]:
    """Run batch inference on samples.

    Args:
        model_id: Trained model identifier
        samples: Input samples for prediction

    Returns:
        Predictions for all samples
    """
    start = time.time()

    predictions = []
    for i, sample in enumerate(samples):
        # Mock prediction (random for demonstration)
        time.sleep(0.01)  # Simulate inference time

        # Generate deterministic but varied predictions
        sample_hash = hashlib.md5(str(sample).encode()).hexdigest()
        confidence = int(sample_hash[:2], 16) / 255.0  # 0.0 to 1.0

        predictions.append(
            {
                "sample_index": i,
                "prediction": round(confidence, 4),
                "class": "positive" if confidence > 0.5 else "negative",
                "confidence": round(abs(confidence - 0.5) * 2, 4),  # Distance from 0.5
            }
        )

    elapsed = time.time() - start

    return {
        "model_id": model_id,
        "sample_count": len(samples),
        "inference_time_seconds": round(elapsed, 4),
        "avg_latency_ms": round(elapsed * 1000 / len(samples), 2) if samples else 0,
        "predictions": predictions,
        "class_distribution": {
            "positive": sum(1 for p in predictions if p["class"] == "positive"),
            "negative": sum(1 for p in predictions if p["class"] == "negative"),
        },
    }


@task(queue_name="ml")
def feature_engineering(
    records: list[dict[str, Any]],
    feature_configs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Engineer features from raw records.

    Args:
        records: Raw data records
        feature_configs: Feature engineering configurations
            Each config: {"name": str, "type": str, "params": dict}
            Types: "polynomial", "interaction", "binning", "hash"

    Returns:
        Records with engineered features
    """
    if feature_configs is None:
        feature_configs = [
            {"name": "squared", "type": "polynomial", "params": {"degree": 2}},
        ]

    enhanced = []
    features_added = 0

    for record in records:
        result = record.copy()

        for config in feature_configs:
            feat_name = config["name"]
            feat_type = config["type"]
            params = config.get("params", {})

            if feat_type == "polynomial":
                degree = params.get("degree", 2)
                for key, value in record.items():
                    if isinstance(value, (int, float)):
                        result[f"{key}_{feat_name}"] = value**degree
                        features_added += 1

            elif feat_type == "interaction":
                fields = params.get("fields", [])
                if len(fields) >= 2:
                    values = [record.get(f, 0) for f in fields]
                    if all(isinstance(v, (int, float)) for v in values):
                        result[feat_name] = math.prod(values)
                        features_added += 1

            elif feat_type == "hash":
                fields = params.get("fields", list(record.keys()))
                hash_input = "".join(str(record.get(f, "")) for f in fields)
                result[feat_name] = int(hashlib.md5(hash_input.encode()).hexdigest()[:8], 16)
                features_added += 1

        enhanced.append(result)

    return {
        "record_count": len(enhanced),
        "features_added": features_added,
        "feature_configs": feature_configs,
        "sample_output": enhanced[:2] if enhanced else [],
    }


@task(queue_name="ml")
def hyperparameter_search(
    dataset_id: str,
    param_grid: dict[str, list[Any]],
    metric: str = "accuracy",
) -> dict[str, Any]:
    """Search hyperparameter space (grid search).

    Args:
        dataset_id: Dataset identifier
        param_grid: Parameter grid to search
            Example: {"learning_rate": [0.01, 0.001], "batch_size": [32, 64]}
        metric: Metric to optimize

    Returns:
        Best hyperparameters and search results
    """
    import itertools

    # Generate all combinations
    keys = list(param_grid.keys())
    values = list(param_grid.values())
    combinations = list(itertools.product(*values))

    results = []

    for combo in combinations:
        params = dict(zip(keys, combo))

        # Simulate training with these params
        time.sleep(0.05)

        # Mock metric (varies with params)
        param_hash = hashlib.md5(str(params).encode()).hexdigest()
        score = 0.7 + int(param_hash[:2], 16) / 1000.0  # 0.7 to 0.95

        results.append(
            {
                "params": params,
                "score": round(score, 4),
            }
        )

    # Find best
    best = max(results, key=lambda x: x["score"])

    return {
        "dataset_id": dataset_id,
        "metric": metric,
        "combinations_tested": len(combinations),
        "best_params": best["params"],
        "best_score": best["score"],
        "all_results": sorted(results, key=lambda x: -x["score"]),
    }


@task(queue_name="ml")
def evaluate_model(
    model_id: str,
    test_data: list[dict[str, Any]],
) -> dict[str, Any]:
    """Evaluate a trained model on test data.

    Args:
        model_id: Model to evaluate
        test_data: Test dataset

    Returns:
        Evaluation metrics
    """
    # Simulate predictions
    tp, fp, tn, fn = 0, 0, 0, 0

    for sample in test_data:
        # Mock prediction
        sample_hash = hashlib.md5(str(sample).encode()).hexdigest()
        predicted = int(sample_hash[0], 16) > 8  # Random-ish
        actual = sample.get("label", int(sample_hash[1], 16) > 8)

        if predicted and actual:
            tp += 1
        elif predicted and not actual:
            fp += 1
        elif not predicted and actual:
            fn += 1
        else:
            tn += 1

    total = tp + fp + tn + fn
    accuracy = (tp + tn) / total if total > 0 else 0
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0

    return {
        "model_id": model_id,
        "test_samples": len(test_data),
        "metrics": {
            "accuracy": round(accuracy, 4),
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1_score": round(f1, 4),
        },
        "confusion_matrix": {
            "true_positive": tp,
            "false_positive": fp,
            "true_negative": tn,
            "false_negative": fn,
        },
    }
