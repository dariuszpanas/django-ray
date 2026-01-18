"""ML Pipeline App - Demonstrates machine learning patterns.

This app shows how to use django-ray for ML workflows:
- Model training tasks
- Batch inference
- Data preprocessing
- Feature engineering

Usage:
    from testproject.apps.ml_pipeline.tasks import train_model

    result = train_model.enqueue(
        dataset_id="train_data_v1",
        hyperparams={"learning_rate": 0.01}
    )

Note: This is a demonstration using mock ML operations.
In production, you would integrate with actual ML libraries.
"""

