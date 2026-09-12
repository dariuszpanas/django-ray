"""Persistence owned by the bundled application, not the task backend."""

from django.db import models


class SampleAdmissionBudget(models.Model):
    """One database-wide sample request window, locked before checking backlog."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    window_started_at = models.DateTimeField()
    request_count = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(id=1), name="sample_admission_singleton")
        ]
